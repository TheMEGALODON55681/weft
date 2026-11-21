"""One URL in, one cited page out: fetch, decode, extract, render if needed, apply output caps.

The page is always fetched over plain HTTP first, so robots.txt, the address guard and every
redirect hop are handled once, by the Fetcher. The browser, when it is used, renders that body.
With a cache, a fresh entry skips all of that, and a stale one is revalidated first.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

from selectolax.lexbor import LexborHTMLParser

from weft.config import Settings
from weft.extract.content import Extracted, extract_page
from weft.extract.decode import decode_body
from weft.fetch.browser import BrowserPool
from weft.fetch.cache import CacheEntry, PageCache
from weft.fetch.errors import FetchError
from weft.fetch.http import Fetcher, FetchResult
from weft.frontier.canon import canonicalize
from weft.models import PageMetadata, PageResult
from weft.obs.logging import safe_url

log = logging.getLogger("weft.fetch")

RenderMode = Literal["auto", "never", "always"]
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})

# A thread cannot be interrupted, so a timeout frees the caller and leaves the thread to finish.
# Extraction gets a pool of its own so that stuck pages cannot use up the default executor,
# which also runs DNS lookups. Once all four are stuck, extraction fails fast until they finish.
# YAGNI note: if that is not enough, run extraction in a child process that can be killed.
_EXTRACT_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="weft-extract")


def wants_render(html: str, markdown: str, min_words: int) -> bool:
    """A browser can only add content a script would write. So: the page has a script, and its
    extracted text is thin, or a <noscript> says the page needs JavaScript."""
    tree = LexborHTMLParser(html)
    if tree.css_first("script") is None:
        return False
    if len(markdown.split()) < min_words:
        return True
    return any("javascript" in n.text().lower() for n in tree.css("noscript"))


def _read_http(res: FetchResult, min_words: int) -> tuple[str, Extracted, bool]:
    html = decode_body(res.body, res.charset)
    page = extract_page(html, res.final_url)
    return html, page, wants_render(html, page.markdown, min_words)


async def off_loop[T](settings: Settings, fn: Callable[[], T]) -> T:
    """Decoding, parsing and ranking are CPU-bound; keep them off the event loop, under a
    timeout."""
    try:
        async with asyncio.timeout(settings.extract_timeout_s):
            return await asyncio.get_running_loop().run_in_executor(_EXTRACT_POOL, fn)
    except TimeoutError:
        raise FetchError(
            "extraction_timeout",
            f"reading the page took longer than {settings.extract_timeout_s:g}s",
        ) from None


def _from_cache(entry: CacheEntry, url: str, start: float) -> PageResult:
    return entry.result.model_copy(
        update={
            "url": url.strip(),
            "cached": True,
            "body_bytes": 0,
            "elapsed_ms": (time.perf_counter() - start) * 1000,
        }
    )


@dataclass(frozen=True, slots=True)
class Document:
    """A page and, if it was asked for, the HTML its Markdown was extracted from: the rendered
    DOM when the browser was used, else the decoded body."""

    page: PageResult
    html: str | None


async def get_document(
    fetcher: Fetcher,
    settings: Settings,
    url: str,
    render: RenderMode = "auto",
    browser: BrowserPool | None = None,
    cache: PageCache | None = None,
    timeout_s: float | None = None,
    with_html: bool = False,
) -> Document:
    start = time.perf_counter()
    entry = key = None
    if cache is not None:
        try:
            key = canonicalize(url)
        except ValueError:
            pass  # the fetch below reports the bad URL
        else:
            entry = await cache.get(key, render, with_html)
            if entry is not None and entry.fresh:
                return Document(_from_cache(entry, url, start), entry.html)
    headers = entry.validators() if entry else None
    res = await fetcher.fetch(url, headers=headers, timeout_s=timeout_s)
    if res.status == 304 and entry is not None and cache is not None and key is not None:
        await cache.refresh(key, render)
        return Document(_from_cache(entry, url, start), entry.html)
    page, html = await _build(res, settings, render, browser, start)
    if cache is not None and key is not None:
        await cache.put(key, render, page, res.etag, res.last_modified, html)
    return Document(page, html if with_html else None)


async def get_page(
    fetcher: Fetcher,
    settings: Settings,
    url: str,
    render: RenderMode = "auto",
    browser: BrowserPool | None = None,
    cache: PageCache | None = None,
    timeout_s: float | None = None,
) -> PageResult:
    return (await get_document(fetcher, settings, url, render, browser, cache, timeout_s)).page


async def _build(
    res: FetchResult,
    settings: Settings,
    render: RenderMode,
    browser: BrowserPool | None,
    start: float,
) -> tuple[PageResult, str]:
    """The page, and the HTML it was extracted from (empty for an error page that is not HTML)."""
    # An absent Content-Type is treated as HTML, as browsers do.
    if res.content_type and res.content_type not in _HTML_TYPES:
        if res.status >= 400:  # an error page is a result whatever its type; there is no text
            empty = Extracted(None, "", PageMetadata(), [])
            return _result(res, empty, res.final_url, res.truncated, "http", start, settings), ""
        raise FetchError(
            "unsupported_content_type",
            f"{res.content_type} (HTTP {res.status}) is not HTML",
        )
    if render != "always":
        html, page, wanted = await off_loop(
            settings, lambda: _read_http(res, settings.render_min_words)
        )
        if render == "never" or not wanted:
            return _result(res, page, res.final_url, res.truncated, "http", start, settings), html
    try:
        if browser is None:
            raise FetchError("render_unavailable", "this server has no browser")
        shot = await browser.render(res)
    except FetchError as exc:
        if render == "always":
            raise
        # auto is best effort: the plain page is still an answer.
        log.info("render skipped", extra={"url": safe_url(res.final_url), "code": exc.code})
        return _result(res, page, res.final_url, res.truncated, "http", start, settings), html
    rendered = await off_loop(settings, lambda: extract_page(shot.html, shot.final_url))
    truncated = res.truncated or shot.truncated
    result = _result(res, rendered, shot.final_url, truncated, "browser", start, settings)
    return result, shot.html


def _result(
    res: FetchResult,
    page: Extracted,
    final_url: str,
    truncated: bool,
    method: Literal["http", "browser"],
    start: float,
    settings: Settings,
) -> PageResult:
    markdown = page.markdown[: settings.max_content_chars]
    return PageResult(
        url=res.url,
        final_url=final_url,
        status=res.status,
        fetch_method=method,
        content_type=res.content_type,
        title=page.title,
        markdown=markdown,
        metadata=page.metadata,
        links=page.links[: settings.max_links],
        links_total=len(page.links),
        body_truncated=truncated,
        content_truncated=len(markdown) < len(page.markdown),
        redirects=res.redirects,
        elapsed_ms=(time.perf_counter() - start) * 1000,
        body_bytes=len(res.body),
    )
