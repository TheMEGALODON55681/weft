import socket
import sys
import time
from collections.abc import Callable
from ipaddress import ip_address
from typing import Any, cast

import httpcore
import pytest

from fixture_site import FixtureSite
from weft.fetch.errors import FetchError
from weft.fetch.http import Fetcher
from weft.fetch.ssrf import GuardedBackend, is_blocked

# --- the address table ---------------------------------------------------------------------

BLOCKED = [
    "127.0.0.1",
    "127.255.255.254",
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",  # AWS, GCP, DigitalOcean metadata
    "100.64.0.1",  # carrier-grade NAT
    "100.100.100.200",  # Alibaba metadata
    "0.0.0.0",
    "224.0.0.1",  # multicast: ipaddress calls it global
    "240.0.0.1",
    "255.255.255.255",
    "192.0.0.192",  # Oracle metadata
    "168.63.129.16",  # Azure wireserver: public-looking, ipaddress calls it global
    "::1",
    "::",
    "fe80::1",
    "fc00::1",
    "fd00:ec2::254",  # AWS IPv6 metadata
    "ff02::1",
    "2001:db8::1",
    "2001::1",  # Teredo
    "2002:808:808::",  # 6to4
    "::ffff:127.0.0.1",  # IPv4-mapped forms of private addresses
    "::ffff:7f00:1",
    "::ffff:10.0.0.1",
    "::ffff:169.254.169.254",
    "64:ff9b::a00:1",  # NAT64 wrapping 10.0.0.1
    "64:ff9b:1::1",
    "::a00:1",
]
PUBLIC = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700::1111", "::ffff:8.8.8.8"]


@pytest.mark.parametrize("addr", BLOCKED)
def test_non_public_addresses_are_blocked(addr: str) -> None:
    assert is_blocked(ip_address(addr))


@pytest.mark.parametrize("addr", PUBLIC)
def test_public_addresses_are_allowed(addr: str) -> None:
    assert not is_blocked(ip_address(addr))


def test_nat64_wrapping_a_public_address_is_allowed() -> None:
    # IPv6-only networks with DNS64 hand out these for every IPv4-only site.
    assert not is_blocked(ip_address("64:ff9b::808:808"))


@pytest.mark.parametrize("addr", ["127.0.0.1", "127.9.9.9", "::1", "::ffff:127.0.0.1"])
def test_allow_loopback_opens_loopback_only(addr: str) -> None:
    assert is_blocked(ip_address(addr))
    assert not is_blocked(ip_address(addr), allow_loopback=True)


@pytest.mark.parametrize("addr", ["10.0.0.1", "169.254.169.254", "fd00:ec2::254", "0.0.0.0"])
def test_allow_loopback_does_not_open_anything_else(addr: str) -> None:
    assert is_blocked(ip_address(addr), allow_loopback=True)


# --- the connect-time backend --------------------------------------------------------------

STREAM = cast(httpcore.AsyncNetworkStream, object())


class FakeInner(httpcore.AsyncNetworkBackend):
    """Records the hosts it is asked to connect to; `refuse` lists addresses that fail."""

    def __init__(self, refuse: tuple[str, ...] = ()) -> None:
        self.connected: list[str] = []
        self._refuse = refuse

    async def connect_tcp(self, host: str, port: int, **_: Any) -> httpcore.AsyncNetworkStream:
        self.connected.append(host)
        if host in self._refuse:
            raise httpcore.ConnectError(f"refused {host}")
        return STREAM

    async def connect_unix_socket(self, path: str, **_: Any) -> httpcore.AsyncNetworkStream:
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        pass


def resolver(*answers: list[str]) -> Callable[[str, int], Any]:
    """Each lookup returns the next answer, the way a rebinding DNS server would."""
    pending = list(answers)

    async def resolve(host: str, port: int) -> list[str]:
        return pending.pop(0)

    return resolve


