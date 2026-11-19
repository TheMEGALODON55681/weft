from pathlib import Path

import pytest
from pydantic import ValidationError

from weft import __version__
from weft.config import Settings


def test_defaults_put_state_under_data_dir() -> None:
    s = Settings()
    assert s.db_path == s.data_dir / "weft.db"
    assert s.user_agent == f"weft/{__version__}"


def test_contact_goes_into_user_agent() -> None:
    s = Settings(contact="ops@example.org")
    assert s.user_agent == f"weft/{__version__} (contact: ops@example.org)"


def test_env_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEFT_MAX_BODY_BYTES", "1234")
    assert Settings().max_body_bytes == 1234


def test_toml_file_is_read_and_env_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / "w.toml"
    cfg.write_text("max_redirects = 3\nmax_links = 7\n")
    monkeypatch.setenv("WEFT_CONFIG", str(cfg))
    monkeypatch.setenv("WEFT_MAX_LINKS", "9")
    s = Settings()
    assert s.max_redirects == 3
    assert s.max_links == 9


def test_unknown_toml_key_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / "w.toml"
    cfg.write_text("max_redirect = 3\n")  # typo
    monkeypatch.setenv("WEFT_CONFIG", str(cfg))
    with pytest.raises(ValidationError):
        Settings()


def test_limits_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(max_body_bytes=0)
