"""Crawl jobs end to end against the fixture site: ordering, traps, dedupe, scope, budgets,
cancellation and resume, including after the process is killed."""

import asyncio
import contextlib
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from fixture_site import FixtureSite
from weft.config import Settings
from weft.db import Database
from weft.fetch.cache import PageCache
from weft.fetch.http import Fetcher
from weft.jobs.crawl import MAX_PATTERN_RUN, CrawlDeps
from weft.jobs.manager import JobManager
from weft.models import CrawlParams, JobResult


class Env:
    def __init__(self, jobs: JobManager, db: Database, settings: Settings) -> None:
        self.jobs, self.db, self.settings = jobs, db, settings

    async def crawl(self, wait_s: float = 60, **params: Any) -> JobResult:
        job_id = await self.jobs.start_crawl(CrawlParams(**{"render": "never", **params}))
        await self.jobs.wait(job_id, wait_s)
        result = await self.jobs.result(job_id, 500, 0)
        assert result is not None
        return result

    async def rows(self, job_id: str) -> dict[str, dict[str, Any]]:
        def work(c: sqlite3.Connection) -> dict[str, dict[str, Any]]:
            c.row_factory = sqlite3.Row
            try:
                return {
                    r["url"]: dict(r)
                    for r in c.execute("SELECT * FROM frontier WHERE job_id = ?", (job_id,))
                }
            finally:
                c.row_factory = None

        return await self.db.run(work)

    async def fetch_order(self, job_id: str) -> list[str]:
        rows = await self.rows(job_id)
        fetched = [r for r in rows.values() if r["fetched_at"] is not None]
        return [r["url"] for r in sorted(fetched, key=lambda r: r["fetched_at"])]


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Callable[..., Any]]:
    opened: list[tuple[JobManager, Fetcher, Database]] = []

    async def make(**overrides: Any) -> Env:
        settings = Settings(data_dir=tmp_path / "data", **overrides)
        db = await Database.open(settings.db_path)
        fetcher = Fetcher(settings)
        jobs = JobManager(CrawlDeps(settings, db, fetcher, None, PageCache(db, 0)))
        opened.append((jobs, fetcher, db))
        return Env(jobs, db, settings)

    yield make
    for jobs, fetcher, db in reversed(opened):
        await jobs.aclose()
        await fetcher.aclose()
        await db.close()


def path(site: FixtureSite, url: str) -> str:
    return url.removeprefix(site.base)


# --- ordering -------------------------------------------------------------------------------


async def test_a_link_to_a_host_idna_rejects_fails_its_own_row_and_not_the_job(
    make_site, env
) -> None:
    site: FixtureSite = make_site()
    e = await env()
    r = await e.crawl(
        seed_urls=[site.url("/site/odd-hosts")], same_domain_only=False, max_pages=5, max_depth=1
    )
    rows = await e.rows(r.job.id)
    assert r.job.status == "done"
    assert (rows["http://xn--/a"]["state"], rows["http://xn--/a"]["error"]) == (
        "failed",
        "invalid_url",
    )
    assert rows[site.url("/site/about")]["state"] == "done"


async def test_the_goal_is_crawled_first(make_site, env) -> None:
    site: FixtureSite = make_site()
    e = await env(crawl_concurrency=1)
    r = await e.crawl(seed_urls=[site.url("/site/")], goal="pricing plans", max_pages=3)
    order = [path(site, u) for u in await e.fetch_order(r.job.id)]
    assert order == ["/site/", "/site/pricing", "/site/pricing/enterprise"]
    assert r.job.stop_reason == "max_pages"
    assert [path(site, p.url) for p in r.pages[:2]] == ["/site/pricing", "/site/pricing/enterprise"]
    assert r.pages[0].relevance > r.pages[-1].relevance
    assert "twelve dollars" in r.pages[0].snippet
    assert site.hits_under("/trap/") == {}


