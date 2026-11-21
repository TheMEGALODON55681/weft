"""The remote transport: bearer auth, /healthz, and a real server process.

The in-process tests run the streamable HTTP app under uvicorn on a free loopback port, in the
test's own event loop. One test starts `python -m weft` as a child process, which is the only
way to check what the command line really wires up.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastmcp import Client
from mcp.shared.exceptions import MCPError

from fixture_site import FixtureSite
from weft.config import Settings
from weft.db import Database
from weft.server import app as server_app
from weft.server.app import build_server
from weft.server.auth import BearerToken

TOKEN = "t0ken-" + "x" * 30
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@asynccontextmanager
async def serve(settings: Settings) -> AsyncIterator[str]:
    """The streamable HTTP app on a free loopback port. Yields the base URL."""
    app = build_server(settings).http_app(transport="http", host_origin_protection="auto")
    # The shutdown timeout keeps a request that never finishes (a broken health check) from
    # hanging the test run: the test fails with a read timeout instead.
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_config=None, timeout_graceful_shutdown=3
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(20):
            while not server.started:
                if task.done():
                    task.result()  # raises what stopped it
                await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


def remote(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        transport="http",
        auth_token=TOKEN,
        **overrides,
    )


def http() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, timeout=20)


# --- the verifier --------------------------------------------------------------------------


async def test_the_right_token_is_accepted_and_every_other_is_not() -> None:
    verifier = BearerToken(TOKEN)
    assert await verifier.verify_token(TOKEN) is not None
    for wrong in ["", " ", TOKEN + "x", TOKEN[:-1], TOKEN.upper(), "é" * 40, "\x00" * 40]:
        assert await verifier.verify_token(wrong) is None, repr(wrong)


async def test_tokens_are_compared_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """`==` stops at the first differing byte, which tells a caller how much of a guess was
    right. Both sides are hashed first so that a wrong length gives nothing away either."""
    seen: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    verifier = BearerToken(TOKEN)
    for guess in [TOKEN, "short", TOKEN + "more", "é" * 40]:
        await verifier.verify_token(guess)
    assert len(seen) == 4
    expected = hashlib.sha256(TOKEN.encode()).digest()
    assert all(b == expected and len(a) == len(b) == 32 for a, b in seen)


async def test_a_rejected_token_is_logged_without_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    guess = "guess-" + "y" * 30
    with caplog.at_level(logging.INFO, logger="weft"):
        assert await BearerToken(TOKEN).verify_token(guess) is None
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    shown = caplog.text + json.dumps([r.__dict__ for r in caplog.records], default=str)
    assert guess not in shown and TOKEN not in shown


def test_a_verifier_cannot_be_made_with_an_empty_token() -> None:
    # `Authorization: Bearer` followed by nothing would otherwise match.
    with pytest.raises(ValueError, match="empty"):
        BearerToken("")


def test_the_verifier_does_not_show_its_token() -> None:
    assert TOKEN not in repr(BearerToken(TOKEN)) and TOKEN not in str(vars(BearerToken(TOKEN)))


# --- the app over a real socket ------------------------------------------------------------


async def test_healthz_is_open_and_the_endpoint_is_not(tmp_path: Path) -> None:
    async with serve(remote(tmp_path)) as base, http() as c:
        health = await c.get(f"{base}/healthz")
        assert health.status_code == 200 and health.json() == {"status": "ok"}
        assert health.headers["cache-control"] == "no-store"
        refused = await c.post(f"{base}/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        assert refused.status_code == 401
        assert refused.headers["www-authenticate"].startswith("Bearer")
        assert TOKEN not in refused.text


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer",
        "Bearer wrong-wrong-wrong-wrong-wrong",
        f"Bearer {TOKEN}x",
        f"Bearer {TOKEN[:-1]}",
        f"Basic {TOKEN}",
        TOKEN,
    ],
)
async def test_the_endpoint_refuses_every_caller_without_the_exact_token(
    authorization: str | None, site: FixtureSite, tmp_path: Path
) -> None:
    headers = dict(MCP_HEADERS)
    if authorization is not None:
        headers["Authorization"] = authorization
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "fetch_page", "arguments": {"url": site.url("/article")}},
    }
    before = site.total_hits
    async with serve(remote(tmp_path)) as base, http() as c:
        for body in (INITIALIZE, call):
            r = await c.post(f"{base}/mcp", json=body, headers=headers)
            assert r.status_code == 401, r.text
        assert (await c.get(f"{base}/mcp", headers=headers)).status_code == 401
        assert (await c.delete(f"{base}/mcp", headers=headers)).status_code == 401
    assert site.total_hits == before  # nothing was fetched on behalf of a stranger


async def test_the_exact_token_opens_the_endpoint(tmp_path: Path) -> None:
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
    async with serve(remote(tmp_path)) as base, http() as c:
        r = await c.post(f"{base}/mcp", json=INITIALIZE, headers=headers)
    assert r.status_code == 200 and '"serverInfo"' in r.text


async def test_a_client_with_the_token_uses_the_tools_and_gets_progress(
    site: FixtureSite, tmp_path: Path
) -> None:
    progress: list[float] = []

    async def on_progress(done: float, total: float | None, _msg: str | None) -> None:
        progress.append(done)

    async with (
        serve(remote(tmp_path)) as base,
        Client(f"{base}/mcp", auth=TOKEN, progress_handler=on_progress) as client,
    ):
        names = {t.name for t in await client.list_tools()}
        page = (
            await client.call_tool("fetch_page", {"url": site.url("/article")})
        ).structured_content
        crawl = (
            await client.call_tool(
                "crawl_site",
                {"seed_urls": [site.url("/site/")], "goal": "pricing", "max_pages": 6},
            )
        ).structured_content
    assert {"fetch_page", "crawl_site", "search_in_site", "extract", "map_site"} <= names
    assert page is not None and page["title"] == "Pricing and plans"
    assert crawl is not None and crawl["job"]["status"] == "done"
    assert progress  # the stream carries notifications while a call is still open


@pytest.mark.parametrize("name", ["token", "access_token", "api_key", "authorization"])
async def test_a_token_in_the_url_does_not_open_the_endpoint(name: str, tmp_path: Path) -> None:
    # A URL ends up in logs, proxies and browser history. Only the header carries the secret.
    async with serve(remote(tmp_path)) as base, http() as c:
        r = await c.post(f"{base}/mcp?{name}={TOKEN}", json=INITIALIZE, headers=MCP_HEADERS)
    assert r.status_code == 401


async def test_a_client_with_the_wrong_token_is_turned_away(tmp_path: Path) -> None:
    async with serve(remote(tmp_path)) as base:
        with pytest.raises(MCPError):
            async with Client(f"{base}/mcp", auth="wrong-" + "z" * 30) as client:
                await client.list_tools()


async def test_a_loopback_server_may_run_without_a_token(tmp_path: Path) -> None:
    open_settings = Settings(data_dir=tmp_path / "data", transport="http")
    async with serve(open_settings) as base, Client(f"{base}/mcp") as client:
        assert "fetch_page" in {t.name for t in await client.list_tools()}


@pytest.mark.parametrize("failure", ["hangs", "raises"])
async def test_healthz_says_so_when_the_database_does_not_answer(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hang(self: Database, fn: Any) -> Any:
        await asyncio.sleep(3600)

    async def broken(self: Database, fn: Any) -> Any:
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(server_app, "HEALTH_TIMEOUT_S", 0.2)
    async with serve(remote(tmp_path)) as base, http() as c:
        assert (await c.get(f"{base}/healthz")).status_code == 200  # healthy to begin with
        started = time.monotonic()
        with monkeypatch.context() as m:  # undone before the server closes its database
            m.setattr(Database, "run", hang if failure == "hangs" else broken)
            r = await c.get(f"{base}/healthz")
        assert r.status_code == 503 and r.json() == {"status": "unavailable"}
        assert "disk" not in r.text  # no detail for a stranger
        assert time.monotonic() - started < 5


# --- the real command line -----------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def weft_process(tmp_path: Path, token: str | None) -> Iterator[tuple[str, dict[str, str]]]:
    """`python -m weft` serving HTTP on a free loopback port. Yields the base URL and a dict that
    holds the process's `out` and `err` once the block has ended and the process has stopped."""
    port = free_port()
    env = {
        "WEFT_TRANSPORT": "http",
        "WEFT_HTTP_PORT": str(port),
        "WEFT_DATA_DIR": str(tmp_path / "data"),
        "WEFT_LOG_LEVEL": "INFO",
    }
    if token is not None:
        env["WEFT_AUTH_TOKEN"] = token
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [sys.executable, "-m", "weft"],
        env={**_inherit(), **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=flags,
        text=True,
    )
    captured: dict[str, str] = {}
    try:
        _wait_for(f"http://127.0.0.1:{port}/healthz", proc)
        yield f"http://127.0.0.1:{port}", captured
    finally:
        _stop(proc)
        captured["out"], captured["err"] = proc.communicate(timeout=10)


