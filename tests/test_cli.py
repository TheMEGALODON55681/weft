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
