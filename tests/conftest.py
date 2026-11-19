import os
from collections.abc import Iterator

import pytest

from fixture_site import FixtureSite, run_site


@pytest.fixture(scope="session")
def site() -> Iterator[FixtureSite]:
    with run_site() as s:
        yield s


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Keep the host's WEFT_* variables and any weft.toml in the cwd out of every test."""
    for key in [k for k in os.environ if k.startswith("WEFT_")]:
        monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))
