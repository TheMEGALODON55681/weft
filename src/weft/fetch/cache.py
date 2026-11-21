"""The page cache: finished PageResults in SQLite, keyed by canonical URL and render mode.

Fresh entries are served without contacting the site. Stale entries with an ETag or a
Last-Modified are revalidated with a conditional request, and a 304 reuses the stored result, a
rendered one included, so an unchanged JavaScript page is not rendered again. Only 200 responses
are stored.

YAGNI note: expired rows are kept, because their validators still save a download. Nothing
deletes them. Add a sweep by `fetched_at` if the file grows too large.
"""

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from weft.db import Database
from weft.models import PageResult


@dataclass(frozen=True, slots=True)
class CacheEntry:
    result: PageResult
    etag: str | None
    last_modified: str | None
    fresh: bool

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

    async def get(self, key: str, render: str) -> CacheEntry | None:
        if not self._ttl:
            return None

        def read(c: sqlite3.Connection) -> tuple[str, str | None, str | None, float] | None:
            row = c.execute(
                "SELECT result, etag, last_modified, expires_at FROM pages "
                "WHERE url = ? AND render = ?",
                (key, render),
            ).fetchone()
            return None if row is None else (row[0], row[1], row[2], row[3])

        row = await self._db.run(read)
        if row is None:
            return None
        result, etag, last_modified, expires_at = row
        return CacheEntry(
            PageResult.model_validate_json(result), etag, last_modified, self._clock() < expires_at
        )

    async def put(
        self, key: str, render: str, result: PageResult, etag: str | None, last_modified: str | None
    ) -> None:
        if not self._ttl or result.status != 200:
            return
        now = self._clock()
        doc = result.model_dump_json()

        def write(c: sqlite3.Connection) -> None:
            with c:
                c.execute(
                    "INSERT OR REPLACE INTO pages(url, render, result, etag, last_modified, "
                    "fetched_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (key, render, doc, etag, last_modified, now, now + self._ttl),
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