async def test_connects_to_the_address_it_checked_not_the_name() -> None:
    inner = FakeInner()
    backend = GuardedBackend(inner, allow_loopback=False, resolve=resolver(["93.184.216.34"]))
    assert await backend.connect_tcp("example.test", 443) is STREAM
    assert inner.connected == ["93.184.216.34"]  # a second lookup inside inner cannot happen


async def test_dns_rebinding_between_connections_is_caught() -> None:
    inner = FakeInner()
    backend = GuardedBackend(
        inner, allow_loopback=False, resolve=resolver(["93.184.216.34"], ["127.0.0.1"])
    )
    await backend.connect_tcp("rebind.test", 80)  # first answer is public: fine
    with pytest.raises(FetchError) as e:
        await backend.connect_tcp("rebind.test", 80)  # the name now points inside
    assert e.value.code == "blocked_address"
    assert inner.connected == ["93.184.216.34"]


async def test_one_bad_address_among_good_ones_refuses_the_name() -> None:
    inner = FakeInner()
    backend = GuardedBackend(
        inner, allow_loopback=False, resolve=resolver(["93.184.216.34", "10.0.0.5"])
    )
    with pytest.raises(FetchError) as e:
        await backend.connect_tcp("mixed.test", 80)
    assert e.value.code == "blocked_address"
    assert inner.connected == []


async def test_the_refusal_does_not_reveal_where_the_name_points() -> None:
    backend = GuardedBackend(FakeInner(), allow_loopback=False, resolve=resolver(["10.1.2.3"]))
    with pytest.raises(FetchError) as e:
        await backend.connect_tcp("internal.test", 80)
    assert "10.1.2.3" not in str(e.value)


async def test_falls_back_to_the_next_address_when_one_refuses() -> None:
    inner = FakeInner(refuse=("2606:4700::1111",))
    backend = GuardedBackend(
        inner, allow_loopback=False, resolve=resolver(["2606:4700::1111", "93.184.216.34"])
    )
    assert await backend.connect_tcp("dual.test", 80) is STREAM
    assert inner.connected == ["2606:4700::1111", "93.184.216.34"]


async def test_every_address_refusing_raises_a_connect_error() -> None:
    inner = FakeInner(refuse=("93.184.216.34",))
    backend = GuardedBackend(inner, allow_loopback=False, resolve=resolver(["93.184.216.34"]))
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("down.test", 80)


def failing_resolver(exc: OSError) -> Callable[[str, int], Any]:
    async def resolve(host: str, port: int) -> list[str]:
        raise exc

    return resolve


async def test_a_name_that_does_not_exist_fails_for_good() -> None:
    # A plain FetchError, not a transport error: the retry loop only retries the latter, and
    # a mistyped host must not cost three lookups and two backoffs.
    gone = failing_resolver(socket.gaierror(socket.EAI_NONAME, "no such host"))
    backend = GuardedBackend(FakeInner(), allow_loopback=False, resolve=gone)
    with pytest.raises(FetchError) as e:
        await backend.connect_tcp("missing.test", 80)
    assert e.value.code == "network_error"


async def test_a_temporary_lookup_failure_is_left_to_the_retry_loop() -> None:
    flaky = failing_resolver(socket.gaierror(socket.EAI_AGAIN, "try again"))
    backend = GuardedBackend(FakeInner(), allow_loopback=False, resolve=flaky)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("slow-dns.test", 80)


async def test_any_other_resolver_failure_is_a_connect_error_not_a_raw_oserror() -> None:
    backend = GuardedBackend(
        FakeInner(), allow_loopback=False, resolve=failing_resolver(OSError("boom"))
    )
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("odd.test", 80)


# --- through the real Fetcher --------------------------------------------------------------

NOT_PUBLIC_URLS = [
    "http://127.0.0.1:1/",
    "http://localhost:1/",
    "http://[::1]:1/",
    "http://[::ffff:127.0.0.1]:1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::ffff:a00:1]/",
    "http://[64:ff9b::a00:1]/",
]


