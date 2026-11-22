"""Load tests against the fixture site: a burst of fetches, a large crawl and many MCP clients at
once. They check what a few requests cannot: that every limit holds under pressure, that nothing is
fetched twice or lost, that memory and threads settle, and that the server stays healthy.

The size is WEFT_LOAD_PAGES, 400 by default so the ordinary suite stays quick. For a heavy run:

    WEFT_LOAD_PAGES=5000 uv run pytest tests/test_load.py -s

`-s` shows the report each test prints (throughput, latency percentiles, memory, database size).
The thresholds are loose on purpose: they catch a leak, a stall or a broken cap, not a slow day.
"""

import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from fastmcp import Client

from fixture_site import FixtureSite
from http_app import TOKEN, remote, serve
from rss import rss_bytes
from weft.config import Settings
from weft.db import Database
from weft.fetch.cache import PageCache
from weft.fetch.http import Fetcher
from weft.jobs.crawl import CrawlDeps
from weft.jobs.manager import JobManager
from weft.models import CrawlParams

PAGES = int(os.environ.get("WEFT_LOAD_PAGES", "400"))
MB = 1024 * 1024


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def report(name: str, **numbers: object) -> None:
    print(f"\n[load] {name}: " + ", ".join(f"{k}={v}" for k, v in numbers.items()))


async def test_a_burst_of_fetches_keeps_to_its_concurrency_cap(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site(load_pages=PAGES)
    fetcher = fetch_with(
        host_concurrency=8, global_concurrency=8, host_min_interval_s=0, total_timeout_s=300
    )
    start = time.perf_counter()
    results = await asyncio.gather(*(fetcher.fetch(site.url(f"/load/{i}")) for i in range(PAGES)))
    elapsed = time.perf_counter() - start

    assert [r.status for r in results] == [200] * PAGES
    assert sum(site.hits_under("/load/").values()) == PAGES  # each page once, no retry
    assert site.load_peak <= 8  # never past the cap, however fast the client is
    latency = [r.elapsed_ms for r in results]
    report(
        "burst of fetches",
        pages=PAGES,
        seconds=round(elapsed, 2),
        per_second=round(PAGES / elapsed),
        peak_in_flight=site.load_peak,
        p50_ms=round(percentile(latency, 0.5)),
        p95_ms=round(percentile(latency, 0.95)),
        p99_ms=round(percentile(latency, 0.99)),
    )


async def test_a_large_crawl_visits_every_page_once_and_settles(
    make_site: Callable[..., FixtureSite], tmp_path: Path
) -> None:
    site = make_site(load_pages=PAGES)
    data_dir = tmp_path / "load"
    settings = Settings(
        data_dir=data_dir,
        crawl_concurrency=16,
        host_concurrency=16,
        global_concurrency=16,
        host_min_interval_s=0,
        crawl_timeout_s=3600,
        total_timeout_s=60,
    )
    db = await Database.open(settings.db_path)
    fetcher = Fetcher(settings)
    jobs = JobManager(CrawlDeps(settings, db, fetcher, None, PageCache(db, 3600)))
    tasks_before = len(asyncio.all_tasks())
    rss_before = rss_bytes()
    try:
        start = time.perf_counter()
        job_id = await jobs.start_crawl(
            CrawlParams(
                seed_urls=[site.url("/load/0")], max_pages=PAGES + 10, max_depth=30, render="never"
            )
        )
        await jobs.wait(job_id, 3600)
        elapsed = time.perf_counter() - start
        result = await jobs.result(job_id, 5, 0)
        assert result is not None
        rss_after = rss_bytes()
        db_bytes = sum(f.stat().st_size for f in data_dir.glob("weft.db*"))
    finally:
        await jobs.aclose()
        await fetcher.aclose()
        await db.close()

    stats = result.job.stats
    grew_mb = None
    if rss_before is not None and rss_after is not None:
        grew_mb = round((rss_after - rss_before) / MB)
    report(
        "crawl",
        pages=PAGES,
        seconds=round(elapsed, 2),
        per_second=round(PAGES / elapsed),
        peak_in_flight=site.load_peak,
        p50_ms=stats.latency_ms_p50,
        p95_ms=stats.latency_ms_p95,
        max_ms=stats.latency_ms_max,
        db_mb=round(db_bytes / MB, 1),
        rss_grew_mb=grew_mb,
    )

    assert result.job.status == "done"
    assert result.job.stop_reason == "exhausted"
    assert (stats.pages_ok, stats.duplicates, stats.failed, stats.pending) == (PAGES, 0, 0, 0)
    assert sum(site.hits_under("/load/").values()) == PAGES  # each page once
    # Extraction, not the network, paces a crawl, so few requests overlap. The cap still holds.
    assert site.load_peak <= 16
    assert len(asyncio.all_tasks()) == tasks_before  # nothing left running behind it
    assert db_bytes < PAGES * 40_000
    if grew_mb is not None:
        assert grew_mb < 150 + PAGES // 10, f"resident memory grew by {grew_mb} MB"


async def test_many_mcp_clients_over_http_with_the_server_staying_healthy(
    make_site: Callable[..., FixtureSite], tmp_path: Path
) -> None:
    site = make_site()
    settings = remote(tmp_path, host_min_interval_s=0, host_concurrency=2)
    clients, calls = 8, 3
    health: list[float] = []

    async def one_client(n: int, base: str) -> None:
        async with Client(f"{base}/mcp", auth=TOKEN) as client:
            for k in range(calls):  # /hold answers after 0.3 s, and each URL is new to the cache
                out = await client.call_tool("fetch_page", {"url": site.url(f"/hold?i={n}-{k}")})
                assert out.structured_content is not None
                assert out.structured_content["status"] == 200

    async def watch(base: str, stop: asyncio.Event) -> None:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as c:
            while not stop.is_set():
                start = time.perf_counter()
                assert (await c.get(f"{base}/healthz")).status_code == 200
                health.append(time.perf_counter() - start)
                await asyncio.sleep(0.1)

    async with serve(settings) as base:
        stop = asyncio.Event()
        watcher = asyncio.create_task(watch(base, stop))
        start = time.perf_counter()
        await asyncio.gather(*(one_client(n, base) for n in range(clients)))
        elapsed = time.perf_counter() - start
        stop.set()
        await watcher

    assert site.peak == 2  # eight clients, and still never more than the host's two slots
    assert len(health) >= 5  # each answer was a 200, which means under the two second limit
    report(
        "mcp clients over http",
        clients=clients,
        fetches=clients * calls,
        seconds=round(elapsed, 2),
        peak_in_flight=site.peak,
        healthz_checks=len(health),
        healthz_max_ms=round(max(health) * 1000),
    )
