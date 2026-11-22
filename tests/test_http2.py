"""HTTP/2 through the guarded transport. The address guard replaces the network backend under
httpx's connection pool, and until now nothing showed that a connection opened that way still
negotiates `h2`, multiplexes and keeps every limit. `tests/h2_site.py` is a server that speaks
HTTP/2 over TLS and records the protocol each connection negotiated, so these tests can say what
really ran."""

import asyncio
import ssl
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path

import pytest

from certs import write_self_signed
from h2_site import BIG, H2Site, run_h2_site
from weft.fetch.errors import FetchError
from weft.fetch.http import Fetcher


@pytest.fixture
def h2_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], H2Site]]:
    """An HTTP/2 site whose certificate names `dns_name`, trusted through SSL_CERT_FILE."""
    with ExitStack() as stack:

        def start(dns_name: str = "localhost") -> H2Site:
            pem = tmp_path / f"{dns_name}.pem"
            write_self_signed(pem, dns_name)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(pem)
            monkeypatch.setenv("SSL_CERT_FILE", str(pem))
            return stack.enter_context(run_h2_site(context))

        yield start


async def test_a_fetch_negotiates_h2_and_one_connection_carries_robots_and_the_page(
    h2_site: Callable[[str], H2Site], fetch_with: Callable[..., Fetcher]
) -> None:
    site = h2_site("localhost")
    r = await fetch_with().fetch(site.url("/article"))
    assert r.status == 200
    assert b"Pricing and plans" in r.body
    assert site.protocols == ["h2"]  # one connection, for robots.txt and for the page
    assert site.requests == [("GET", "/robots.txt"), ("GET", "/article")]


async def test_with_http2_off_the_same_server_sees_http_1_1(
    h2_site: Callable[[str], H2Site], fetch_with: Callable[..., Fetcher]
) -> None:
    site = h2_site("localhost")
    r = await fetch_with(http2=False).fetch(site.url("/article"))
    assert r.status == 200
    assert b"Pricing and plans" in r.body
    assert set(site.protocols) == {"http/1.1"}


async def test_the_certificate_is_still_checked_against_the_name_over_h2(
    h2_site: Callable[[str], H2Site], fetch_with: Callable[..., Fetcher]
) -> None:
    site = h2_site("other.test")
    with pytest.raises(FetchError) as e:
        await fetch_with(max_retries=0).fetch(site.url("/article"))
    assert e.value.code == "network_error"
    assert "CERTIFICATE_VERIFY_FAILED" in str(e.value)
    assert site.requests == []


async def test_the_address_guard_refuses_before_any_connection_over_h2(
    h2_site: Callable[[str], H2Site], fetch_with: Callable[..., Fetcher]
) -> None:
    site = h2_site("localhost")
    with pytest.raises(FetchError) as e:
        await fetch_with(allow_loopback=False).fetch(site.url("/article"))
    assert e.value.code == "blocked_address"
    assert site.protocols == []  # the server never saw a connection


async def test_a_redirect_to_a_private_address_is_refused_over_h2(
    h2_site: Callable[[str], H2Site], fetch_with: Callable[..., Fetcher]
) -> None:
    site = h2_site("localhost")
    with pytest.raises(FetchError) as e:
        await fetch_with().fetch(site.url("/redirect-to-private"))
    assert e.value.code == "blocked_address"
    assert ("GET", "/redirect-to-private") in site.requests


async def test_redirects_are_followed_by_hand_over_h2(
    h2_site: Callable[[str], H2Site], fetch_with: Callable[..., Fetcher]
) -> None:
    site = h2_site("localhost")
    r = await fetch_with().fetch(site.url("/redirect"))
    assert r.status == 200
    assert r.final_url == site.url("/article")
    assert r.redirects == [site.url("/redirect")]
    assert set(site.protocols) == {"h2"}


async def test_a_body_larger_than_the_flow_control_window_arrives_whole(
    h2_site: Callable[[str], H2Site], fetch_with: Callable[..., Fetcher]
) -> None:
    site = h2_site("localhost")
    r = await fetch_with().fetch(site.url("/big"))
    assert r.body == BIG
    assert not r.truncated


async def test_the_body_cap_cuts_an_h2_stream_and_the_connection_survives(
    h2_site: Callable[[str], H2Site], fetch_with: Callable[..., Fetcher]
) -> None:
    site = h2_site("localhost")
    fetcher = fetch_with(max_body_bytes=50_000)
    r = await fetcher.fetch(site.url("/big"))
    assert r.truncated
    assert len(r.body) == 50_000
    assert (await fetcher.fetch(site.url("/article"))).status == 200  # the next stream works
    assert set(site.protocols) == {"h2"}


async def test_gzip_is_decoded_over_h2(
    h2_site: Callable[[str], H2Site], fetch_with: Callable[..., Fetcher]
) -> None:
    site = h2_site("localhost")
    r = await fetch_with().fetch(site.url("/gzip"))
    assert b"Pricing and plans" in r.body


async def test_parallel_fetches_share_h2_connections(
    h2_site: Callable[[str], H2Site], fetch_with: Callable[..., Fetcher]
) -> None:
    site = h2_site("localhost")
    fetcher = fetch_with(host_concurrency=8)
    results = await asyncio.gather(*(fetcher.fetch(site.url("/article")) for _ in range(8)))
    assert [r.status for r in results] == [200] * 8
    assert set(site.protocols) == {"h2"}
    assert len(site.protocols) < 8  # streams were multiplexed, not one connection each


async def test_a_reset_stream_is_a_network_error_and_the_next_request_works(
    h2_site: Callable[[str], H2Site], fetch_with: Callable[..., Fetcher]
) -> None:
    site = h2_site("localhost")
    site.reset_paths.add("/article")
    fetcher = fetch_with(max_retries=0)
    with pytest.raises(FetchError) as e:
        await fetcher.fetch(site.url("/article"))
    assert e.value.code == "network_error"
    site.reset_paths.clear()
    assert (await fetcher.fetch(site.url("/article"))).status == 200
