import asyncio
import sqlite3
from pathlib import Path

import pytest

from weft.db import SCHEMA_VERSION, Database


async def test_open_creates_parent_dirs_and_enables_wal(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "a" / "b" / "weft.db")
    try:
        mode = await db.run(lambda c: c.execute("PRAGMA journal_mode").fetchone()[0])
        fk = await db.run(lambda c: c.execute("PRAGMA foreign_keys").fetchone()[0])
    finally:
        await db.close()
    assert mode == "wal"
    assert fk == 1


async def test_schema_is_created_once_and_reopen_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "weft.db"
    db = await Database.open(path)
    tables = await db.run(
        lambda c: {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    )
    assert {"jobs", "frontier", "pages"} <= tables
    await db.run(
        lambda c: c.execute(
            "INSERT INTO jobs(id, kind, status, params, created_at, updated_at) "
            "VALUES ('j1', 'crawl', 'queued', '{}', 1, 1)"
        ).connection.commit()
    )
    await db.close()

    db = await Database.open(path)
    try:
        version = await db.run(lambda c: c.execute("PRAGMA user_version").fetchone()[0])
        count = await db.run(lambda c: c.execute("SELECT count(*) FROM jobs").fetchone()[0])
    finally:
        await db.close()
    assert version == SCHEMA_VERSION
    assert count == 1


async def test_frontier_rows_die_with_their_job(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "weft.db")

    def work(c: sqlite3.Connection) -> int:
        with c:
            c.execute(
                "INSERT INTO jobs(id, kind, status, params, created_at, updated_at) "
                "VALUES ('j1', 'crawl', 'queued', '{}', 1, 1)"
            )
            c.execute(
                "INSERT INTO frontier(job_id, url, depth, score, state, discovered_at) "
                "VALUES ('j1', 'https://a.test/', 0, 1.0, 'pending', 1)"
            )
            c.execute("DELETE FROM jobs WHERE id = 'j1'")
        return int(c.execute("SELECT count(*) FROM frontier").fetchone()[0])

    try:
        assert await db.run(work) == 0
    finally:
        await db.close()


async def test_concurrent_callers_are_serialised(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "weft.db")

    def insert(i: int):
        def fn(c: sqlite3.Connection) -> None:
            with c:
                c.execute(
                    "INSERT INTO jobs(id, kind, status, params, created_at, updated_at) "
                    "VALUES (?, 'crawl', 'queued', '{}', 1, 1)",
                    (f"j{i}",),
                )

        return fn

    try:
        await asyncio.gather(*(db.run(insert(i)) for i in range(50)))
        count = await db.run(lambda c: c.execute("SELECT count(*) FROM jobs").fetchone()[0])
    finally:
        await db.close()
    assert count == 50


async def test_refuses_a_database_from_a_newer_version(tmp_path: Path) -> None:
    path = tmp_path / "weft.db"
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    raw.close()
    with pytest.raises(RuntimeError, match="newer"):
        await Database.open(path)


async def test_a_version_1_file_is_migrated_and_keeps_its_jobs(tmp_path: Path) -> None:
    from weft.db import _MIGRATIONS

    path = tmp_path / "weft.db"
    raw = sqlite3.connect(path)
    raw.executescript(f"BEGIN;{_MIGRATIONS[0]}PRAGMA user_version = 1;COMMIT;")
    raw.execute(
        "INSERT INTO jobs(id, kind, status, params, created_at, updated_at) "
        "VALUES ('old', 'crawl', 'done', '{}', 1, 1)"
    )
    raw.commit()
    raw.close()

    db = await Database.open(path)
    try:
        version = await db.run(lambda c: c.execute("PRAGMA user_version").fetchone()[0])
        job = await db.run(lambda c: c.execute("SELECT id, run_s FROM jobs").fetchone())
        frontier = await db.run(lambda c: {r[1] for r in c.execute("PRAGMA table_info(frontier)")})
        pages = await db.run(lambda c: {r[1] for r in c.execute("PRAGMA table_info(pages)")})
    finally:
        await db.close()
    assert version == SCHEMA_VERSION == 2
    assert job == ("old", 0)
    assert {"simhash", "duplicate_of", "relevance", "run"} <= frontier
    assert {"url", "render", "result", "etag"} <= pages
