"""Crawl jobs: start, inspect, cancel, list, and resume after a restart.

A job is a row in `jobs` plus its frontier rows. Its task runs in this process. Shutting down
cancels the tasks but leaves their rows `running`, which is also what a killed process leaves,
so both cases resume the same way the next time the server starts. Only `cancel` and the end of a
crawl change a job's final status.
"""

import asyncio
import logging
import math
import sqlite3
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from weft.jobs.crawl import Crawl, CrawlDeps
from weft.models import CrawlPage, CrawlParams, JobInfo, JobResult, JobStats, JobStatus

log = logging.getLogger("weft.jobs")

_LIVE = ("queued", "running")


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _percentile(sorted_values: list[float], p: float) -> float | None:
    """Nearest rank."""
    if not sorted_values:
        return None
    return round(sorted_values[max(0, math.ceil(p * len(sorted_values)) - 1)], 1)


def _stats(c: sqlite3.Connection, job_id: str, run_s: float) -> JobStats:
    counts = {"done": 0, "duplicate": 0, "failed": 0, "pending": 0, "fetching": 0}
    for state, n in c.execute(
        "SELECT state, count(*) FROM frontier WHERE job_id = ? "
        "AND (state != 'duplicate' OR fetched_at IS NOT NULL) GROUP BY state",
        (job_id,),
    ):
        counts[state] = n
    cached, total_bytes = c.execute(
        "SELECT coalesce(sum(cached), 0), coalesce(sum(bytes), 0) FROM frontier WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    errors = dict(
        c.execute(
            "SELECT error, count(*) FROM frontier WHERE job_id = ? AND state = 'failed' "
            "GROUP BY error ORDER BY count(*) DESC",
            (job_id,),
        ).fetchall()
    )
    latencies = [
        r[0]
        for r in c.execute(
            "SELECT elapsed_ms FROM frontier WHERE job_id = ? AND elapsed_ms IS NOT NULL "
            "AND NOT cached ORDER BY elapsed_ms",
            (job_id,),
        )
    ]
    return JobStats(
        pages_fetched=counts["done"] + counts["duplicate"] + counts["failed"],
        pages_ok=counts["done"],
        duplicates=counts["duplicate"],
        failed=counts["failed"],
        pending=counts["pending"] + counts["fetching"],
        cached=cached,
        bytes=total_bytes,
        errors=errors,
        latency_ms_p50=_percentile(latencies, 0.5),
        latency_ms_p95=_percentile(latencies, 0.95),
        latency_ms_max=_percentile(latencies, 1.0),
        run_s=round(run_s, 1),
    )


_SELECT_JOB = (
    "SELECT id, kind, status, stop_reason, error, params, created_at, updated_at, finished_at, "
    "run_s FROM jobs "
)


def _info(c: sqlite3.Connection, row: tuple[Any, ...]) -> JobInfo:
    job_id, _kind, status, stop_reason, error, params, created, updated, finished, run_s = row
    return JobInfo(
        id=job_id,
        kind="crawl",
        status=status,
        stop_reason=stop_reason,
        error=error,
        params=CrawlParams.model_validate_json(params),
        stats=_stats(c, job_id, run_s),
        created_at=_iso(created) or "",
        updated_at=_iso(updated) or "",
        finished_at=_iso(finished),
    )


