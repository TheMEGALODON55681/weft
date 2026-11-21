"""Command line entry point: `weft` or `python -m weft`. Serves MCP over stdio, or over streamable
HTTP with --transport http."""

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
        prog="weft", description="Weft MCP server. Logs are JSON lines on stderr."
    )
    parser.add_argument("--config", type=Path, help="TOML config file (default: ./weft.toml)")
    parser.add_argument("--transport", choices=["stdio", "http"], help="default: stdio")
    parser.add_argument("--host", help="http bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, help="http port (default: 8000)")
    parser.add_argument("--version", action="version", version=f"weft {__version__}")
    args = parser.parse_args(argv)

    if args.config is not None:
        if not args.config.is_file():
            parser.error(f"config file not found: {args.config}")
        os.environ["WEFT_CONFIG"] = str(args.config)
    try:
        flags = {"transport": args.transport, "http_host": args.host, "http_port": args.port}
        settings = Settings(**{k: v for k, v in flags.items() if v is not None})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" if e["loc"] else e["msg"]
            for e in exc.errors(include_input=False, include_url=False)
        )
        parser.exit(2, f"weft: invalid configuration: {problems}\n")
    except tomllib.TOMLDecodeError as exc:
        parser.exit(2, f"weft: config file is not valid TOML: {exc}\n")

    configure_logging(settings.log_level)
    server = build_server(settings)
    if settings.transport == "http":
        server.run(
            transport="http",
            host=settings.http_host,
            port=settings.http_port,
            show_banner=False,
            # Without a token, a loopback server checks the Host and Origin headers, which stops a
            # web page from reaching it by pointing its own name at 127.0.0.1. With a token the
            # check goes: the token is the defence, and a proxy on this machine forwards under
            # the public host name, which the check would refuse.
            host_origin_protection="auto" if settings.auth_token is None else False,
            # Logging goes through weft's JSON handler. The access log is off, because a client
            # that puts a secret in a query string would have it logged.
            uvicorn_config={"log_config": None, "access_log": False},
        )
    else:
        server.run(transport="stdio", show_banner=False)
