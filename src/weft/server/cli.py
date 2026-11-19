"""Command line entry point: `weft` or `python -m weft`. Serves MCP over stdio."""

import argparse
import os
import tomllib
from pathlib import Path

from pydantic import ValidationError

from weft import __version__
from weft.config import Settings
from weft.obs.logging import configure_logging
from weft.server.app import build_server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="weft", description="Weft MCP server (stdio). Logs are JSON lines on stderr."
    )
    parser.add_argument("--config", type=Path, help="TOML config file (default: ./weft.toml)")
    parser.add_argument("--version", action="version", version=f"weft {__version__}")
    args = parser.parse_args(argv)

    if args.config is not None:
        if not args.config.is_file():
            parser.error(f"config file not found: {args.config}")
        os.environ["WEFT_CONFIG"] = str(args.config)
    try:
        settings = Settings()
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)
        )
        parser.exit(2, f"weft: invalid configuration: {problems}\n")
    except tomllib.TOMLDecodeError as exc:
        parser.exit(2, f"weft: config file is not valid TOML: {exc}\n")

    configure_logging(settings.log_level)
    build_server(settings).run(transport="stdio", show_banner=False)
