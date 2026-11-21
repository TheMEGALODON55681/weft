"""The crawl frontier: one job's URLs in SQLite, taken best score first.

Rows move pending -> fetching -> done | duplicate | failed. Every move commits on its own, so a
killed process leaves at most the rows that were `fetching` at the time, and `reopen` puts those
back to `pending`. The rest of a resumed crawl carries on from the table as it was.
"""

import sqlite3
import time
from dataclasses import dataclass

from weft.db import Database


@dataclass(frozen=True, slots=True)
class Item:
    url: str
    depth: int
    score: float
    run: int  # ancestors in a row that share this URL's pattern


@dataclass(frozen=True, slots=True)
class NewUrl:
    url: str
    depth: int
    score: float
    parent_url: str | None
    anchor_text: str | None
    run: int = 0


@dataclass(frozen=True, slots=True)
class Outcome:
    state: str  # done | duplicate | failed
    final_url: str | None = None
    status: int | None = None
    title: str | None = None
    snippet: str | None = None
    relevance: float | None = None
    content_hash: str | None = None
    simhash: int | None = None  # signed
    duplicate_of: str | None = None
    error: str | None = None
    bytes: int = 0
    elapsed_ms: float | None = None
    cached: bool = False


class Frontier:
    def __init__(self, db: Database, job_id: str) -> None:
        self._db = db
        self._job = job_id

    async def reopen(self) -> None:
        """Put back what a previous run had taken but not finished."""

        def work(c: sqlite3.Connection) -> None:
            with c:
                c.execute(
                    "UPDATE frontier SET state = 'pending' WHERE job_id = ? AND state = 'fetching'",
                    (self._job,),
                )

        await self._db.run(work)

    async def add(self, urls: list[NewUrl]) -> None:
        """Insert new URLs. A URL already known keeps its row. If it is still pending, a better
        link improves it: the higher score, the shallower depth, the shorter pattern run."""
        now = time.time()
        # A microsecond apart, so ties in score go to links in the order the page listed them.
        rows = [
            (self._job, u.url, u.depth, u.score, u.parent_url, u.anchor_text, u.run, now + i / 1e6)
            for i, u in enumerate(urls)
        ]

        def work(c: sqlite3.Connection) -> None:
            with c:
                c.executemany(
                    "INSERT INTO frontier(job_id, url, depth, score, parent_url, anchor_text, run, "
                    "state, discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?) "
                    "ON CONFLICT(job_id, url) DO UPDATE SET score = max(score, excluded.score), "
                    "depth = min(depth, excluded.depth), run = min(run, excluded.run) "
                    "WHERE state = 'pending'",
                    rows,
                )

        await self._db.run(work)

    async def claim(self) -> Item | None:
        """Take the best pending URL; ties go to the one found first."""

        def work(c: sqlite3.Connection) -> Item | None:
            with c:
                row = c.execute(
                    "SELECT url, depth, score, run FROM frontier "
                    "WHERE job_id = ? AND state = 'pending' "
                    "ORDER BY score DESC, discovered_at LIMIT 1",
                    (self._job,),
                ).fetchone()
                if row is None:
                    return None
                c.execute(
                    "UPDATE frontier SET state = 'fetching' WHERE job_id = ? AND url = ?",
                    (self._job, row[0]),
                )
                return Item(row[0], row[1], row[2], row[3])

        return await self._db.run(work)

    async def finish(self, url: str, o: Outcome) -> None:
        def work(c: sqlite3.Connection) -> None:
            with c:
                c.execute(
                    "UPDATE frontier SET state = ?, fetched_at = ?, final_url = ?, status = ?, "
                    "title = ?, snippet = ?, relevance = ?, content_hash = ?, simhash = ?, "
                    "duplicate_of = ?, error = ?, bytes = ?, elapsed_ms = ?, cached = ? "
                    "WHERE job_id = ? AND url = ?",
                    (
                        o.state,
                        time.time(),
                        o.final_url,
                        o.status,
                        o.title,
                        o.snippet,
                        o.relevance,
                        o.content_hash,
                        o.simhash,
                        o.duplicate_of,
                        o.error,
                        o.bytes,
                        o.elapsed_ms,
                        int(o.cached),
                        self._job,
                        url,
                    ),
                )

        await self._db.run(work)

    async def mark_alias(self, url: str, of: str) -> None:
        """`url` turned out to be another name for `of` (a redirect target or a canonical link).
        Record it as a duplicate so it is never fetched, unless it was fetched already."""
        now = time.time()

        def work(c: sqlite3.Connection) -> None:
            with c:
                c.execute(
                    "INSERT INTO frontier(job_id, url, depth, score, state, duplicate_of, "
                    "discovered_at) VALUES (?, ?, 0, 0, 'duplicate', ?, ?) "
                    "ON CONFLICT(job_id, url) DO UPDATE SET state = 'duplicate', "
                    "duplicate_of = excluded.duplicate_of WHERE state = 'pending'",
                    (self._job, url, of, now),
                )

        await self._db.run(work)

    async def state_of(self, url: str) -> str | None:
        def work(c: sqlite3.Connection) -> str | None:
            row = c.execute(
                "SELECT state FROM frontier WHERE job_id = ? AND url = ?", (self._job, url)
            ).fetchone()
            return None if row is None else str(row[0])

        return await self._db.run(work)

    async def taken(self) -> int:
        """URLs taken off the frontier so far, in any run: what max_pages counts. Aliases were
        never fetched and do not count."""

        def work(c: sqlite3.Connection) -> int:
            return int(
                c.execute(
                    "SELECT count(*) FROM frontier WHERE job_id = ? "
                    "AND (fetched_at IS NOT NULL OR state = 'fetching')",
                    (self._job,),
                ).fetchone()[0]
            )

        return await self._db.run(work)

    async def fingerprints(self) -> list[tuple[str, str | None, int | None]]:
        """(url, content_hash, signed simhash) of every page kept so far, so a resumed crawl
        can rebuild its duplicate check."""

        def work(c: sqlite3.Connection) -> list[tuple[str, str | None, int | None]]:
            return [
                (r[0], r[1], r[2])
                for r in c.execute(
                    "SELECT url, content_hash, simhash FROM frontier "
                    "WHERE job_id = ? AND state = 'done'",
                    (self._job,),
                )
            ]

        return await self._db.run(work)
