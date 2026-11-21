"""The local proxy the browser is forced through. Every connection it opens goes through the
same GuardedBackend as plain HTTP, so these tests drive it with raw sockets and a recording
backend, no browser."""

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpcore
import pytest

from fixture_site import FixtureSite
from weft.fetch.proxy import GuardedProxy
from weft.fetch.ssrf import GuardedBackend


class Recording(httpcore.AsyncNetworkBackend):
    """A real backend that remembers every address it was asked to connect to."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, int]] = []
        self._real = httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, **kw: Any) -> httpcore.AsyncNetworkStream:
        self.asked.append((host, port))
        return await self._real.connect_tcp(host, port, **kw)

    async def connect_unix_socket(self, path: str, **_: Any) -> httpcore.AsyncNetworkStream:
        raise AssertionError("not used")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def answers(table: dict[str, list[list[str]]]) -> Callable[[str, int], Any]:
    """A resolver that gives each name its answers in turn; IP literals resolve to themselves."""

    async def resolve(host: str, port: int) -> list[str]:
        if host in table:
            queue = table[host]
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return [host]

    return resolve


@pytest.fixture
async def proxy_with() -> AsyncIterator[Callable[..., Any]]:
    opened: list[GuardedProxy] = []

    async def make(
        allow_loopback: bool, resolve: Callable[[str, int], Any] | None = None
    ) -> tuple[int, Recording]:
        inner = Recording()
        kw = {"resolve": resolve} if resolve else {}
        proxy = GuardedProxy(GuardedBackend(inner, allow_loopback, **kw), connect_timeout_s=5)
        opened.append(proxy)
        return await proxy.start(), inner

    yield make
    for p in opened:
        await p.aclose()


async def exchange(port: int, data: bytes) -> bytes:
    """Send `data` to the proxy and read until it closes the connection."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(data)
        await writer.drain()
        async with asyncio.timeout(10):
            return await reader.read(-1)
    finally:
        writer.close()


def get(host: str, path: str) -> bytes:
    return f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()


async def test_connect_tunnels_to_an_allowed_address(site: FixtureSite, proxy_with) -> None:
    port, inner = await proxy_with(allow_loopback=True)
    target = f"127.0.0.1:{site.port}"
    out = await exchange(
        port, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode() + get(target, "/ping")
    )
    assert out.startswith(b"HTTP/1.1 200")
    assert b"pong" in out
    assert inner.asked == [("127.0.0.1", site.port)]


async def test_connect_to_a_blocked_address_is_refused_before_connecting(
    site: FixtureSite, proxy_with
) -> None:
    port, inner = await proxy_with(allow_loopback=False)
    target = f"127.0.0.1:{site.port}"
    before = site.total_hits
    out = await exchange(
        port, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode() + get(target, "/ping")
    )
    assert out.startswith(b"HTTP/1.1 403")
    assert inner.asked == []
    assert site.total_hits == before


async def test_plain_http_is_forwarded_in_origin_form(site: FixtureSite, proxy_with) -> None:
    port, inner = await proxy_with(allow_loopback=True)
    target = f"127.0.0.1:{site.port}"
    request = (
        f"GET http://{target}/ua?x=1 HTTP/1.1\r\nHost: {target}\r\nUser-Agent: probe\r\n"
        "Proxy-Connection: keep-alive\r\n\r\n"
    ).encode()
    before = site.hits("/ua")
    out = await exchange(port, request)
    assert out.startswith(b"HTTP/1.0 200") or out.startswith(b"HTTP/1.1 200")
    assert out.endswith(b"probe")  # headers went through
    # The fixture counts by path, so an absolute URL in the request line would not count here.
    assert site.hits("/ua") == before + 1
    assert inner.asked == [("127.0.0.1", site.port)]


async def test_a_request_body_is_forwarded(site: FixtureSite, proxy_with) -> None:
    port, _ = await proxy_with(allow_loopback=True)
    target = f"127.0.0.1:{site.port}"
    out = await exchange(
        port,
        f"POST http://{target}/echo-body HTTP/1.1\r\nHost: {target}\r\n"
        "Content-Length: 5\r\n\r\nhello".encode(),
    )
    assert out.endswith(b"hello")


async def test_a_chunked_request_body_is_refused(proxy_with) -> None:
    """Without a length the proxy cannot tell where one request ends, so it does not guess."""
    port, inner = await proxy_with(allow_loopback=True)
    out = await exchange(
        port,
        b"POST http://127.0.0.1:1/ HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
    )
    assert out.startswith(b"HTTP/1.1 411")
    assert inner.asked == []


async def test_the_host_header_follows_the_url(site: FixtureSite, proxy_with) -> None:
    """The address was checked for the URL's host; the upstream is told that same host."""
    port, _ = await proxy_with(allow_loopback=True)
    target = f"127.0.0.1:{site.port}"
    out = await exchange(
        port, f"GET http://{target}/host HTTP/1.1\r\nHost: internal.example\r\n\r\n".encode()
    )
    assert out.endswith(target.encode())


