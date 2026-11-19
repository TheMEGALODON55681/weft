"""Outgoing links: absolute, fragment-free, http(s) only, one entry per URL."""

from urllib.parse import urldefrag, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from weft.models import Link

MAX_ANCHOR_CHARS = 200


def extract_links(html: str, base_url: str) -> list[Link]:
    tree = LexborHTMLParser(html)
    base = base_url
    if (node := tree.css_first("base[href]")) is not None:
        base = resolve_url(base_url, node.attributes.get("href") or "") or base_url

    found: dict[str, Link] = {}
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        url = resolve_url(base, href)
        if url is None:
            continue
        text = " ".join(a.text(separator=" ").split())[:MAX_ANCHOR_CHARS]
        nofollow = "nofollow" in (a.attributes.get("rel") or "").lower().split()
        if url not in found:
            found[url] = Link(url=url, text=text, nofollow=nofollow)
        elif not found[url].text and text:  # logo link first, text link later
            found[url].text = text
    return list(found.values())


def resolve_url(base: str, href: str) -> str | None:
    try:
        url = urldefrag(urljoin(base, href.strip())).url
        parts = urlsplit(url)
    except ValueError:  # e.g. an unterminated IPv6 literal
        return None
    return url if parts.scheme in ("http", "https") and parts.netloc else None
