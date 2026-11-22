"""The streamable HTTP app on a free loopback port, in the test's own event loop. Shared by the
transport tests and the load tests."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn

from weft.config import Settings
from weft.server.app import build_server

TOKEN = "t0ken-" + "x" * 30


@asynccontextmanager
async def serve(settings: Settings) -> AsyncIterator[str]:
    """The streamable HTTP app on a free loopback port. Yields the base URL."""
    app = build_server(settings).http_app(transport="http", host_origin_protection="auto")
    # The shutdown timeout keeps a request that never finishes (a broken health check) from
    # hanging the test run: the test fails with a read timeout instead.
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_config=None, timeout_graceful_shutdown=3
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(20):
            while not server.started:
                if task.done():
                    task.result()  # raises what stopped it
                await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


def remote(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        transport="http",
        auth_token=TOKEN,
        **overrides,
    )
