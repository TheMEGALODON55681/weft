from pathlib import Path

import pytest

from weft import __version__
from weft.server.cli import main


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"weft {__version__}"


def test_missing_config_file_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        main(["--config", str(tmp_path / "nope.toml")])
    assert e.value.code == 2
    assert "config file not found" in capsys.readouterr().err


def test_invalid_setting_names_the_field_and_hides_the_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "w.toml"
    cfg.write_text('max_body_bytes = -5\nlog_level = "hunter2"\n')
    with pytest.raises(SystemExit) as e:
        main(["--config", str(cfg)])
    err = capsys.readouterr().err
    assert e.value.code == 2
    assert "max_body_bytes" in err
    assert "log_level" in err
    assert "hunter2" not in err


def test_malformed_toml_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "w.toml"
    cfg.write_text("this is = = not toml")
    with pytest.raises(SystemExit) as e:
        main(["--config", str(cfg)])
    assert e.value.code == 2
    assert "not valid TOML" in capsys.readouterr().err


# --- Phase 6: the transport flags ----------------------------------------------------------


class _Recorder:
    """Stands in for the server so that main() can be called without starting anything."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr("weft.server.cli.build_server", lambda _settings: rec)
    return rec


def test_the_default_is_still_stdio(server: _Recorder) -> None:
    main([])
    assert server.calls == [{"transport": "stdio", "show_banner": False}]


def test_http_binds_where_it_is_told_and_guards_the_host_header(server: _Recorder) -> None:
    main(["--transport", "http", "--host", "127.0.0.1", "--port", "9123"])
    (call,) = server.calls
    assert (call["transport"], call["host"], call["port"]) == ("http", "127.0.0.1", 9123)
    assert call["host_origin_protection"] == "auto"
    assert call["show_banner"] is False


def test_the_environment_can_pick_the_transport_and_a_flag_beats_it(
    server: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEFT_TRANSPORT", "http")
    monkeypatch.setenv("WEFT_HTTP_PORT", "9001")
    main([])
    main(["--port", "9002"])
    main(["--transport", "stdio"])
    assert [c["transport"] for c in server.calls] == ["http", "http", "stdio"]
    assert [c.get("port") for c in server.calls] == [9001, 9002, None]


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "example.org"])
def test_http_beyond_loopback_without_a_token_exits_2_and_starts_nothing(
    host: str, server: _Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as e:
        main(["--transport", "http", "--host", host])
    assert e.value.code == 2
    assert "auth_token is required" in capsys.readouterr().err
    assert server.calls == []


def test_a_token_lets_http_bind_beyond_loopback(
    server: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEFT_AUTH_TOKEN", "t0ken-" + "x" * 30)
    main(["--transport", "http", "--host", "0.0.0.0"])
    assert server.calls[0]["host"] == "0.0.0.0"


def test_a_bad_token_is_named_and_never_printed(
    server: _Recorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WEFT_AUTH_TOKEN", "hunter2")
    with pytest.raises(SystemExit) as e:
        main(["--transport", "http"])
    err = capsys.readouterr().err
    assert e.value.code == 2 and "auth_token" in err and "hunter2" not in err
    assert server.calls == []


def test_an_empty_token_variable_does_not_open_a_public_server(
    server: _Recorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WEFT_AUTH_TOKEN", "")
    with pytest.raises(SystemExit):
        main(["--transport", "http", "--host", "0.0.0.0"])
    assert "auth_token is required" in capsys.readouterr().err
    assert server.calls == []


def test_a_port_out_of_range_exits_2(server: _Recorder, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        main(["--transport", "http", "--port", "70000"])
    assert e.value.code == 2 and "http_port" in capsys.readouterr().err
    assert server.calls == []


def test_a_token_replaces_the_host_check_so_a_proxy_on_the_same_machine_can_forward(
    server: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    main(["--transport", "http"])  # loopback, no token
    monkeypatch.setenv("WEFT_AUTH_TOKEN", "t0ken-" + "x" * 30)
    main(["--transport", "http"])  # loopback, token
    assert [c["host_origin_protection"] for c in server.calls] == ["auto", False]
