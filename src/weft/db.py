"""SQLite storage: frontier, job state, page cache. One WAL database, no external services.

All access goes through a single dedicated thread. That keeps the connection thread-local
(sqlite3 enforces it) and serialises callers even when one of them is cancelled mid-call.
Pass `run` a function that takes the connection; use `with conn:` inside it for a transaction.
"""

import asyncio
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Self

# Append-only. Entry N upgrades a database from user_version N-1 to N.
_MIGRATIONS = [
    """
    CREATE TABLE jobs (
        id          TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,   -- crawl | search
        status      TEXT NOT NULL,   -- queued | running | done | failed | cancelled
        params      TEXT NOT NULL,   -- JSON of the tool arguments, enough to resume
        error       TEXT,
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL,
        finished_at REAL
    );

    CREATE TABLE frontier (
        job_id        TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        url           TEXT NOT NULL,  -- canonical form
        depth         INTEGER NOT NULL,
        score         REAL NOT NULL,
        parent_url    TEXT,
        anchor_text   TEXT,
        state         TEXT NOT NULL,  -- pending | fetching | done | failed | skipped
        attempts      INTEGER NOT NULL DEFAULT 0,
        discovered_at REAL NOT NULL,
        PRIMARY KEY (job_id, url)
    ) WITHOUT ROWID;
    CREATE INDEX frontier_next ON frontier(job_id, state, score DESC);

    CREATE TABLE pages (
        url           TEXT PRIMARY KEY,  -- canonical form
        final_url     TEXT NOT NULL,
        status        INTEGER NOT NULL,
        etag          TEXT,
        last_modified TEXT,
        content_type  TEXT,
        content_hash  TEXT,
        title         TEXT,
        markdown      TEXT,
        fetched_at    REAL NOT NULL,
        expires_at    REAL NOT NULL
    );
    CREATE INDEX pages_content_hash ON pages(content_hash);
    """,
    # Version 2: the first consumers. Nothing ever wrote to frontier or pages, so both are
    # rebuilt in the shape the crawler and the page cache need.
    """
    ALTER TABLE jobs ADD COLUMN stop_reason TEXT;  -- max_pages | time_budget | exhausted
    ALTER TABLE jobs ADD COLUMN run_s REAL NOT NULL DEFAULT 0;  -- time spent running, all runs

    DROP TABLE frontier;
    CREATE TABLE frontier (
        job_id        TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        url           TEXT NOT NULL,  -- canonical form
        depth         INTEGER NOT NULL,
        score         REAL NOT NULL,  -- priority while pending
        parent_url    TEXT,
        anchor_text   TEXT,
        run           INTEGER NOT NULL DEFAULT 0,  -- ancestors in a row with this URL's pattern
        -- pending | fetching | done | duplicate | failed
        state         TEXT NOT NULL,
        discovered_at REAL NOT NULL,
        fetched_at    REAL,
        final_url     TEXT,
        status        INTEGER,
        title         TEXT,
        snippet       TEXT,
        relevance     REAL,  -- the fetched page against the goal, 0 to 1
        content_hash  TEXT,
        simhash       INTEGER,  -- signed 64-bit
        duplicate_of  TEXT,
        error         TEXT,   -- FetchError code
        bytes         INTEGER,
        elapsed_ms    REAL,
        cached        INTEGER,
        PRIMARY KEY (job_id, url)
    ) WITHOUT ROWID;
    CREATE INDEX frontier_next ON frontier(job_id, state, score DESC, discovered_at);

    DROP TABLE pages;
    CREATE TABLE pages (
        url           TEXT NOT NULL,  -- canonical form of the requested URL
        render        TEXT NOT NULL,  -- the render mode it was fetched with
        result        TEXT NOT NULL,  -- PageResult as JSON
        etag          TEXT,
        last_modified TEXT,
        fetched_at    REAL NOT NULL,
        expires_at    REAL NOT NULL,
        PRIMARY KEY (url, render)
    ) WITHOUT ROWID;
    """,
]
SCHEMA_VERSION = len(_MIGRATIONS)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"{path} has schema version {current}, newer than this build ({SCHEMA_VERSION})"
            )
        for version in range(current, SCHEMA_VERSION):
            # Schema and version bump commit together, so a crash never leaves a half-migrated
            # file.
            conn.executescript(
                f"BEGIN;{_MIGRATIONS[version]}PRAGMA user_version = {version + 1};COMMIT;"
            )
    except BaseException:
        conn.close()
        raise
    return conn


class Database:
    def __init__(self, executor: ThreadPoolExecutor, conn: sqlite3.Connection) -> None:
        self._executor = executor
        self._conn = conn

    @classmethod
    async def open(cls, path: Path) -> Self:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="weft-db")
        try:
            conn = await asyncio.get_running_loop().run_in_executor(executor, _connect, path)
        except BaseException:
            executor.shutdown(wait=False)
            raise
        return cls(executor, conn)

    async def run[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn, self._conn)

    async def close(self) -> None:
        await self.run(lambda c: c.close())
        self._executor.shutdown(wait=True)
