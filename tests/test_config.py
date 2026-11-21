from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

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


# --- the optional model providers -----------------------------------------------------------


def test_the_model_features_are_off_and_unconfigured_by_default() -> None:
    s = Settings()
    assert s.embedding_rerank is False and s.llm_extraction is False
    assert s.embedding_base_url == s.llm_base_url == ""
    assert s.embedding_api_key is None and s.llm_api_key is None


@pytest.mark.parametrize(
    ("flag", "prefix"), [("embedding_rerank", "embedding"), ("llm_extraction", "llm")]
)
def test_a_feature_cannot_be_turned_on_without_an_endpoint_and_a_model(
    flag: str, prefix: str
) -> None:
    with pytest.raises(ValidationError, match=f"{prefix}_base_url and {prefix}_model") as e:
        Settings(**{flag: True})
    assert flag in str(e.value)  # the error names the flag
    with pytest.raises(ValidationError, match=f"{prefix}_base_url and {prefix}_model"):
        Settings(**{flag: True, f"{prefix}_base_url": "https://models.test/v1"})
    with pytest.raises(ValidationError, match=f"{prefix}_base_url and {prefix}_model"):
        Settings(**{flag: True, f"{prefix}_model": "m"})
    ok = Settings(
        **{flag: True, f"{prefix}_base_url": "https://models.test/v1", f"{prefix}_model": "m"}
    )
    assert getattr(ok, flag) is True


@pytest.mark.parametrize("field", ["embedding_base_url", "llm_base_url"])
def test_an_endpoint_must_be_http_or_https(field: str) -> None:
    Settings(**{field: "http://localhost:8080/v1"})
    Settings(**{field: "HTTPS://models.test/v1"})  # a scheme is not case sensitive
    for bad in ("ftp://models.test", "models.test/v1", "file:///etc/passwd"):
        with pytest.raises(ValidationError, match="http"):
            Settings(**{field: bad})


def test_an_api_key_never_shows_in_the_settings_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEFT_LLM_API_KEY", "key-from-the-environment")
    s = Settings(embedding_api_key="key-given-directly")
    assert s.llm_api_key is not None and s.embedding_api_key is not None
    assert s.llm_api_key.get_secret_value() == "key-from-the-environment"
    assert s.embedding_api_key.get_secret_value() == "key-given-directly"
    shown = repr(s) + str(s) + s.model_dump_json()
    assert "key-from-the-environment" not in shown and "key-given-directly" not in shown


@pytest.mark.parametrize("field", ["llm_max_input_chars", "provider_timeout_s"])
def test_provider_limits_must_be_positive(field: str) -> None:
    Settings(**{field: 1})
    with pytest.raises(ValidationError):
        Settings(**{field: 0})


# --- Phase 6: the remote transport -----------------------------------------------------------

TOKEN = "t0ken-" + "x" * 30


def test_the_default_transport_is_stdio_on_loopback_with_no_token() -> None:
    s = Settings()
    assert (s.transport, s.http_host, s.http_port, s.auth_token) == (
        "stdio",
        "127.0.0.1",
        8000,
        None,
    )


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "127.9.9.9", "::1", "[::1]", "localhost", "LOCALHOST"]
)
def test_http_on_loopback_needs_no_token(host: str) -> None:
    assert Settings(transport="http", http_host=host).auth_token is None


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "", "192.168.1.5", "10.0.0.1", "example.org", "127.0.0.1.evil"],
)
def test_http_beyond_loopback_refuses_to_start_without_a_token(host: str) -> None:
    with pytest.raises(ValidationError, match="auth_token is required"):
        Settings(transport="http", http_host=host)
    assert Settings(transport="http", http_host=host, auth_token=TOKEN).auth_token is not None


def test_stdio_ignores_the_bind_address() -> None:
    assert Settings(transport="stdio", http_host="0.0.0.0").transport == "stdio"


@pytest.mark.parametrize("empty", ["", "   ", "\n"])
def test_an_empty_token_counts_as_no_token(empty: str) -> None:
    # An unset variable in a compose file or a secret mount arrives as an empty string.
    assert Settings(auth_token=empty).auth_token is None
    with pytest.raises(ValidationError, match="auth_token is required"):
        Settings(transport="http", http_host="0.0.0.0", auth_token=empty)


@pytest.mark.parametrize("weak", ["short", "a" * 15, "has a space in it, long enough", "tökén" * 6])
def test_a_weak_or_unusable_token_is_refused_and_never_echoed(weak: str) -> None:
    with pytest.raises(ValidationError) as e:
        Settings(auth_token=weak)
    shown = str(e.value.errors(include_input=False, include_url=False))  # as the CLI prints it
    assert "auth_token" in str(e.value) and weak not in shown


def test_a_token_is_trimmed_and_never_shows_in_the_settings_text() -> None:
    s = Settings(auth_token=f"{TOKEN}\n")
    assert s.auth_token is not None and s.auth_token.get_secret_value() == TOKEN
    assert TOKEN not in repr(s) and TOKEN not in str(s)


def test_the_token_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEFT_TRANSPORT", "http")
    monkeypatch.setenv("WEFT_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("WEFT_AUTH_TOKEN", TOKEN)
    s = Settings()
    assert s.auth_token is not None and s.auth_token.get_secret_value() == TOKEN


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_the_port_must_be_a_real_port(port: int) -> None:
    with pytest.raises(ValidationError):
        Settings(http_port=port)


def test_the_transport_is_one_of_two() -> None:
    with pytest.raises(ValidationError):
        Settings(transport="sse")  # type: ignore[arg-type]


@pytest.mark.parametrize("secret", ["", "   "])
def test_an_empty_secret_string_is_no_token(secret: str) -> None:
    """Settings built in code can pass a SecretStr, which a check that only reads str skips."""
    assert Settings(auth_token=SecretStr(secret)).auth_token is None
    with pytest.raises(ValidationError, match="auth_token is required"):
        Settings(transport="http", http_host="0.0.0.0", auth_token=SecretStr(secret))


def test_a_weak_secret_string_is_refused() -> None:
    with pytest.raises(ValidationError):
        Settings(auth_token=SecretStr("short"))


def test_a_good_secret_string_is_kept() -> None:
    s = Settings(auth_token=SecretStr(TOKEN))
    assert s.auth_token is not None and s.auth_token.get_secret_value() == TOKEN
