import gzip

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
