import pytest

from fixture_site import FixtureSite
from weft.config import Settings
from weft.fetch.http import Fetcher, FetchError
from weft.fetch.page import get_page


@pytest.fixture
async def page_of(site: FixtureSite):
    opened: list[Fetcher] = []

    async def go(path: str, render: str = "auto", **overrides: object):
        settings = Settings(**overrides)
        fetcher = Fetcher(settings)
        opened.append(fetcher)
        return await get_page(fetcher, settings, site.url(path), render)

    yield go
    for f in opened:
        await f.aclose()


async def test_article_end_to_end(site: FixtureSite, page_of) -> None:
    p = await page_of("/article")
    assert p.status == 200
    assert p.url == p.final_url == site.url("/article")
    assert p.fetch_method == "http"
    assert p.content_type == "text/html"
    assert p.title == "Pricing and plans"
    assert "## Team plan" in p.markdown
    assert p.metadata.canonical_url == site.url("/pricing")
    assert p.links_total == 4
    assert len(p.links) == 4
    assert not p.body_truncated
    assert not p.content_truncated
    assert p.elapsed_ms > 0


async def test_redirects_are_reported(site: FixtureSite, page_of) -> None:
    p = await page_of("/redirect")
    assert p.url == site.url("/redirect")
    assert p.final_url == site.url("/article")
    assert p.redirects == [site.url("/redirect")]


async def test_error_pages_are_returned_with_their_status(page_of) -> None:
    p = await page_of("/status/404")
    assert p.status == 404
    assert p.title == "Gone"


@pytest.mark.parametrize("path", ["/pdf", "/json"])
async def test_non_html_is_refused_with_a_clear_code(path: str, page_of) -> None:
    with pytest.raises(FetchError) as e:
        await page_of(path)
    assert e.value.code == "unsupported_content_type"


async def test_empty_body_is_an_empty_page_not_an_error(page_of) -> None:
    p = await page_of("/empty")
    assert p.status == 200
    assert p.markdown == ""
    assert p.links == []


async def test_declared_latin1_is_decoded(page_of) -> None:
    p = await page_of("/latin1")
    assert p.title == "Café"
    assert "crème brûlée" in p.markdown


async def test_meta_declared_cyrillic_is_decoded(page_of) -> None:
    p = await page_of("/meta-charset")
    assert p.title == "Привет"
    assert "Привет, мир" in p.markdown


async def test_byte_cap_is_surfaced(page_of) -> None:
    p = await page_of("/big", max_body_bytes=1000)
    assert p.body_truncated


async def test_link_cap_keeps_the_total(page_of) -> None:
    p = await page_of("/article", max_links=2)
    assert len(p.links) == 2
    assert p.links_total == 4


async def test_markdown_cap_is_surfaced(page_of) -> None:
    p = await page_of("/article", max_content_chars=100)
    assert len(p.markdown) == 100
    assert p.content_truncated


@pytest.mark.parametrize("mode", ["auto", "never"])
async def test_http_only_modes_fetch_over_http(mode: str, page_of) -> None:
    assert (await page_of("/article", render=mode)).fetch_method == "http"


async def test_forced_rendering_says_it_is_unavailable(page_of) -> None:
    with pytest.raises(FetchError) as e:
        await page_of("/article", render="always")
    assert e.value.code == "render_unavailable"
