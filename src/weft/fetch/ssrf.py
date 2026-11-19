"""SSRF guard. Names are resolved here, every address is checked, and the connection goes to one
of the addresses that was checked. The check and the connect share one code path, so a DNS answer
that changes in between (DNS rebinding) has nowhere to hide. It runs on every new connection, so
every redirect hop and every robots.txt request is covered without a separate check.
"""

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable, Iterable
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network

import httpcore
import httpx

from weft.fetch.errors import FetchError

log = logging.getLogger("weft.fetch")

_NAT64 = ip_network("64:ff9b::/96")
# ipaddress calls these global. The Azure wireserver serves instance secrets; Teredo and 6to4
# addresses carry an arbitrary IPv4 address inside them.
_ALSO_BLOCKED = tuple(ip_network(n) for n in ("168.63.129.16/32", "2001::/32", "2002::/16"))

Resolver = Callable[[str, int], Awaitable[list[str]]]


def is_blocked(ip: IPv4Address | IPv6Address, allow_loopback: bool = False) -> bool:
    """True for anything that is not a public unicast address."""
    if isinstance(ip, IPv6Address):
        # Judge the IPv4 address inside a mapped or NAT64 address. NAT64 is how IPv6-only
        # networks reach IPv4-only sites, so it cannot be blocked wholesale. The stdlib has
        # changed how it treats mapped addresses between releases, so do not depend on it.
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:
            ip = IPv4Address(ip.packed[-4:])
    if ip.is_loopback:
        return not allow_loopback
    return (
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or any(ip in net for net in _ALSO_BLOCKED)
    )


async def _system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Opens TCP connections only to public addresses, and only to ones it has just checked.

    One bad address in the answer refuses the whole name: the OS would be free to pick it.
    Addresses are tried in resolver order, one at a time.
    """

    def __init__(
        self,
        inner: httpcore.AsyncNetworkBackend,
        allow_loopback: bool,
        resolve: Resolver = _system_resolve,
    ) -> None:
        self._inner = inner
        self._allow_loopback = allow_loopback
        self._resolve = resolve

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 (the signature is httpcore's)
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            addrs = await self._resolve(host, port)
        except socket.gaierror as exc:
            if exc.errno == socket.EAI_AGAIN:  # temporary: the retry loop may try again
                raise httpcore.ConnectError(f"cannot resolve {host}: {exc}") from None
            # The name does not exist, or the resolver gave up for good. Retrying cannot help,
            # and a mistyped host should not cost three lookups and two backoffs.
            raise FetchError("network_error", f"cannot resolve {host}") from None
        except OSError as exc:
            raise httpcore.ConnectError(f"cannot resolve {host}: {exc}") from None
        if not addrs:
            raise httpcore.ConnectError(f"no addresses for {host}")
        blocked = [a for a in addrs if is_blocked(ip_address(a), self._allow_loopback)]
        if blocked:
            # The address stays in the log; the caller only learns that the name is refused,
            # so a probe cannot map internal DNS.
            log.warning("refused non-public address", extra={"host": host, "addresses": blocked})
            raise FetchError("blocked_address", f"{host} resolves to a non-public address")

        async def connect(addr: str) -> httpcore.AsyncNetworkStream:
            return await self._inner.connect_tcp(
                addr,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )

        for addr in addrs[:-1]:
            try:
                return await connect(addr)
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                continue
        return await connect(addrs[-1])

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 (the signature is httpcore's)
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def guarded_transport(allow_loopback: bool, http2: bool) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(http2=http2)
    # httpx has no hook for how connections are opened; its pool takes a backend as an attribute.
    # The SSRF tests fail if this stops working, because they expect connections to be refused.
    pool = transport._pool
    pool._network_backend = GuardedBackend(pool._network_backend, allow_loopback)
    return transport