class JobManager:
    def __init__(self, deps: CrawlDeps) -> None:
        self._deps = deps
        self._db = deps.db
        self._slots = asyncio.Semaphore(deps.settings.crawl_max_jobs)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def start_crawl(self, params: CrawlParams) -> str:
        job_id = uuid.uuid4().hex
        now = time.time()
        doc = params.model_dump_json()

        def work(c: sqlite3.Connection) -> None:
            with c:
                c.execute(
                    "INSERT INTO jobs(id, kind, status, params, created_at, updated_at) "
                    "VALUES (?, 'crawl', 'queued', ?, ?, ?)",
                    (job_id, doc, now, now),
                )

        await self._db.run(work)
        self._spawn(job_id, params)
        log.info("job queued", extra={"job": job_id, "seeds": len(params.seed_urls)})
        return job_id

    async def resume(self) -> int:
        """Restart every job a previous process left queued or running. Returns how many."""

        def work(c: sqlite3.Connection) -> list[tuple[str, str]]:
            return c.execute(
                "SELECT id, params FROM jobs WHERE status IN ('queued', 'running') "
                "ORDER BY created_at"
            ).fetchall()

        rows = await self._db.run(work)
        for job_id, params in rows:
            self._spawn(job_id, CrawlParams.model_validate_json(params))
            log.info("job resumed", extra={"job": job_id})
        return len(rows)

    def _spawn(self, job_id: str, params: CrawlParams) -> None:
        task = asyncio.create_task(self._run(job_id, params), name=f"crawl-{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(job_id, None))

    async def _set(self, job_id: str, sql: str, args: tuple[object, ...]) -> bool:
        """Run an UPDATE on one job; True if it changed the row."""

        def work(c: sqlite3.Connection) -> bool:
            with c:
                return c.execute(sql, (*args, job_id)).rowcount > 0

        return await self._db.run(work)

    async def _run(self, job_id: str, params: CrawlParams) -> None:
        async with self._slots:
            now = time.time()
            started = await self._set(
                job_id,
                "UPDATE jobs SET status = 'running', updated_at = ? "
                "WHERE status IN ('queued', 'running') AND id = ?",
                (now,),
            )
            if not started:  # cancelled while it waited for a slot
                return
            run_before = await self._db.run(
                lambda c: float(
                    c.execute("SELECT run_s FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
                )
            )
            try:
                reason = await Crawl(self._deps, job_id, params).run(run_before)
            except asyncio.CancelledError:
                raise  # cancel_job or shutdown; the row says which
            except Exception as exc:
                log.exception("job failed", extra={"job": job_id})
                cause = exc.exceptions[0] if isinstance(exc, ExceptionGroup) else exc
                now = time.time()
                await self._set(
                    job_id,
                    "UPDATE jobs SET status = 'failed', error = ?, updated_at = ?, "
                    "finished_at = ? WHERE status = 'running' AND id = ?",
                    (f"internal error: {type(cause).__name__}", now, now),
                )
                return
            now = time.time()
            await self._set(
                job_id,
                "UPDATE jobs SET status = 'done', stop_reason = ?, updated_at = ?, "
                "finished_at = ? WHERE status = 'running' AND id = ?",
                (reason, now, now),
            )
            log.info("job done", extra={"job": job_id, "stop_reason": reason})

    async def cancel(self, job_id: str) -> JobInfo | None:
        now = time.time()
        await self._set(
            job_id,
            "UPDATE jobs SET status = 'cancelled', updated_at = ?, finished_at = ? "
            "WHERE status IN ('queued', 'running') AND id = ?",
            (now, now),
        )
        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await self._deps.db.run(
                lambda c: c.execute(
                    "UPDATE frontier SET state = 'pending' WHERE job_id = ? AND state = 'fetching'",
                    (job_id,),
                ).connection.commit()
            )
        return await self.info(job_id)

    async def wait(self, job_id: str, timeout_s: float) -> None:
        """Wait up to `timeout_s` for the job's task to finish. The job is not cancelled."""
        task = self._tasks.get(job_id)
        if task is not None:
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout_s)

    def running(self, job_id: str) -> bool:
        return job_id in self._tasks

    async def info(self, job_id: str) -> JobInfo | None:
        def work(c: sqlite3.Connection) -> JobInfo | None:
            row = c.execute(_SELECT_JOB + "WHERE id = ?", (job_id,)).fetchone()
            return None if row is None else _info(c, row)

        return await self._db.run(work)

    async def result(self, job_id: str, limit: int, offset: int) -> JobResult | None:
        def work(c: sqlite3.Connection) -> JobResult | None:
            row = c.execute(_SELECT_JOB + "WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            info = _info(c, row)
            pages = [
                CrawlPage(
                    url=r[0],
                    final_url=r[1],
                    title=r[2],
                    status=r[3],
                    depth=r[4],
                    relevance=round(r[5], 4),
                    snippet=r[6] or "",
                    cached=bool(r[7]),
                )
                for r in c.execute(
                    "SELECT url, final_url, title, status, depth, relevance, snippet, cached "
                    "FROM frontier WHERE job_id = ? AND state = 'done' "
                    "ORDER BY relevance DESC, fetched_at LIMIT ? OFFSET ?",
                    (job_id, limit, offset),
                )
            ]
            return JobResult(job=info, pages=pages, pages_total=info.stats.pages_ok)

        return await self._db.run(work)

    async def list(self, status: JobStatus | None, limit: int) -> list[JobInfo]:
        def work(c: sqlite3.Connection) -> list[JobInfo]:
            where, args = ("WHERE status = ?", (status, limit)) if status else ("", (limit,))
            rows = c.execute(
                _SELECT_JOB + where + " ORDER BY created_at DESC, rowid DESC LIMIT ?", args
            ).fetchall()
            return [_info(c, r) for r in rows]

        return await self._db.run(work)

    async def aclose(self) -> None:
        """Stop every task and leave the rows as they are, to be resumed by the next start."""
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
