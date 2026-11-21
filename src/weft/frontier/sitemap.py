"""map_site: a URL inventory from sitemaps plus the links on the start page.

Sitemaps come from the `Sitemap:` lines of robots.txt, or `/sitemap.xml` when there are none.
Sitemap indexes are followed, gzip files are unpacked, and every file is fetched through the
Fetcher, so robots.txt, pacing and the address guard apply to each one.

The XML is read with a pattern match for `<loc>` and `<lastmod>`, not an XML parser. A sitemap is
untrusted input, and this way there are no entities to expand and no parser to attack. Sitemaps
that use a namespace prefix on their tags (`<s:loc>`) are not read.
"""

import html
import re
import zlib
from typing import Literal

import httpx

from weft.config import Settings
from weft.fetch.errors import FetchError
from weft.fetch.http import Fetcher, parse_http_url
from weft.fetch.page import get_page
from weft.fetch.robots import Rules
from weft.frontier.canon import canonicalize, site_key
from weft.models import SiteMap, SiteMapEntry, SitemapRead

MAX_SITEMAPS = 20  # files read per call, indexes included
_URL_BLOCK = re.compile(rb"<url>(.*?)</url>", re.S)
_SITEMAP_BLOCK = re.compile(rb"<sitemap>(.*?)</sitemap>", re.S)
_LOC = re.compile(rb"<loc>\s*([^<]*?)\s*</loc>")
_LASTMOD = re.compile(rb"<lastmod>\s*([^<]*?)\s*</lastmod>")


def _gunzip(body: bytes, limit: int) -> bytes:
    """Unpack gzip without letting a bomb past `limit` bytes."""
    d = zlib.decompressobj(wbits=31)
    try:
        return d.decompress(body, limit)
    except zlib.error:
        return b""


def _text(raw: bytes | None) -> str | None:
    return html.unescape(raw.decode("utf-8", "replace")) if raw else None


def parse_sitemap(body: bytes) -> tuple[list[str], list[tuple[str, str | None]]]:
    """(child sitemaps, (url, lastmod) entries). A file that is neither comes back empty."""
    children = [
        loc
        for block in _SITEMAP_BLOCK.findall(body)
        if (m := _LOC.search(block)) and (loc := _text(m.group(1)))
    ]
    entries = []
    for block in _URL_BLOCK.findall(body):
        m = _LOC.search(block)
        if m is None or not (url := _text(m.group(1))):
            continue
        mod = _LASTMOD.search(block)
        entries.append((url, _text(mod.group(1)) if mod else None))
    return children, entries


def _join(base: httpx.URL, ref: str) -> str | None:
    try:
        return str(base.join(ref.strip()))
    except httpx.InvalidURL:
        return None


def _path(url: str) -> str:
    raw = parse_http_url(url).raw_path
    return raw.decode("ascii", "replace")


async def map_site(fetcher: Fetcher, settings: Settings, url: str, max_urls: int) -> SiteMap:
    """Raises FetchError only if the start page itself cannot be fetched. Sitemap failures are
    reported per file. Reading stops once `max_urls` URLs are known."""
    page = await get_page(fetcher, settings, url, render="never")
    start = parse_http_url(page.final_url)
    site = site_key(start.host)
    rules: Rules | None
    try:
        rules = await fetcher.robots_rules(page.final_url)
    except FetchError:
        rules = None

    found: dict[str, SiteMapEntry] = {}

    truncated = False

    def add(raw: str, source: Literal["sitemap", "link"], lastmod: str | None = None) -> None:
        nonlocal truncated
        try:
            u = canonicalize(raw)
            same_site = site_key(parse_http_url(u).host) == site
        except (ValueError, FetchError):
            return
        if not same_site:
            return
        if u in found:  # a link found first; the sitemap may still know when it changed
            if lastmod and found[u].lastmod is None:
                found[u].lastmod = lastmod
            return
        if len(found) >= max_urls:
            truncated = True
            return
        # Each origin has its own robots.txt: www and the bare domain, http and https.
        own = fetcher.cached_robots_rules(u)
        allowed = own.allows(_path(u)) if own is not None else None
        found[u] = SiteMapEntry(url=u, source=source, lastmod=lastmod, allowed=allowed)

    add(page.final_url, "link")
    for link in page.links:
        add(link.url, "link")

    # Relative Sitemap lines are against the rules, and common. They are read from the site root.
    listed = rules.sitemaps if rules and rules.sitemaps else ("/sitemap.xml",)
    queue = [u for sm in listed if (u := _join(start, sm))]
    reads: list[SitemapRead] = []
    seen: set[str] = set()
    while queue and len(reads) < MAX_SITEMAPS and not truncated:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            res = await fetcher.fetch(sm)
        except FetchError as exc:
            reads.append(SitemapRead(url=sm, status=None, urls=0, error=exc.code))
            continue
        body = res.body
        if body[:2] == b"\x1f\x8b":
            body = _gunzip(body, settings.max_body_bytes)
        children, entries = parse_sitemap(body) if res.status == 200 else ([], [])
        base = parse_http_url(res.final_url)
        queue += [u for c in children if (u := _join(base, c))]
        for loc, lastmod in entries:
            add(loc, "sitemap", lastmod)
        error = None if children or entries or res.status != 200 else "not_a_sitemap"
        reads.append(SitemapRead(url=sm, status=res.status, urls=len(entries), error=error))

    truncated = truncated or bool(queue)  # files left unread
    return SiteMap(
        url=page.final_url, sitemaps=reads, urls=list(found.values()), truncated=truncated
    )
