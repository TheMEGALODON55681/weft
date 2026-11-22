import importlib.util
import socket

import pytest

from fixture_site import FixtureSite
from weft.fetch.errors import FetchError


async def test_plain_fetch(site: FixtureSite, fetch_with) -> None:
    r = await fetch_with().fetch(site.url("/article"))
    assert r.status == 200
    assert r.final_url == site.url("/article")
    assert r.content_type == "text/html"
    assert r.charset == "utf-8"
    assert b"Pricing and plans" in r.body
    assert r.redirects == []
    assert not r.truncated


async def test_follows_redirect_chain_and_records_hops(site: FixtureSite, fetch_with) -> None:
    r = await fetch_with().fetch(site.url("/chain/1"))
    assert r.status == 200
    assert r.final_url == site.url("/article")
    assert r.redirects == [site.url("/chain/1"), site.url("/chain/2")]


async def test_redirect_loop_is_reported(site: FixtureSite, fetch_with) -> None:
    with pytest.raises(FetchError) as e:
        await fetch_with().fetch(site.url("/loop/a"))
    assert e.value.code == "redirect_loop"


async def test_redirect_chain_is_capped(site: FixtureSite, fetch_with) -> None:
    with pytest.raises(FetchError) as e:
        await fetch_with(max_redirects=3).fetch(site.url("/endless/0"))
    assert e.value.code == "too_many_redirects"


async def test_redirect_to_non_http_scheme_is_refused(site: FixtureSite, fetch_with) -> None:
    with pytest.raises(FetchError) as e:
        await fetch_with().fetch(site.url("/redirect-to-file"))
    assert e.value.code == "invalid_url"


async def test_a_redirect_to_a_host_idna_rejects_is_an_invalid_url(
    site: FixtureSite, fetch_with
) -> None:
    with pytest.raises(FetchError) as e:
        await fetch_with().fetch(site.url("/goto?u=http%3A%2F%2Fxn--%2F"))
    assert e.value.code == "invalid_url"


async def test_3xx_without_location_is_returned_as_is(site: FixtureSite, fetch_with) -> None:
    r = await fetch_with().fetch(site.url("/redirect-no-location"))
    assert r.status == 302
    assert r.redirects == []


async def test_body_is_capped_and_flagged(site: FixtureSite, fetch_with) -> None:
    r = await fetch_with(max_body_bytes=1000).fetch(site.url("/big"))
    assert r.truncated
    assert len(r.body) == 1000


async def test_gzip_bomb_is_capped_after_decompression(site: FixtureSite, fetch_with) -> None:
    r = await fetch_with(max_body_bytes=10_000).fetch(site.url("/bomb"))
    assert r.truncated
    assert len(r.body) == 10_000


async def test_gzip_is_decoded(site: FixtureSite, fetch_with) -> None:
    r = await fetch_with().fetch(site.url("/gzip"))
    assert b"Pricing and plans" in r.body


async def test_error_statuses_are_results_not_exceptions(site: FixtureSite, fetch_with) -> None:
    assert (await fetch_with().fetch(site.url("/status/404"))).status == 404
    assert (await fetch_with().fetch(site.url("/status/500"))).status == 500


async def test_non_html_is_fetched_and_labelled(site: FixtureSite, fetch_with) -> None:
    r = await fetch_with().fetch(site.url("/pdf"))
    assert r.content_type == "application/pdf"


async def test_read_timeout(site: FixtureSite, fetch_with) -> None:
    with pytest.raises(FetchError) as e:
        await fetch_with(read_timeout_s=0.3).fetch(site.url("/slow"))
    assert e.value.code == "timeout"


async def test_total_timeout_catches_a_server_that_never_goes_idle(
    site: FixtureSite, fetch_with
) -> None:
    with pytest.raises(FetchError) as e:
        await fetch_with(total_timeout_s=0.5).fetch(site.url("/drip"))
    assert e.value.code == "timeout"


async def test_connection_refused_is_a_network_error(fetch_with) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(FetchError) as e:
        # No retries: this is about the error code, and Windows takes ~2 s to refuse a connection.
        await fetch_with(max_retries=0).fetch(f"http://127.0.0.1:{port}/")
    assert e.value.code == "network_error"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "ftp://example.test/x",
        "file:///etc/passwd",
        "http:///nohost",
        "http://user:pw@example.test/",
        "http://example.test/" + "a" * 9000,
        "http://xn--/",  # idna cannot decode these host names, and says so with its own error
        "http://xn--a/",
    ],
)
async def test_bad_urls_are_rejected_before_any_request(url: str, fetch_with) -> None:
    with pytest.raises(FetchError) as e:
        await fetch_with().fetch(url)
    assert e.value.code == "invalid_url"


async def test_user_agent_carries_contact(site: FixtureSite, fetch_with) -> None:
    r = await fetch_with(contact="ops@example.org").fetch(site.url("/ua"))
    assert b"weft/" in r.body
    assert b"contact: ops@example.org" in r.body


async def test_cookies_never_persist_between_fetches(site: FixtureSite, fetch_with) -> None:
    fetcher = fetch_with()
    await fetcher.fetch(site.url("/set-cookie"))
    r = await fetcher.fetch(site.url("/echo-cookie"))
    assert r.body == b""


async def test_one_hop_mode_returns_the_redirect_with_its_target(
    site: FixtureSite, fetch_with
) -> None:
    before = site.hits("/article")
    r = await fetch_with().fetch(site.url("/chain/1"), follow_redirects=False)
    assert r.status == 301
    assert r.location == site.url("/chain/2")  # made absolute
    assert r.redirects == []
    assert site.hits("/article") == before  # nothing past the first hop was requested


async def test_one_hop_mode_still_checks_robots(site: FixtureSite, fetch_with) -> None:
    with pytest.raises(FetchError) as e:
        await fetch_with().fetch(site.url("/private/secret"), follow_redirects=False)
    assert e.value.code == "robots_disallowed"


def test_sniffio_is_installed_because_httpcore_looks_for_it_on_every_call() -> None:
    """httpcore does `import sniffio` inside a function that runs several times per request. When
    the package is missing, each call searches sys.path for it again: the load test measured 40 to
    50 fetches a second without it and 200 to 240 with it. See the dependency note in
    pyproject.toml."""
    assert importlib.util.find_spec("sniffio") is not None


def test_cached_robots_rules_for_a_host_idna_rejects_is_none(fetch_with) -> None:
    # The crawl asks this for every link it finds. `xn--` is made of letters, so it canonicalises,
    # and idna's error used to come out of this call and stop the job.
    assert fetch_with().cached_robots_rules("http://xn--/page") is None