async def test_anchor_text_alone_decides_which_link_is_fetched_first(make_site, env) -> None:
    """Four links with the same URL shape, the matching one listed last, from a page that says
    nothing about the goal. Only anchor text can put it first. Dropping the anchor weight, or
    the crawler failing to hand the anchor text to the scorer, leaves a four-way tie that goes
    to the first link listed, and this test fails."""
    site: FixtureSite = make_site()
    e = await env(crawl_concurrency=1)
    r = await e.crawl(seed_urls=[site.url("/site/anchors")], goal="refund policy", max_pages=2)
    order = [path(site, u) for u in await e.fetch_order(r.job.id)]
    assert order == ["/site/anchors", "/site/anchors/a4"]
    assert site.hits_under("/site/anchors/a") == {"/site/anchors/a4": 1}
    scores = {path(site, u): row["score"] for u, row in (await e.rows(r.job.id)).items()}
    assert scores["/site/anchors/a4"] > 0
    assert (
        scores["/site/anchors/a1"] == scores["/site/anchors/a2"] == scores["/site/anchors/a3"] == 0
    )


async def test_without_a_goal_the_crawl_is_breadth_first(make_site, env) -> None:
    site: FixtureSite = make_site()
    e = await env(crawl_concurrency=1)
    r = await e.crawl(seed_urls=[site.url("/site/")], max_pages=3)
    order = [path(site, u) for u in await e.fetch_order(r.job.id)]
    assert order == ["/site/", "/site/about", "/site/blog"]  # the order the home page lists them


# --- traps, dedupe, canonical tags ----------------------------------------------------------


async def test_infinite_link_traps_are_bounded(make_site, env) -> None:
    """/trap/N links to /trap/N+1 forever, and the calendar links a month either way forever.
    With depth and page budgets out of the way, the pattern-run cap ends both and the crawl
    finishes on its own."""
    site: FixtureSite = make_site()
    e = await env()
    r = await e.crawl(seed_urls=[site.url("/site/")], max_pages=500, max_depth=20)
    assert r.job.status == "done"
    assert r.job.stop_reason == "exhausted"
    assert len(site.hits_under("/trap/")) == MAX_PATTERN_RUN + 1
    assert site.hits("/site/calendar") <= 2 * MAX_PATTERN_RUN + 1
    assert r.job.stats.pages_fetched < 60


async def test_near_and_exact_duplicates_are_kept_once(make_site, env) -> None:
    site: FixtureSite = make_site()
    e = await env()
    r = await e.crawl(seed_urls=[site.url("/site/")], max_depth=1, max_pages=100)
    rows = await e.rows(r.job.id)
    dups = {path(site, u): rows[site.url(u)] for u in ("/site/dup-a", "/site/dup-b", "/site/dup-c")}
    kept = [p for p, row in dups.items() if row["state"] == "done"]
    assert len(kept) == 1, dups
    for p, row in dups.items():
        if p not in kept:
            assert row["state"] == "duplicate"
            assert row["duplicate_of"] == site.url(kept[0])
    assert not any(
        path(site, p.url).startswith("/site/dup-") and p.url != site.url(kept[0]) for p in r.pages
    )
    assert r.job.stats.duplicates >= 2


async def test_short_exact_duplicates_are_caught_by_the_text_hash(make_site, env) -> None:
    """Under MIN_WORDS there is no simhash, so the exact hash is all that finds the copy."""
    site: FixtureSite = make_site()
    e = await env(crawl_concurrency=1)
    r = await e.crawl(seed_urls=[site.url("/site/edge/short-a"), site.url("/site/edge/short-b")])
    rows = await e.rows(r.job.id)
    a, b = rows[site.url("/site/edge/short-a")], rows[site.url("/site/edge/short-b")]
    assert (a["state"], a["simhash"]) == ("done", None)
    assert (b["state"], b["duplicate_of"]) == ("duplicate", site.url("/site/edge/short-a"))


async def test_a_canonical_link_makes_a_page_an_alias(make_site, env) -> None:
    site: FixtureSite = make_site()
    e = await env(crawl_concurrency=1)
    r = await e.crawl(seed_urls=[site.url("/site/")], max_depth=1, max_pages=100)
    rows = await e.rows(r.job.id)
    alias = rows[site.url("/site/alias")]  # utm_source was dropped on the way in
    assert alias["state"] == "duplicate"
    assert alias["duplicate_of"] == site.url("/site/pricing")
    assert rows[site.url("/site/pricing")]["state"] == "done"
    assert site.url("/site/alias") not in {p.url for p in r.pages}


# --- scope ----------------------------------------------------------------------------------


async def test_links_out_of_bounds_are_never_queued(make_site, env) -> None:
    site: FixtureSite = make_site()
    e = await env()
    r = await e.crawl(seed_urls=[site.url("/site/")], max_depth=2, max_pages=100)
    rows = await e.rows(r.job.id)
    assert site.url("/site/nofollowed") not in rows  # rel=nofollow
    assert site.url("/private/site-secret") not in rows  # robots.txt
    assert not any("other.test" in u for u in rows)  # another site
    assert site.hits("/site/nofollowed") == 0
    assert site.hits_under("/private/") == {}


