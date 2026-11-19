"""Shapes handed back to the calling agent. Every result carries the URL it came from."""

from typing import Literal

from pydantic import BaseModel


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
