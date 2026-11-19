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


def test_safety_defaults_are_the_conservative_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    # The suite runs with loopback allowed and no host delay; this test needs the real defaults.
    monkeypatch.delenv("WEFT_ALLOW_LOOPBACK")
    monkeypatch.delenv("WEFT_HOST_MIN_INTERVAL_S")
    monkeypatch.delenv("WEFT_RETRY_BACKOFF_S")
    s = Settings()
    assert s.allow_loopback is False
    assert s.host_min_interval_s == 1.0
    assert s.host_concurrency == 2
    assert s.global_concurrency == 16
    assert s.max_retries == 2
    assert s.retry_backoff_s == 0.5
    assert s.breaker_threshold == 5
    assert s.extract_timeout_s == 10


@pytest.mark.parametrize(
    "field",
    [
        "host_concurrency",
        "global_concurrency",
        "breaker_threshold",
        "extract_timeout_s",
    ],
)
def test_new_limits_must_be_positive(field: str) -> None:
    Settings(**{field: 1})  # the field exists; without this an unknown key would also raise
    with pytest.raises(ValidationError):
        Settings(**{field: 0})


@pytest.mark.parametrize("field", ["robots_ttl_s", "breaker_cooldown_s"])
def test_values_that_never_needed_tuning_are_not_settings(field: str) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: 1})


@pytest.mark.parametrize("field", ["host_min_interval_s", "max_retries", "retry_backoff_s"])
def test_zero_is_allowed_where_it_means_off(field: str) -> None:
    Settings(**{field: 0})
    with pytest.raises(ValidationError):
        Settings(**{field: -1})
