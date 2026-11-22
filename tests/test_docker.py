"""The Dockerfile, checked without Docker.

Nothing here builds an image. The health check is the one part that runs anywhere, so it runs
against a real server. The rest pins the requirements the file exists to meet, so that an edit
cannot drop one unnoticed.
"""

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from http_app import remote, serve
from test_http import _inherit, http
from weft.db import Database
from weft.server import app as server_app

DOCKERFILE = Path(__file__).parent.parent / "Dockerfile"

if not DOCKERFILE.exists():  # the image's test stage holds tests/ and the package, not the repo
    pytest.skip("the Dockerfile is not in this tree", allow_module_level=True)


def stage(name: str) -> str:
    """The text of one build stage."""
    text = DOCKERFILE.read_text()
    parts = re.split(r"^FROM .* AS (\w+)$", text, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))[name]


def healthcheck() -> list[str]:
    """The probe's own command line, from the Dockerfile, run with this interpreter."""
    line = re.search(r"^HEALTHCHECK .*\\\n\s+CMD (\[.*\])$", DOCKERFILE.read_text(), re.M)
    assert line is not None
    cmd: list[str] = json.loads(line.group(1))
    assert cmd[0] == "python"
    return [sys.executable, *cmd[1:]]


async def probe(port: int) -> int:
    # An HTTP proxy in the environment must not be where the probe goes.
    env = {**_inherit(), "WEFT_HTTP_PORT": str(port), "HTTP_PROXY": "http://127.0.0.1:9"}
    done = await asyncio.to_thread(
        subprocess.run, healthcheck(), env=env, capture_output=True, timeout=30, check=False
    )
    return done.returncode


async def test_the_health_check_passes_on_a_healthy_server(tmp_path: Path) -> None:
    async with serve(remote(tmp_path)) as base:
        assert await probe(int(base.rsplit(":", 1)[1])) == 0


async def test_the_health_check_fails_when_the_server_says_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: Database, fn: object) -> object:
        raise RuntimeError("disk I/O error")

    async with serve(remote(tmp_path)) as base, http() as c:
        port = int(base.rsplit(":", 1)[1])
        assert (await c.get(f"{base}/healthz")).status_code == 200
        with monkeypatch.context() as m:  # undone before the server closes its database
            m.setattr(server_app, "HEALTH_TIMEOUT_S", 0.2)
            m.setattr(Database, "run", broken)
            assert await probe(port) != 0


async def test_the_health_check_fails_when_nothing_listens(tmp_path: Path) -> None:
    async with serve(remote(tmp_path)) as base:
        port = int(base.rsplit(":", 1)[1])
    assert await probe(port) != 0


def test_the_image_runs_as_an_unprivileged_user() -> None:
    runtime = stage("runtime")
    users = re.findall(r"^USER (\S+)$", runtime, flags=re.M)
    assert users and users[-1] not in ("root", "0")
    assert "useradd" in stage("base")  # and the user exists


def test_the_default_target_is_the_runtime_stage() -> None:
    stages = re.findall(r"^FROM .* AS (\w+)$", DOCKERFILE.read_text(), flags=re.M)
    assert stages[-1] == "runtime"  # a plain `docker build` makes the server, not the test run


def test_the_headless_shell_is_installed_with_its_libraries() -> None:
    assert "playwright install --with-deps chromium-headless-shell" in stage("base")


def test_the_playwright_driver_gets_libatomic_in_the_same_layer() -> None:
    # The driver is a bundled Node that links libatomic, which the slim image lacks. Without it
    # `playwright install` exits 127 before --with-deps can install anything, so the package goes
    # in first. The apt lists are removed in the same RUN, or they stay in the layer.
    runs = re.findall(r"^RUN ((?:.*\\\n)*.*)", stage("base"), flags=re.MULTILINE)
    (run,) = [r for r in runs if "playwright install" in r]
    flat = re.sub(r"\s*\\\n\s*", " ", run)
    steps = [
        "apt-get update",
        "apt-get install -y --no-install-recommends libatomic1",
        "playwright install --with-deps chromium-headless-shell",
        "rm -rf /var/lib/apt/lists/*",
    ]
    assert all(step in flat for step in steps), flat
    positions = [flat.index(step) for step in steps]
    assert positions == sorted(positions), flat


def test_the_install_is_exactly_the_lockfile() -> None:
    base = stage("base")
    assert re.findall(r"uv sync (.*)", base) and all(
        "--frozen" in args for args in re.findall(r"uv sync (.*)", base)
    )
    assert (DOCKERFILE.parent / "uv.lock").is_file()
    assert re.search(r"pip install uv==\d+\.\d+\.\d+", base)  # the tool is pinned too


def test_the_server_cannot_start_in_the_image_without_a_token_and_carries_none() -> None:
    text = DOCKERFILE.read_text()
    runtime = stage("runtime")
    assert "WEFT_HTTP_HOST=0.0.0.0" in runtime and "WEFT_TRANSPORT=http" in runtime
    assert "WEFT_AUTH_TOKEN" not in re.sub(r"^#.*$", "", text, flags=re.M)  # only in comments
    assert not re.search(r"^ENV .*(TOKEN|SECRET|KEY)", text, flags=re.M | re.I)


def test_what_is_not_wanted_in_an_image_stays_out_of_the_build_context() -> None:
    ignore = (DOCKERFILE.parent / ".dockerignore").read_text().split()
    for path in (".git", ".handoff", ".venv", ".env", "*.db", "weft.toml"):
        assert path in ignore
