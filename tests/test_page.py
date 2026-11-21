import asyncio
import threading
import time

import pytest

from fixture_site import FixtureSite
from weft.config import Settings
from weft.fetch.browser import BrowserPool
from weft.fetch.errors import FetchError
from weft.fetch.http import Fetcher
from weft.fetch.page import get_page, wants_render


@pytest.fixture
async def page_of(site: FixtureSite):
    """Fetch a fixture path through get_page. `go.pools` holds the browser pool of each call."""
    opened: list[tuple[BrowserPool, Fetcher]] = []

    async def go(path: str, render: str = "auto", **overrides: object):
        settings = Settings(**overrides)
        fetcher = Fetcher(settings)
        pool = BrowserPool(settings, fetcher)
        opened.append((pool, fetcher))
        return await get_page(fetcher, settings, site.url(path), render, browser=pool)

    go.pools = opened  # type: ignore[attr-defined]
    yield go
    for pool, fetcher in opened:
        await pool.aclose()
        await fetcher.aclose()


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


async def test_extraction_that_overruns_is_cut_off(
    site: FixtureSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def hung(*_args: object) -> None:
        release.wait(10)

    monkeypatch.setattr("weft.fetch.page.extract_page", hung)
    settings = Settings(extract_timeout_s=0.2)
    fetcher = Fetcher(settings)
    try:
        start = time.perf_counter()
        with pytest.raises(FetchError) as e:
            await get_page(fetcher, settings, site.url("/article"))
        elapsed = time.perf_counter() - start
    finally:
        release.set()  # the thread cannot be cancelled, only let go; do not leave it behind
        await fetcher.aclose()
    assert e.value.code == "extraction_timeout"
    assert elapsed < 1.0  # the caller is freed while the thread is still stuck


async def test_extraction_runs_in_a_small_pool_of_its_own(
    site: FixtureSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock, running, peak = threading.Lock(), 0, 0

    def slow(*_args: object) -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.4)
        with lock:
            running -= 1
        raise ValueError  # any outcome will do; only the overlap matters

    monkeypatch.setattr("weft.fetch.page.extract_page", slow)
    settings = Settings(host_concurrency=8)
    fetcher = Fetcher(settings)
    try:
        await asyncio.gather(
            *(get_page(fetcher, settings, site.url("/article")) for _ in range(8)),
            return_exceptions=True,
        )
    finally:
        await fetcher.aclose()
    # At most four at once, however many pages are waiting. Not exactly four: a thread released
    # by an earlier test can still be winding down.
    assert 2 <= peak <= 4


async def test_auto_leaves_a_full_page_alone_and_starts_no_browser(page_of) -> None:
    p = await page_of("/article", render="auto")
    assert p.fetch_method == "http"
    assert page_of.pools[0][0].launches == 0


async def test_auto_renders_a_javascript_shell(page_of, chromium) -> None:
    p = await page_of("/js-shell", render="auto")
    assert p.fetch_method == "browser"
    assert "Rendered report" in p.markdown
    assert p.final_url.endswith("/js-shell")


async def test_never_returns_the_shell_as_fetched(page_of) -> None:
    p = await page_of("/js-shell", render="never")
    assert p.fetch_method == "http"
    assert "Rendered report" not in p.markdown
    assert page_of.pools[0][0].launches == 0


async def test_always_renders_even_a_full_page(page_of, chromium) -> None:
    p = await page_of("/article", render="always")
    assert p.fetch_method == "browser"
    assert "## Team plan" in p.markdown


async def test_a_short_page_without_scripts_is_not_rendered(page_of) -> None:
    """Thin but static: a browser would show the same words, so auto does not start one."""
    p = await page_of("/ping", render="auto")
    assert p.fetch_method == "http"
    assert page_of.pools[0][0].launches == 0


async def test_always_without_a_browser_says_so(
    page_of, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    with pytest.raises(FetchError) as e:
        await page_of("/article", render="always")
    assert e.value.code == "render_unavailable"


async def test_auto_falls_back_to_plain_http_without_a_browser(
    page_of, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    p = await page_of("/js-shell", render="auto")
    assert p.fetch_method == "http"


async def test_always_still_refuses_non_html(page_of) -> None:
    with pytest.raises(FetchError) as e:
        await page_of("/pdf", render="always")
    assert e.value.code == "unsupported_content_type"
    assert page_of.pools[0][0].launches == 0


LONG = " ".join(["word"] * 60)


@pytest.mark.parametrize(
    ("html", "markdown", "expected"),
    [
        ("<div id=root></div><script>x()</script>", "", True),  # thin, with a script
        ("<p>short</p>", "short", False),  # thin, but nothing would run
        (f"<p>{LONG}</p><script>x()</script>", LONG, False),  # full page with scripts
        (
            f"<p>{LONG}</p><script>x()</script><noscript>Please enable JavaScript</noscript>",
            LONG,
            True,
        ),
        (f"<p>{LONG}</p><noscript>Enable JavaScript</noscript>", LONG, False),  # nothing to run
    ],
)
def test_render_policy(html: str, markdown: str, expected: bool) -> None:
    assert wants_render(html, markdown, min_words=50) is expected
