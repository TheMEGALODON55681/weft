"""A local HTTP proxy that Chromium is forced through, so the browser never opens a connection of
its own. Every upstream connection goes through the same GuardedBackend as plain HTTP: the name
is resolved here, every address is checked, and the connection goes to a checked address.

HTTPS and WebSockets arrive as CONNECT and become byte tunnels. Plain HTTP arrives with an
absolute URL and is forwarded in origin form with `Connection: close`, one request per client
connection, so a second request can never ride a connection that was checked for another host.
"""

import asyncio
import logging
from contextlib import suppress
from http import HTTPStatus
from urllib.parse import urlsplit

import httpcore

from weft.fetch.errors import FetchError

log = logging.getLogger("weft.fetch")

MAX_HEAD_BYTES = 64 * 1024
_CHUNK = 64 * 1024
# Headers that describe the hop to the proxy, not the request. Host is rewritten from the URL.
_DROP = frozenset(
    {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "upgrade", "host"}
)


class _BadRequest(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _split_authority(authority: str) -> tuple[str, int]:
    """`host:port` or `[v6]:port` to its parts. The port is required."""
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        port = rest.removeprefix(":") if rest.startswith(":") else ""
    else:
        host, _, port = authority.rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise _BadRequest(400, "expected host:port")
    return host, int(port)


def _parse_head(head: bytes) -> tuple[str, str, list[tuple[str, str]]]:
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[0].isalpha() or not parts[2].startswith("HTTP/1."):
        raise _BadRequest(400, "malformed request line")
    headers = []
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            raise _BadRequest(400, "malformed header")
        headers.append((name, value.strip()))
    return parts[0].upper(), parts[1], headers


async def _reply(writer: asyncio.StreamWriter, status: int, message: str) -> None:
    body = message.encode()
    writer.write(
        f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\nContent-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )
    with suppress(OSError):
        await writer.drain()


class GuardedProxy:
    def __init__(
        self,
        backend: httpcore.AsyncNetworkBackend,
        connect_timeout_s: float,
        idle_timeout_s: float = 60.0,
    ) -> None:
        self._backend = backend
        self._connect_timeout = connect_timeout_s
        self._idle_timeout = idle_timeout_s
        self._server: asyncio.Server | None = None
        self._conns: set[asyncio.Task[None]] = set()

    async def start(self) -> int:
        """Listen on a free loopback port and return it."""
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, limit=MAX_HEAD_BYTES
        )
        return int(self._server.sockets[0].getsockname()[1])

    async def aclose(self) -> None:
        """Stop listening and end every open connection, tunnels included."""
        if self._server is None:
            return
        self._server.close()
        for task in self._conns:
            task.cancel()
        await asyncio.gather(*self._conns, return_exceptions=True)
        await self._server.wait_closed()
        self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task:  # start_server always runs this in a task
            self._conns.add(task)
        try:
            await self._serve(reader, writer)
        except _BadRequest as exc:
            await _reply(writer, exc.status, str(exc))
        except (OSError, asyncio.IncompleteReadError):
            pass  # the client went away
        finally:
            if task:
                self._conns.discard(task)
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError:
            raise _BadRequest(400, "request head too large") from None
        method, target, headers = _parse_head(head[:-4])

        if method == "CONNECT":
            host, port = _split_authority(target)
            upstream = await self._open(host, port, writer)
            if upstream is None:
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await self._relay(reader, writer, upstream, forward_client=True)
            return

        url = urlsplit(target)
        if url.scheme != "http" or not url.hostname or "@" in url.netloc:
            raise _BadRequest(400, "expected an absolute http:// URL")
        try:
            port = url.port or 80
        except ValueError:
            raise _BadRequest(400, "bad port") from None
        names = {n.lower(): v for n, v in headers}
        if "transfer-encoding" in names:
            raise _BadRequest(411, "a request body needs a Content-Length")
        length = names.get("content-length", "0")
        if not length.isdigit():
            raise _BadRequest(400, "bad Content-Length")

        upstream = await self._open(url.hostname, port, writer)
        if upstream is None:
            return
        path = (url.path or "/") + (f"?{url.query}" if url.query else "")
        lines = [f"{method} {path} HTTP/1.1", f"Host: {url.netloc}"]
        lines += [f"{n}: {v}" for n, v in headers if n.lower() not in _DROP]
        lines += ["Connection: close", "", ""]
        await upstream.write("\r\n".join(lines).encode("latin-1"))
        remaining = int(length)
        while remaining:
            chunk = await reader.read(min(_CHUNK, remaining))
            if not chunk:
                break
            await upstream.write(chunk)
            remaining -= len(chunk)
        # Anything else the client sends is read and dropped: it belongs to a request that would
        # need its own connection and its own check.
        await self._relay(reader, writer, upstream, forward_client=False)

    async def _open(
        self, host: str, port: int, writer: asyncio.StreamWriter
    ) -> httpcore.AsyncNetworkStream | None:
        """Connect through the guard, or answer the client with why not and return None."""
        try:
            return await self._backend.connect_tcp(host, port, timeout=self._connect_timeout)
        except FetchError as exc:
            await _reply(writer, 403 if exc.code == "blocked_address" else 502, str(exc))
        except httpcore.ConnectTimeout:
            await _reply(writer, 504, f"timed out connecting to {host}")
        except (httpcore.ConnectError, OSError, ValueError):
            await _reply(writer, 502, f"cannot connect to {host}")
        return None

    async def _relay(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream: httpcore.AsyncNetworkStream,
        forward_client: bool,
    ) -> None:
        """Copy bytes until either side closes, goes quiet for the idle timeout, or fails."""

        async def up() -> None:
            while chunk := await reader.read(_CHUNK):
                if forward_client:
                    await upstream.write(chunk)

        async def down() -> None:
            while chunk := await upstream.read(_CHUNK, timeout=self._idle_timeout):
                writer.write(chunk)
                await writer.drain()

        tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await upstream.aclose()