@pytest.mark.parametrize(
    "target",
    ["10.0.0.1:80", "169.254.169.254:80", "[::1]:80", "[::ffff:a00:1]:80", "127.0.0.1:80"],
)
async def test_plain_http_to_a_blocked_address_is_refused(target: str, proxy_with) -> None:
    port, inner = await proxy_with(allow_loopback=False)
    out = await exchange(port, get(target, f"http://{target}/latest/meta-data/"))
    assert out.startswith(b"HTTP/1.1 403")
    assert inner.asked == []


async def test_a_name_is_checked_by_what_it_resolves_to(proxy_with) -> None:
    port, inner = await proxy_with(
        allow_loopback=True, resolve=answers({"metadata.test": [["169.254.169.254"]]})
    )
    out = await exchange(port, b"CONNECT metadata.test:443 HTTP/1.1\r\nHost: metadata.test\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 403")
    assert b"169.254" not in out  # the caller is not told where the name points
    assert inner.asked == []


async def test_dns_rebinding_between_connections_is_caught(site: FixtureSite, proxy_with) -> None:
    port, inner = await proxy_with(
        allow_loopback=True,
        resolve=answers({"flip.test": [["127.0.0.1"], ["10.0.0.1"]]}),
    )
    host = f"flip.test:{site.port}"
    first = await exchange(port, get(host, f"http://{host}/ping"))
    second = await exchange(port, get(host, f"http://{host}/ping"))
    assert b"pong" in first
    assert second.startswith(b"HTTP/1.1 403")
    assert inner.asked == [("127.0.0.1", site.port)]


async def test_one_request_per_connection(site: FixtureSite, proxy_with) -> None:
    """A second request on the same client connection must not ride the first one's upstream,
    which was checked for a different host."""
    port, inner = await proxy_with(allow_loopback=True)
    target = f"127.0.0.1:{site.port}"
    first = f"GET http://{target}/ping HTTP/1.1\r\nHost: {target}\r\n\r\n".encode()
    second = b"GET http://10.0.0.1/admin HTTP/1.1\r\nHost: 10.0.0.1\r\n\r\n"
    out = await exchange(port, first + second)
    assert b"pong" in out
    assert inner.asked == [("127.0.0.1", site.port)]
    # The forwarded request asked the upstream to close, so the second one went nowhere.
    assert site.hits("/admin") == 0


@pytest.mark.parametrize(
    "request_bytes",
    [
        b"nonsense\r\n\r\n",
        b"GET /relative HTTP/1.1\r\nHost: x\r\n\r\n",
        b"GET ftp://127.0.0.1/ HTTP/1.1\r\n\r\n",
        b"CONNECT nohostport HTTP/1.1\r\n\r\n",
        b"CONNECT 127.0.0.1:notaport HTTP/1.1\r\n\r\n",
    ],
)
async def test_malformed_requests_get_400(request_bytes: bytes, proxy_with) -> None:
    port, inner = await proxy_with(allow_loopback=True)
    out = await exchange(port, request_bytes)
    assert out.startswith(b"HTTP/1.1 400")
    assert inner.asked == []


async def test_an_oversized_request_head_is_refused(proxy_with) -> None:
    port, inner = await proxy_with(allow_loopback=True)
    out = await exchange(port, b"GET http://127.0.0.1/ HTTP/1.1\r\nX: " + b"a" * 100_000)
    assert out.startswith(b"HTTP/1.1 400") or out == b""
    assert inner.asked == []


async def test_an_unresolvable_name_is_a_502(proxy_with) -> None:
    async def nxdomain(host: str, port: int) -> list[str]:
        import socket

        raise socket.gaierror(socket.EAI_NONAME, "no such name")

    port, _ = await proxy_with(allow_loopback=True, resolve=nxdomain)
    out = await exchange(port, b"CONNECT nowhere.test:443 HTTP/1.1\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 502")


async def test_closing_the_proxy_ends_open_tunnels(site: FixtureSite, proxy_with) -> None:
    proxy = GuardedProxy(GuardedBackend(Recording(), True), connect_timeout_s=5)
    port = await proxy.start()
    target = f"127.0.0.1:{site.port}"
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
    await writer.drain()
    assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
    await proxy.aclose()
    async with asyncio.timeout(5):
        assert await reader.read(-1) == b""  # the tunnel was closed, not left hanging
    writer.close()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


async def test_closing_is_not_held_up_by_idle_connections() -> None:
    """Chromium preconnects sockets to its proxy and may send nothing on them. Closing must not
    wait for those, whatever point their handler has reached: not started yet, or waiting for a
    request head. A render's proxy is closed inside the render's time budget."""
    for yields in range(6):
        proxy = GuardedProxy(GuardedBackend(Recording(), True), connect_timeout_s=5)
        port = await proxy.start()
        idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(3)]
        try:
            for _ in range(yields):
                await asyncio.sleep(0)
            async with asyncio.timeout(2):
                await proxy.aclose()
        finally:
            for s in idle:
                s.close()
