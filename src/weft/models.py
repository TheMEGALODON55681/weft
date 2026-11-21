"""Shapes handed back to the calling agent. Every result carries the URL it came from."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

Pattern = Annotated[str, Field(min_length=1, max_length=500)]


class Link(BaseModel):
    url: str
    text: str
    nofollow: bool = False


class PageMetadata(BaseModel):
    description: str | None = None
    author: str | None = None
    published: str | None = None
    language: str | None = None
    site_name: str | None = None
    canonical_url: str | None = None


class PageResult(BaseModel):
    url: str  # as requested
    final_url: str  # after redirects; cite this one
    status: int
    fetch_method: Literal["http", "browser"]
    content_type: str
    title: str | None
    markdown: str
    metadata: PageMetadata
    links: list[Link]
    links_total: int  # before the max_links cap
    body_truncated: bool  # the response hit the byte cap
    content_truncated: bool  # the markdown hit the character cap
    redirects: list[str]  # URLs requested before final_url
    elapsed_ms: float
    body_bytes: int = 0  # bytes received for the document; 0 when served from the cache
    cached: bool = False  # served from the page cache, fresh or revalidated with a 304


JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]


class CrawlParams(BaseModel):
    """The crawl_site arguments, stored with the job so a resumed job runs the same crawl."""

    seed_urls: list[str]
    goal: str = ""
    max_pages: int = 50
    max_depth: int = 3
    include_patterns: list[Pattern] = []
    exclude_patterns: list[Pattern] = []
    same_domain_only: bool = True
    render: Literal["auto", "never", "always"] = "auto"


class JobStats(BaseModel):
    pages_fetched: int  # every URL taken from the frontier and finished, whatever the outcome
    pages_ok: int  # fetched, not a duplicate
    duplicates: int
    failed: int
    pending: int
    cached: int  # served from the page cache
    bytes: int
    errors: dict[str, int]  # failures by code
    latency_ms_p50: float | None
    latency_ms_p95: float | None
    latency_ms_max: float | None
    run_s: float  # running time, summed across resumes


class JobInfo(BaseModel):
    id: str
    kind: Literal["crawl"]
    status: JobStatus
    stop_reason: str | None  # max_pages | time_budget | exhausted, once done
    error: str | None
    params: CrawlParams
    stats: JobStats
    created_at: str
    updated_at: str
    finished_at: str | None


class CrawlPage(BaseModel):
    url: str  # canonical form of the URL that was crawled
    final_url: str  # cite this one
    title: str | None
    status: int
    depth: int
    relevance: float  # the page against the goal, 0 to 1
    snippet: str
    cached: bool


class JobResult(BaseModel):
    job: JobInfo
    pages: list[CrawlPage]  # most relevant first
    pages_total: int  # pages_ok, before limit and offset


class SiteMapEntry(BaseModel):
    url: str
    source: Literal["sitemap", "link"]
    lastmod: str | None = None
    allowed: bool | None  # robots.txt lets Weft fetch it; None when that origin's file is unread


class SitemapRead(BaseModel):
    url: str
    status: int | None
    urls: int  # entries found in this file
    error: str | None = None  # FetchError code, or "not_a_sitemap"


class SiteMap(BaseModel):
    url: str
    sitemaps: list[SitemapRead]
    urls: list[SiteMapEntry]  # same site as the start page, canonical, start page links first
    truncated: bool  # max_urls was reached, or sitemap files were left unread
