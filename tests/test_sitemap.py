import gzip
import time

import pytest

from fixture_site import DEFAULT_ROBOTS, FixtureSite
from weft.config import Settings
from weft.fetch.errors import FetchError
from weft.frontier.sitemap import _gunzip, map_site, parse_sitemap


def test_a_urlset_is_read_with_lastmod_and_entities() -> None:
    children, entries = parse_sitemap(
        b"<urlset><url><loc> https://a.test/x?a=1&amp;b=2 </loc><lastmod>2026-01-02</lastmod>"
        b"</url><url><loc>https://a.test/y</loc></url><url><lastmod>x</lastmod></url></urlset>"
    )
    assert children == []
    assert entries == [("https://a.test/x?a=1&b=2", "2026-01-02"), ("https://a.test/y", None)]


def test_an_index_lists_its_children() -> None:
    children, entries = parse_sitemap(
        b"<sitemapindex><sitemap><loc>https://a.test/1.xml</loc></sitemap>"
        b"<sitemap><loc>/2.xml</loc></sitemap></sitemapindex>"
    )
    assert children == ["https://a.test/1.xml", "/2.xml"]
    assert entries == []


def test_entities_are_not_expanded_beyond_the_basics() -> None:
    """No XML parser, so a billion-laughs file is just text with no <loc> in it."""
    bomb = b'<!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><urlset>&b;</urlset>'
    assert parse_sitemap(bomb) == ([], [])


@pytest.mark.parametrize(
    ("body", "children", "entries"),
    [
        # a <loc> with markup inside is not read, and neither is an empty one
        (b"<url><loc><![CDATA[https://a.test/c]]></loc></url>", [], []),
        (b"<url><loc></loc></url>", [], []),
        (b"<url><loc>  </loc><loc>https://a.test/second</loc></url>", [], []),
        # a stray <loc> before the real one does not hide it
        (b"<url><loc>x<loc>https://a.test/y</loc></url>", [], [("https://a.test/y", None)]),
        # an unclosed block is dropped, and the closed ones before it are kept
        (
            b"<url><loc>https://a.test/a</loc></url><url><loc>https://a.test/b</loc>",
            [],
            [("https://a.test/a", None)],
        ),
        # a block opened twice runs to the first close, as the pattern always did
        (b"<url><url><loc>https://a.test/n</loc></url>", [], [("https://a.test/n", None)]),
        # whitespace around a value is trimmed
        (
            b"<url><loc>https://a.test/t</loc><lastmod>\n 2026-01-02\t</lastmod></url>",
            [],
            [("https://a.test/t", "2026-01-02")],
        ),
        (b"<urlset><loc>https://a.test/outside</loc></urlset>", [], []),
    ],
)
def test_odd_shapes(
    body: bytes, children: list[str], entries: list[tuple[str, str | None]]
) -> None:
    assert parse_sitemap(body) == (children, entries)


@pytest.mark.parametrize(
    "body",
    [
        b"<url>" * 40_000,
        b"<sitemap>" * 25_000,
        b"<url><loc>" + b" " * 40_000 + b"x</url>",
        b"<url><loc>" + b" " * 20_000 + b"x" + b" " * 20_000 + b"</url>",
        b"<url><loc>" * 20_000 + b"</url>",
        b"<url><lastmod>" + b" " * 40_000 + b"x</url>",
    ],
    ids=[
        "url-never-closed",
        "sitemap-never-closed",
        "loc-spaces",
        "loc-both-sides",
        "loc-many",
        "lastmod",
    ],
)
def test_hostile_files_are_read_in_linear_time(body: bytes) -> None:
    """The pattern this replaced took 12 s on the first of these and 26 s on the fourth at a tenth
    of their size, and the parser runs on the event loop, so one such file stalled the server."""
    start = time.perf_counter()
    parse_sitemap(body)
    assert time.perf_counter() - start < 1


def test_gzip_is_unpacked_up_to_the_cap() -> None:
    assert _gunzip(gzip.compress(b"x" * 1000), 100) == b"x" * 100
    assert _gunzip(b"\x1f\x8bnot gzip", 100) == b""


async def test_the_default_sitemap_and_its_index_are_followed(make_site, fetch_with) -> None:
    site: FixtureSite = make_site()
    m = await map_site(fetch_with(), Settings(), site.url("/site/about"), 500)
    reads = {r.url.removeprefix(site.base): r for r in m.sitemaps}
    assert set(reads) == {
        "/sitemap.xml",
        "/sitemaps/pages.xml",
        "/sitemaps/more.xml.gz",
        "/sitemaps/missing.xml",
    }
    assert reads["/sitemaps/pages.xml"].urls == 5
    assert reads["/sitemaps/more.xml.gz"].urls == 1  # gzip, served as application/gzip
    assert reads["/sitemaps/missing.xml"].status == 404
    urls = {u.url.removeprefix(site.base): u for u in m.urls}
    assert urls["/site/about"].source == "link"  # the start page itself comes first
    assert urls["/site/pricing"].source == "sitemap"
    assert urls["/site/pricing"].lastmod == "2026-11-01"
    assert urls["/site/blog/1"].source == "sitemap"
    assert "/site/search?page=2&q=a" in urls  # unescaped and canonical
    assert urls["/private/listed"].allowed is False  # robots.txt disallows /private/
    assert urls["/site/pricing"].allowed is True
    assert not any("other.test" in u.url for u in m.urls)
    assert not m.truncated


async def test_sitemap_lines_in_robots_txt_are_used(make_site, fetch_with) -> None:
    site: FixtureSite = make_site(robots=DEFAULT_ROBOTS + "Sitemap: /sitemaps/more.xml.gz\n")
    m = await map_site(fetch_with(), Settings(), site.url("/site/"), 500)
    assert [r.url.removeprefix(site.base) for r in m.sitemaps] == ["/sitemaps/more.xml.gz"]
    assert site.hits("/sitemap.xml") == 0
    found = {u.url.removeprefix(site.base): u.source for u in m.urls}
    assert found["/site/blog/1"] == "sitemap"
    assert found["/site/pricing"] == "link"  # from the start page


async def test_a_missing_sitemap_still_maps_the_start_page(make_site, fetch_with) -> None:
    site: FixtureSite = make_site(robots=DEFAULT_ROBOTS + "Sitemap: /nope.xml\n")
    m = await map_site(fetch_with(), Settings(), site.url("/site/"), 500)
    assert m.sitemaps[0].status == 404
    assert any(u.url.endswith("/site/pricing") for u in m.urls)


async def test_a_page_that_is_not_a_sitemap_says_so(make_site, fetch_with) -> None:
    site: FixtureSite = make_site(robots=DEFAULT_ROBOTS + "Sitemap: /article\n")
    m = await map_site(fetch_with(), Settings(), site.url("/site/"), 500)
    assert m.sitemaps[0].error == "not_a_sitemap"


async def test_max_urls_truncates(make_site, fetch_with) -> None:
    site: FixtureSite = make_site()
    m = await map_site(fetch_with(), Settings(), site.url("/site/"), 3)
    assert len(m.urls) == 3
    assert m.truncated


async def test_a_blocked_start_page_raises(fetch_with) -> None:
    with pytest.raises(FetchError) as e:
        await map_site(fetch_with(allow_loopback=False), Settings(), "http://127.0.0.1:9/", 10)
    assert e.value.code == "blocked_address"