def test_the_command_line_serves_http_with_a_token_and_clean_json_logs(tmp_path: Path) -> None:
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    with weft_process(tmp_path, TOKEN) as (base, got), httpx.Client(trust_env=False) as c:
        assert c.post(f"{base}/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 401
        ok = c.post(f"{base}/mcp", json=INITIALIZE, headers={**MCP_HEADERS, **bearer})
        assert ok.status_code == 200
        # A reverse proxy on the same machine forwards under the public host name. With a token
        # that is served, and without one it is still refused.
        host = {"Host": "weft.example.org"}
        proxied = c.post(f"{base}/mcp", json=INITIALIZE, headers={**MCP_HEADERS, **bearer, **host})
        assert proxied.status_code == 200
        assert (
            c.post(f"{base}/mcp", json=INITIALIZE, headers={**MCP_HEADERS, **host}).status_code
            == 401
        )
        # A client that puts a secret in the URL must not find it in the logs.
        leaky = f"{base}/mcp?access_token=QUERY-SECRET-123"
        assert c.post(leaky, json=INITIALIZE, headers=MCP_HEADERS).status_code == 401
    assert got["out"] == ""  # nothing writes to stdout, which is not the protocol channel here
    assert TOKEN not in got["err"] and "QUERY-SECRET-123" not in got["err"]
    lines = [ln for ln in got["err"].splitlines() if ln.strip()]
    for ln in lines:
        json.loads(ln)  # one format on stderr, the server's own and the HTTP server's alike
    assert [json.loads(ln)["msg"] for ln in lines].count("ready") == 1


def test_a_loopback_server_without_a_token_refuses_pages_that_name_another_host(
    tmp_path: Path,
) -> None:
    """A web page can make a browser send a loopback server a request that names another host
    (DNS rebinding) or carries another origin. With no token, those are all that stands between
    that page and the tools."""
    with weft_process(tmp_path, None) as (base, _), httpx.Client(trust_env=False) as c:
        assert c.post(f"{base}/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 200
        rebound = {**MCP_HEADERS, "Host": "evil.example"}
        assert c.post(f"{base}/mcp", json=INITIALIZE, headers=rebound).status_code == 421
        assert c.get(f"{base}/healthz", headers={"Host": "evil.example"}).status_code == 421
        cross = {**MCP_HEADERS, "Origin": "https://evil.example"}
        assert c.post(f"{base}/mcp", json=INITIALIZE, headers=cross).status_code == 403


def _inherit() -> dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "HOME", "LOCALAPPDATA", "APPDATA")
    return {k: v for k, v in os.environ.items() if k.upper() in keep}


def _wait_for(url: str, proc: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited early: {proc.communicate()[1]}")
        try:
            if httpx.get(url, trust_env=False, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise AssertionError("server did not come up in 30 seconds")


def _stop(proc: subprocess.Popen[str]) -> None:
    """Ask for a clean shutdown, and kill the process if it does not come."""
    if proc.poll() is None:
        proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