@pytest.mark.parametrize("url", NOT_PUBLIC_URLS)
async def test_non_public_targets_are_refused(url: str, fetch_with: Callable[..., Fetcher]) -> None:
    fetcher = fetch_with(allow_loopback=False, connect_timeout_s=1, total_timeout_s=3)
    with pytest.raises(FetchError) as e:
        await fetcher.fetch(url)
    assert e.value.code == "blocked_address"


@pytest.mark.parametrize(
    "host",
    [
        "2130706433",  # 127.0.0.1 as one decimal number
        "0x7f000001",  # the same in hex
        "127.1",  # the short form
        "0",  # "this host"; some stacks connect it to localhost
    ],
)
async def test_numeric_host_spellings_never_get_through(
    host: str, fetch_with: Callable[..., Fetcher]
) -> None:
    # glibc and macOS resolve these to 127.0.0.1 and the guard must refuse that. Windows refuses
    # to resolve them at all, so nothing can connect and the failure is a plain network error.
    # Windows takes about 3 s to give up, hence the generous total timeout.
    expected = "network_error" if sys.platform == "win32" else "blocked_address"
    fetcher = fetch_with(allow_loopback=False, connect_timeout_s=1, total_timeout_s=15)
    with pytest.raises(FetchError) as e:
        await fetcher.fetch(f"http://{host}:1/")
    assert e.value.code == expected


async def test_the_default_refuses_loopback_and_sends_nothing(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    with pytest.raises(FetchError) as e:
        await fetch_with(allow_loopback=False).fetch(site.url("/article"))
    assert e.value.code == "blocked_address"
    assert site.hits("/article") == 0
    assert site.hits("/robots.txt") == 0


async def test_a_refused_address_is_not_retried(fetch_with: Callable[..., Fetcher]) -> None:
    fetcher = fetch_with(allow_loopback=False, max_retries=2, retry_backoff_s=5)
    start = time.perf_counter()
    with pytest.raises(FetchError):
        await fetcher.fetch("http://10.0.0.1/")
    assert time.perf_counter() - start < 2  # a retry would have slept at least 2.5 s


async def test_allow_loopback_lets_the_fixture_site_through(
    site: FixtureSite, fetch_with: Callable[..., Fetcher]
) -> None:
    assert (await fetch_with(allow_loopback=True).fetch(site.url("/article"))).status == 200


@pytest.mark.parametrize(
    "path", ["/redirect-to-metadata", "/redirect-to-private", "/redirect-to-mapped"]
)
async def test_a_redirect_into_a_non_public_address_is_refused(
    path: str, site: FixtureSite, fetch_with: Callable[..., Fetcher]
) -> None:
    # Loopback is allowed so the first hop succeeds; the second hop must be checked on its own.
    fetcher = fetch_with(allow_loopback=True, connect_timeout_s=1, total_timeout_s=3)
    with pytest.raises(FetchError) as e:
        await fetcher.fetch(site.url(path))
    assert e.value.code == "blocked_address"


async def test_a_hostname_is_resolved_and_checked(
    site: FixtureSite, fetch_with: Callable[..., Fetcher]
) -> None:
    url = site.url("/article").replace("127.0.0.1", "localhost")
    with pytest.raises(FetchError) as e:
        await fetch_with(allow_loopback=False).fetch(url)
    assert e.value.code == "blocked_address"
    # Allowed, it must still connect: localhost may list ::1 first, where nothing listens.
    assert (await fetch_with(allow_loopback=True).fetch(url)).status == 200


async def test_proxy_environment_variables_are_ignored(
    site: FixtureSite, fetch_with: Callable[..., Fetcher], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy resolves names itself, so honouring one would route around the address check."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")  # nothing listens on the discard port
    assert (await fetch_with().fetch(site.url("/article"))).status == 200