async def test_paths_that_repeat_a_segment_are_never_queued(make_site, env) -> None:
    site: FixtureSite = make_site()
    e = await env()
    r = await e.crawl(seed_urls=[site.url("/site/edge")], max_depth=2)
    rows = await e.rows(r.job.id)
    assert site.url("/site/edge/a/b/a/b/a/b") not in rows
    assert site.hits("/site/edge/a/b/a/b/a/b") == 0
    assert site.url("/site/edge/short-a") in rows  # the other links on the page were followed


async def test_failures_are_recorded_by_code(make_site, env) -> None:
    site: FixtureSite = make_site()
    e = await env()
    r = await e.crawl(seed_urls=[site.url("/site/")], max_depth=1, max_pages=100)
    assert r.job.stats.errors == {"http_404": 1, "unsupported_content_type": 1}
    assert r.job.stats.failed == 2
    assert r.job.stats.pages_fetched == (
        r.job.stats.pages_ok + r.job.stats.duplicates + r.job.stats.failed
    )
    assert r.job.stats.bytes > 0
    assert r.job.stats.latency_ms_p50 is not None
    assert r.job.stats.latency_ms_p50 <= r.job.stats.latency_ms_p95 <= r.job.stats.latency_ms_max


async def test_depth_zero_fetches_only_the_seeds(make_site, env) -> None:
    site: FixtureSite = make_site()
    e = await env()
    r = await e.crawl(seed_urls=[site.url("/site/"), site.url("/site/about")], max_depth=0)
    assert r.job.stats.pages_fetched == 2
    assert r.job.stop_reason == "exhausted"


async def test_include_and_exclude_patterns(make_site, env) -> None:
    site: FixtureSite = make_site()
    e = await env()
    r = await e.crawl(
        seed_urls=[site.url("/site/")],
        include_patterns=["*/site/blog*"],
        exclude_patterns=["*/blog/2"],
        max_pages=100,
    )
    fetched = {path(site, u) for u in await e.fetch_order(r.job.id)}
    assert fetched == {"/site/", "/site/blog", "/site/blog/1", "/site/blog/3"}


# --- budgets, jobs, cancel, resume -----------------------------------------------------------


async def test_the_time_budget_stops_a_job_and_leaves_nothing_half_taken(make_site, env) -> None:
    site: FixtureSite = make_site()
    e = await env(crawl_timeout_s=1.0, host_min_interval_s=0.4)
    r = await e.crawl(seed_urls=[site.url("/site/")], max_pages=100, max_depth=5)
    assert r.job.stop_reason == "time_budget"
    assert 1 <= r.job.stats.pages_fetched < 10
    rows = await e.rows(r.job.id)
    assert not any(row["state"] == "fetching" for row in rows.values())
    assert r.job.stats.run_s >= 1.0


async def _insert_running_job(e: Env, job_id: str, params: CrawlParams, run_s: float) -> None:
    """A job a dead process left `running`, with `run_s` seconds of running time already spent."""
    now = time.time()
    await e.db.run(
        lambda c: c.execute(
            "INSERT INTO jobs(id, kind, status, params, created_at, updated_at, run_s) "
            "VALUES (?, 'crawl', 'running', ?, ?, ?, ?)",
            (job_id, params.model_dump_json(), now, now, run_s),
        ).connection.commit()
    )


async def test_a_resumed_job_has_only_the_time_it_has_left(make_site, env) -> None:
    site: FixtureSite = make_site()
    e = await env(crawl_timeout_s=30.0, host_min_interval_s=0)
    params = CrawlParams(seed_urls=[site.url("/site/")], render="never")
    await _insert_running_job(e, "spent", params, run_s=30.0)  # the whole budget, over restarts
    assert await e.jobs.resume() == 1
    await e.jobs.wait("spent", 10)
    info = await e.jobs.info("spent")
    assert info is not None
    assert (info.status, info.stop_reason) == ("done", "time_budget")
    assert site.total_hits == 0


