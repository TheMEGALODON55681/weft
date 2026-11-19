"""HTML to clean Markdown plus metadata and links."""

import logging
from dataclasses import dataclass

import trafilatura
from selectolax.lexbor import LexborHTMLParser

from weft.extract.links import extract_links, resolve_url
from weft.models import Link, PageMetadata
from weft.obs.logging import safe_url

log = logging.getLogger("weft.extract")


@dataclass(frozen=True, slots=True)
class Extracted:
    title: str | None
    markdown: str
    metadata: PageMetadata
    links: list[Link]


def _clean(value: str | None) -> str | None:
    value = " ".join((value or "").split())
    return value or None


def extract_page(html: str, base_url: str) -> Extracted:
    """Never raises: a page the parsers choke on comes back with empty content."""
    markdown = ""
    doc = None
    try:
        # Known cost: the page is parsed four times (trafilatura twice, selectolax twice, once
        # more in extract_links). Pass one tree around if profiling shows parsing dominates.
        markdown = (
            trafilatura.extract(
                html,
                url=base_url,
                output_format="markdown",
                include_tables=True,
                include_links=False,
                include_comments=False,
            )
            or ""
        ).strip()
        doc = trafilatura.extract_metadata(html, default_url=base_url)
    except Exception:
        log.warning("extraction failed", extra={"url": safe_url(base_url)}, exc_info=True)

    tree = LexborHTMLParser(html)
    title_node = tree.css_first("title")
    root = tree.css_first("html")
    return Extracted(
        title=_clean(doc.title if doc else None)
        or _clean(title_node.text() if title_node else None),
        markdown=markdown,
        metadata=PageMetadata(
            description=_clean(doc.description if doc else None),
            author=_clean(doc.author if doc else None),
            published=_clean(doc.date if doc else None),
            language=_clean(root.attributes.get("lang") if root else None),
            site_name=_clean(doc.sitename if doc else None),
            canonical_url=_canonical(tree, base_url),
        ),
        links=extract_links(html, base_url),
    )


def _canonical(tree: LexborHTMLParser, base_url: str) -> str | None:
    for link in tree.css("link[rel][href]"):
        if "canonical" in (link.attributes.get("rel") or "").lower().split():
            return resolve_url(base_url, link.attributes.get("href") or "")
    return None
