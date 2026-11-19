"""One URL in, one cited page out: fetch, decode, extract, apply the output caps."""

import asyncio
from typing import Literal

from weft.config import Settings
from weft.extract.content import extract_page
from weft.extract.decode import decode_body
from weft.fetch.http import Fetcher, FetchError
from weft.models import PageResult

RenderMode = Literal["auto", "never", "always"]
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})


async def get_page(
    fetcher: Fetcher, settings: Settings, url: str, render: RenderMode = "auto"
) -> PageResult:
    if render == "always":
        # Browser rendering arrives with the Playwright pool; until then say so plainly
        # instead of quietly returning an unrendered page.
        raise FetchError("render_unavailable", "browser rendering is not available in this build")
    res = await fetcher.fetch(url)
    # An absent Content-Type is treated as HTML, as browsers do.
    if res.content_type and res.content_type not in _HTML_TYPES:
        raise FetchError(
            "unsupported_content_type",
            f"{res.content_type} (HTTP {res.status}) is not HTML",
        )
    # Decoding and parsing are CPU-bound; keep them off the event loop.
    extracted = await asyncio.to_thread(
        lambda: extract_page(decode_body(res.body, res.charset), res.final_url)
    )
    markdown = extracted.markdown[: settings.max_content_chars]
    return PageResult(
        url=res.url,
        final_url=res.final_url,
        status=res.status,
        fetch_method="http",
        content_type=res.content_type,
        title=extracted.title,
        markdown=markdown,
        metadata=extracted.metadata,
        links=extracted.links[: settings.max_links],
        links_total=len(extracted.links),
        body_truncated=res.truncated,
        content_truncated=len(markdown) < len(extracted.markdown),
        redirects=res.redirects,
        elapsed_ms=res.elapsed_ms,
    )