async def test_a_resumed_job_adds_to_the_time_it_already_ran(make_site, env) -> None:
    site: FixtureSite = make_site()
    e = await env(crawl_timeout_s=30.0, host_min_interval_s=0)
    params = CrawlParams(seed_urls=[site.url("/site/")], max_pages=2, render="never")
    await _insert_running_job(e, "part", params, run_s=20.0)
    assert await e.jobs.resume() == 1
    await e.jobs.wait("part", 10)
    info = await e.jobs.info("part")
    assert info is not None and info.status == "done"
    assert 20.0 <= info.stats.run_s < 30.0  # reported to a tenth of a second


async def test_jobs_beyond_the_limit_wait_their_turn(make_site, env) -> None:
    site: FixtureSite = make_site()
    e = await env(crawl_max_jobs=1, host_min_interval_s=0.2)
    p = CrawlParams(seed_urls=[site.url("/site/")], max_pages=4, render="never")
    first = await e.jobs.start_crawl(p)
    second = await e.jobs.start_crawl(p)
    await asyncio.sleep(0.3)
    assert (await e.jobs.info(first)).status == "running"
    assert (await e.jobs.info(second)).status == "queued"
    await e.jobs.wait(first, 30)
    await e.jobs.wait(second, 30)
    assert (await e.jobs.info(second)).status == "done"
    listed = await e.jobs.list(None, 10)
    assert [j.id for j in listed] == [second, first]
    assert [j.id for j in await e.jobs.list("done", 10)] == [second, first]


async def test_cancel_stops_fetching_and_is_final(make_site, env) -> None:
    site: FixtureSite = make_site()
    e = await env(host_min_interval_s=0.3)
    job_id = await e.jobs.start_crawl(
        CrawlParams(seed_urls=[site.url("/site/")], max_pages=100, render="never")
    )
    await asyncio.sleep(1.0)
    info = await e.jobs.cancel(job_id)
    assert info is not None and info.status == "cancelled"
    assert not e.jobs.running(job_id)
    hits = site.total_hits
    await asyncio.sleep(0.8)
    assert site.total_hits == hits  # nothing more was fetched
    assert await e.jobs.resume() == 0  # a cancelled job is never resumed
    again = await e.jobs.cancel(job_id)  # cancelling twice changes nothing
    assert again is not None and again.status == "cancelled"
    assert await e.jobs.cancel("no-such-job") is None


