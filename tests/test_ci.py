"""The CI workflow, read as text. Nothing here runs a workflow: these pin what the file is for, as
`test_docker.py` does for the Dockerfile, so an edit that unpins an action, reaches for a secret or
drops the Docker job fails here before it fails in a pull request."""

import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def text() -> str:
    if not WORKFLOW.exists():  # an sdist or the Docker image has tests but no .github
        pytest.skip("the workflow file is not in this tree")
    return WORKFLOW.read_text(encoding="utf-8")


def test_every_action_is_pinned_to_a_commit(text: str) -> None:
    uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", text, re.M)
    assert uses, "no actions found, so the pattern is wrong"
    for ref in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), f"{ref} is not pinned to a commit"


def test_no_secrets_and_a_read_only_token(text: str) -> None:
    assert "secrets." not in text
    assert "pull_request_target" not in text
    assert re.search(r"^permissions:\n  contents: read\n", text, re.M)
    assert not re.search(r":\s*write\b", text), "no permission above read"


def test_the_three_jobs_run_on_ubuntu_latest(text: str) -> None:
    jobs = re.findall(r"^  (\w+):\n    name:.*\n    runs-on: (\S+)", text, re.M)
    assert [name for name, _ in jobs] == ["lint", "test", "docker"]
    assert {runner for _, runner in jobs} == {"ubuntu-latest"}


@pytest.mark.parametrize(
    "command",
    [
        "ruff check",
        "ruff format --check",
        "mypy",
        "uv lock --check",
        "pytest",
        "playwright install --with-deps chromium-headless-shell",
        "docker build --target test",
        "docker build --tag weft .",
    ],
)
def test_the_workflow_runs(text: str, command: str) -> None:
    assert command in text


def test_chromium_missing_fails_the_run_instead_of_skipping(text: str) -> None:
    # tests/skips.py fails instead of skipping when CI is set, so both test jobs must set it.
    assert text.count('CI: "true"') + text.count("CI=true") >= 2
