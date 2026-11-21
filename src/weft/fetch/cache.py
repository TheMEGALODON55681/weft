"""The page cache: finished PageResults in SQLite, keyed by canonical URL and render mode.

Fresh entries are served without contacting the site. Stale entries with an ETag or a
Last-Modified are revalidated with a conditional request, and a 304 reuses the stored result, a
rendered one included, so an unchanged JavaScript page is not rendered again. Only 200 responses
are stored.

Each row also holds the HTML the Markdown was extracted from (the rendered DOM, for a page that
went through the browser), compressed. Extraction rules need the document, and the Markdown has
lost its structure. It is read only when a caller asks for it.

YAGNI note: expired rows are kept, because their validators still save a download. Nothing
deletes them. Add a sweep by `fetched_at` if the file grows too large. The HTML makes rows about
ten times larger, so that sweep matters more than it did.
"""

import sqlite3
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass

from weft.db import Database
from weft.models import PageResult

_SELECT = (
    "SELECT result, etag, last_modified, expires_at, NULL FROM pages WHERE url = ? AND render = ?"
)
_SELECT_HTML = (
    "SELECT result, etag, last_modified, expires_at, html FROM pages WHERE url = ? AND render = ?"
)


@dataclass(frozen=True, slots=True)
class CacheEntry:
    result: PageResult
    etag: str | None
    last_modified: str | None
    fresh: bool
    html: str | None = None  # only when `get` was asked for it

    def validators(self) -> dict[str, str] | None:
        headers = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        return headers or None


class PageCache:
    def __init__(self, db: Database, ttl_s: float, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._ttl = ttl_s
        self._clock = clock

    async def get(self, key: str, render: str, with_html: bool = False) -> CacheEntry | None:
        if not self._ttl:
            return None

        def read(
            c: sqlite3.Connection,
        ) -> tuple[str, str | None, str | None, float, str | None] | None:
            row = c.execute(_SELECT_HTML if with_html else _SELECT, (key, render)).fetchone()
            if row is None:
                return None
            # Decompressed here, in the database thread, not on the event loop.
            html = None if row[4] is None else zlib.decompress(row[4]).decode()
            return (row[0], row[1], row[2], row[3], html)

        row = await self._db.run(read)
        if row is None:
            return None
        result, etag, last_modified, expires_at, html = row
        return CacheEntry(
            PageResult.model_validate_json(result),
            etag,
            last_modified,
            self._clock() < expires_at,
            html,
        )

    async def put(
        self,
        key: str,
        render: str,
        result: PageResult,
        etag: str | None,
        last_modified: str | None,
        html: str,
    ) -> None:
        if not self._ttl or result.status != 200:
            return
        now = self._clock()
        doc = result.model_dump_json()

        def write(c: sqlite3.Connection) -> None:
            blob = zlib.compress(html.encode(errors="replace"), 6)  # off the event loop, too
            with c:
                c.execute(
                    "INSERT OR REPLACE INTO pages(url, render, result, html, etag, last_modified, "
                    "fetched_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, render, doc, blob, etag, last_modified, now, now + self._ttl),
                )

        await self._db.run(write)

    async def refresh(self, key: str, render: str) -> None:
        """A 304 said the stored copy is still current: give it another TTL."""
        expires = self._clock() + self._ttl

        def write(c: sqlite3.Connection) -> None:
            with c:
                c.execute(
                    "UPDATE pages SET expires_at = ? WHERE url = ? AND render = ?",
                    (expires, key, render),
                )

        await self._db.run(write)