async def test_a_crawl_that_finishes_as_it_is_cancelled_stays_cancelled(
    make_site, env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crawl can return just as cancel lands, before it sees the cancellation. Its late `done`
    must not overwrite `cancelled`."""
    from weft.jobs import crawl as crawl_module

    started = asyncio.Event()

    async def stubborn(self, run_before_s: float) -> str:  # type: ignore[no-untyped-def]
        started.set()
        with contextlib.suppress(asyncio.CancelledError):  # finishes anyway, as a last page would
            await asyncio.sleep(60)
        return "exhausted"

    monkeypatch.setattr(crawl_module.Crawl, "run", stubborn)
    site: FixtureSite = make_site()
    e = await env()
    job_id = await e.jobs.start_crawl(CrawlParams(seed_urls=[site.url("/site/")], render="never"))
    await asyncio.wait_for(started.wait(), 10)
    info = await e.jobs.cancel(job_id)
    assert info is not None
    assert (info.status, info.stop_reason) == ("cancelled", None)
    assert not e.jobs.running(job_id)


async def test_a_stopped_server_resumes_its_jobs(make_site, env) -> None:
    """Shutting down leaves jobs `running`, as a kill would. A new manager on the same database
    picks them up, keeps what was done, and fetches each page once (bar those cut off in
    flight)."""
    site: FixtureSite = make_site()
    first = await env(host_min_interval_s=0.25, crawl_concurrency=2)
    job_id = await first.jobs.start_crawl(
        CrawlParams(seed_urls=[site.url("/site/")], max_pages=12, max_depth=1, render="never")
    )
    while (await first.jobs.info(job_id)).stats.pages_fetched < 3:  # noqa: ASYNC110
        await asyncio.sleep(0.05)
    await first.jobs.aclose()
    before = (await first.jobs.info(job_id)).stats.pages_fetched
    assert (await first.jobs.info(job_id)).status == "running"

    second = await env(host_min_interval_s=0)
    assert await second.jobs.resume() == 1
    await second.jobs.wait(job_id, 30)
    r = await second.jobs.result(job_id, 500, 0)
    assert r is not None
    assert r.job.status == "done"
    assert r.job.stats.pages_fetched == 12 > before
    refetched = sum(n - 1 for p, n in site.hits_under("/site/").items() if n > 1)
    assert refetched <= 2  # at most the pages that were in flight at the stop


CHILD = textwrap.dedent(
    """
    import asyncio, sys
    from weft.config import Settings
    from weft.db import Database
    from weft.fetch.cache import PageCache
    from weft.fetch.http import Fetcher
    from weft.jobs.crawl import CrawlDeps
    from weft.jobs.manager import JobManager
    from weft.models import CrawlParams

    async def main():
        s = Settings()
        db = await Database.open(s.db_path)
        jobs = JobManager(CrawlDeps(s, db, Fetcher(s), None, PageCache(db, s.cache_ttl_s)))
        job_id = await jobs.start_crawl(CrawlParams(
            seed_urls=[sys.argv[1]], max_pages=12, max_depth=1, render="never"))
        print(job_id, flush=True)
        await asyncio.sleep(120)  # the test kills the process long before this

    asyncio.run(main())
    """
)


async def test_a_killed_process_resumes_its_job(make_site, env, tmp_path: Path) -> None:
    site: FixtureSite = make_site()
    data = tmp_path / "data"
    child_env = {
        **os.environ,  # conftest has already set loopback on and cleared the host's WEFT_*
        "WEFT_DATA_DIR": str(data),
        "WEFT_HOST_MIN_INTERVAL_S": "0.25",
        "WEFT_CRAWL_CONCURRENCY": "2",
    }
    proc = subprocess.Popen(  # noqa: S603, ASYNC220 - our own interpreter; Popen returns at once
        [sys.executable, "-c", CHILD, site.url("/site/")],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert proc.stdout is not None
        job_id = proc.stdout.readline().strip()
        deadline = time.monotonic() + 30
        # The child is another process: polling its effect on the site is the only signal.
        while sum(site.hits_under("/site/").values()) < 3 and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
        proc.kill()  # no shutdown, no cleanup: TerminateProcess on Windows, SIGKILL elsewhere
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    if proc.stdout:
        proc.stdout.close()

    e = await env(host_min_interval_s=0)
    info = await e.jobs.info(job_id)
    assert info is not None and info.status == "running"  # nobody got to mark it otherwise
    assert info.stats.pages_fetched >= 1
    assert await e.jobs.resume() == 1
    await e.jobs.wait(job_id, 30)
    r = await e.jobs.result(job_id, 500, 0)
    assert r is not None and r.job.status == "done"
    assert r.job.stats.pages_fetched == 12
    refetched = sum(n - 1 for n in site.hits_under("/site/").values() if n > 1)
    assert refetched <= 2


async def test_a_crash_in_one_worker_stops_the_whole_job(
    make_site, env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from weft.jobs import crawl as crawl_module

    real = crawl_module.Crawl._judge
    calls = 0

    async def flaky(self, item, page, final):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("boom")
        return await real(self, item, page, final)

    monkeypatch.setattr(crawl_module.Crawl, "_judge", flaky)
    site: FixtureSite = make_site()
    e = await env(host_min_interval_s=0.1)
    r = await e.crawl(seed_urls=[site.url("/site/")], max_pages=100)
    assert r.job.status == "failed"
    assert r.job.error == "internal error: RuntimeError"
    hits = site.total_hits
    await asyncio.sleep(0.5)
    assert site.total_hits == hits  # no worker outlived the job


async def test_a_shorter_path_to_a_pending_url_improves_it(tmp_path: Path) -> None:
    from weft.frontier.store import Frontier, NewUrl

    db = await Database.open(tmp_path / "weft.db")
    try:
        await db.run(
            lambda c: c.execute(
                "INSERT INTO jobs(id, kind, status, params, created_at, updated_at) "
                "VALUES ('j', 'crawl', 'running', '{}', 1, 1)"
            ).connection.commit()
        )
        f = Frontier(db, "j")
        await f.add([NewUrl("http://a.test/x", 5, 0.2, None, None, run=4)])
        await f.add([NewUrl("http://a.test/x", 2, 0.1, None, None, run=1)])
        item = await f.claim()
        assert item is not None
        assert (item.depth, item.score, item.run) == (2, 0.2, 1)
        await f.add([NewUrl("http://a.test/x", 0, 0.9, None, None)])  # taken: left alone
        assert await f.state_of("http://a.test/x") == "fetching"
        assert await f.claim() is None
    finally:
        await db.close()
