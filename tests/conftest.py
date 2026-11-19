import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack

import pytest

from fixture_site import FixtureSite, run_site
from weft.config import Settings
from weft.fetch.http import Fetcher


@pytest.fixture(scope="session")
def site() -> Iterator[FixtureSite]:
    with run_site() as s:
        yield s


@pytest.fixture
def make_site() -> Iterator[Callable[..., FixtureSite]]:
    """A fresh fixture site per call, for tests that need their own robots.txt or hit counts."""
    with ExitStack() as stack:
        yield lambda **kw: stack.enter_context(run_site(**kw))


@pytest.fixture
async def fetch_with() -> AsyncIterator[Callable[..., Fetcher]]:
    """Build Fetchers from Settings overrides; every one is closed when the test ends."""
    opened: list[Fetcher] = []

    def make(**overrides: object) -> Fetcher:
        f = Fetcher(Settings(**overrides))
        opened.append(f)
        return f

    yield make
    for f in opened:
        await f.aclose()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Keep the host's WEFT_* variables and any weft.toml in the cwd out of every test.

    The fixture site lives on 127.0.0.1, so tests run with loopback allowed, no per-host delay
    and a 10 ms retry backoff. Tests of the SSRF guard, the limiter and the retry timing pass
    their own values; init arguments beat the environment.
    """
    for key in [k for k in os.environ if k.startswith("WEFT_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("WEFT_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("WEFT_HOST_MIN_INTERVAL_S", "0")
    monkeypatch.setenv("WEFT_RETRY_BACKOFF_S", "0.01")
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))
