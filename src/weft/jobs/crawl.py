"""One crawl: best-first over the job's frontier, with dedupe and trap limits.

Workers take the best pending URL, fetch it through get_page (so the cache, the render policy,
robots.txt, pacing and the address guard all apply), record the outcome, and add the page's
links with their goal scores. The crawl stops when the frontier is empty, `max_pages` URLs have
been taken, or the time budget runs out.

Traps are bounded four ways: `max_depth`, `max_pages`, a cap on how many links in a row may
share their parent's URL pattern (calendars, endless pagination, counters in paths), and a
refusal of URLs whose path repeats a segment (relative-link loops) or that are very long. Goal
scoring adds a fifth in practice: trap pages say nothing about the goal, so they sink.
"""

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from urllib.parse import urlsplit

from weft.config import Settings
from weft.db import Database
from weft.fetch.browser import BrowserPool
from weft.fetch.cache import PageCache
from weft.fetch.errors import FetchError
from weft.fetch.http import Fetcher
from weft.fetch.page import get_page
from weft.frontier.canon import canonicalize, pattern, repeats_segments, site_key
from weft.frontier.dedupe import (
    MIN_WORDS,
    NEAR_BITS,
    content_hash,
    distance,
    from_signed,
    simhash,
    to_signed,
    words,
)
from weft.frontier.score import link_score, relevance
from weft.frontier.store import Frontier, Item, NewUrl, Outcome
from weft.models import CrawlParams, PageResult
from weft.obs.logging import safe_url
from weft.rank.bm25 import Bm25, terms

log = logging.getLogger("weft.crawl")

MAX_PATTERN_RUN = 10  # links in a row that share their parent's URL pattern
MAX_CRAWL_URL = 2048
SNIPPET_CHARS = 280


@dataclass(frozen=True, slots=True)
class CrawlDeps:
    settings: Settings
    db: Database
    fetcher: Fetcher
    browser: BrowserPool | None
    cache: PageCache | None


def _host(url: str) -> str:
    return urlsplit(url).hostname or ""


class Crawl:
    def __init__(self, deps: CrawlDeps, job_id: str, params: CrawlParams) -> None:
        self._deps = deps
        self._job = job_id
        self._p = params
        self._frontier = Frontier(deps.db, job_id)
        self._goal = Bm25(params.goal)
        self._seeds = [canonicalize(u) for u in params.seed_urls]
        self._sites = {site_key(_host(u)) for u in self._seeds}
        self._hashes: dict[str, str] = {}  # content hash -> first URL with it
        self._simhashes: list[tuple[int, str]] = []
        self._deadline = 0.0
        self._started = 0.0
        self._run_before = 0.0

    async def run(self, run_before_s: float) -> str:
        """Crawl until done and return the stop reason. `run_before_s` is the running time of
        earlier runs of this job, which counts against the time budget."""
        budget = self._deps.settings.crawl_timeout_s - run_before_s
        self._run_before = run_before_s
        self._started = time.monotonic()
        self._deadline = self._started + budget
        await self._frontier.reopen()
        # YAGNI note: the duplicate check is a linear scan of every kept page's simhash, fine for
        # thousands of pages; bucket by bit bands if crawls grow to millions.
        for url, h, sim in await self._frontier.fingerprints():
            if h:
                self._hashes.setdefault(h, url)
            if sim is not None:
                self._simhashes.append((from_signed(sim), url))
        await self._frontier.add([NewUrl(u, 0, 1.0, None, None) for u in self._seeds])
        if budget <= 0:
            return "time_budget"
        try:
            async with asyncio.timeout(budget) as deadline:
                reason = await self._work()
        except TimeoutError:
            if not deadline.expired():
                raise  # not ours
            reason = "time_budget"
        await self._frontier.reopen()  # rows cut off by the deadline go back to pending
        await self._save_run_time()
        return reason

    async def _work(self) -> str:
        cond = asyncio.Condition()
        taken = await self._frontier.taken()
        in_flight = 0

        async def worker() -> None:
            nonlocal taken, in_flight
            while True:
                async with cond:
                    while True:
                        if taken >= self._p.max_pages:
                            return
                        item = await self._frontier.claim()
                        if item is not None:
                            taken += 1
                            in_flight += 1
                            break
                        if in_flight == 0:  # nothing pending and nobody left to add more
                            cond.notify_all()
                            return
                        await cond.wait()
                try:
                    await self._process(item)
                finally:
                    async with cond:
                        in_flight -= 1
                        cond.notify_all()

        n = min(self._deps.settings.crawl_concurrency, self._p.max_pages)
        # A TaskGroup, not gather: if one worker fails, the others must stop too, or they would
        # keep crawling for a job already marked failed.
        async with asyncio.TaskGroup() as tg:
            for _ in range(n):
                tg.create_task(worker())
        return "max_pages" if taken >= self._p.max_pages else "exhausted"

    async def _save_run_time(self) -> None:
        run_s = self._run_before + time.monotonic() - self._started
        now = time.time()

        def work(c: sqlite3.Connection) -> None:
            with c:
                c.execute(
                    "UPDATE jobs SET run_s = ?, updated_at = ? WHERE id = ?",
                    (run_s, now, self._job),
                )

        await self._deps.db.run(work)

    def _in_scope(self, url: str) -> bool:
        if len(url) > MAX_CRAWL_URL or repeats_segments(url):
            return False
        if self._p.same_domain_only and site_key(_host(url)) not in self._sites:
            return False
        if self._p.include_patterns and not any(
            fnmatchcase(url, pat) for pat in self._p.include_patterns
        ):
            return False
        return not any(fnmatchcase(url, pat) for pat in self._p.exclude_patterns)

    async def _process(self, item: Item) -> None:
        d = self._deps
        start = time.perf_counter()
        left = self._deadline - time.monotonic()
        timeout = min(d.settings.crawl_fetch_timeout_s, max(1.0, left))
        try:
            page = await get_page(
                d.fetcher, d.settings, item.url, self._p.render, d.browser, d.cache, timeout
            )
        except FetchError as exc:
            elapsed = (time.perf_counter() - start) * 1000
            outcome = Outcome("failed", error=exc.code, elapsed_ms=elapsed)
            await self._frontier.finish(item.url, outcome)
            await self._save_run_time()
            return
        try:
            final = canonicalize(page.final_url)
        except ValueError:
            final = item.url
        if final != item.url:
            await self._frontier.mark_alias(final, item.url)
        outcome = await self._judge(item, page, final)
        await self._frontier.finish(item.url, outcome)
        if outcome.state == "done" and item.depth < self._p.max_depth:
            await self._expand(item, page, outcome.relevance or 0.0)
        await self._save_run_time()
        log.info(
            "crawled",
            extra={"job": self._job, "url": safe_url(item.url), "state": outcome.state},
        )

    async def _judge(self, item: Item, page: PageResult, final: str) -> Outcome:
        seen = Outcome(
            "done",
            final_url=page.final_url,
            status=page.status,
            title=page.title,
            bytes=page.body_bytes,
            elapsed_ms=page.elapsed_ms,
            cached=page.cached,
        )
        if page.status >= 400:
            return replace(seen, state="failed", error=f"http_{page.status}")

        dup_of = await self._canonical_target(item, page, final)
        h = sim = None
        if dup_of is None and page.markdown.strip():
            h = content_hash(page.markdown)
            dup_of = self._hashes.get(h)
            if dup_of is None and len(words(page.markdown)) >= MIN_WORDS:
                sim = simhash(page.markdown)
                dup_of = next(
                    (u for s, u in self._simhashes if distance(s, sim) <= NEAR_BITS), None
                )
        if dup_of is not None:
            return replace(seen, state="duplicate", duplicate_of=dup_of)

        if h is not None:
            self._hashes[h] = item.url
        if sim is not None:
            self._simhashes.append((sim, item.url))
        rel = relevance(self._goal, terms(f"{page.title or ''}\n{page.markdown}"))
        return replace(
            seen,
            snippet=" ".join(page.markdown.split())[:SNIPPET_CHARS],
            relevance=rel,
            content_hash=h,
            simhash=None if sim is None else to_signed(sim),
        )

    async def _canonical_target(self, item: Item, page: PageResult, final: str) -> str | None:
        """A page whose canonical link names another in-scope URL is an alias of that URL. The
        target is queued if it is new. If the target is itself recorded as a duplicate (two pages
        naming each other), the link is ignored, so one of them is kept."""
        raw = page.metadata.canonical_url
        if not raw:
            return None
        try:
            target = canonicalize(raw)
        except ValueError:
            return None
        if target in (item.url, final) or not self._in_scope(target):
            return None
        state = await self._frontier.state_of(target)
        if state == "duplicate":
            return None
        if state is None:
            await self._frontier.add([NewUrl(target, item.depth, item.score, item.url, None)])
        return target

    async def _expand(self, item: Item, page: PageResult, rel: float) -> None:
        fetcher = self._deps.fetcher
        parent_pattern = pattern(item.url)
        new: list[NewUrl] = []
        for link in page.links:
            if link.nofollow:
                continue
            try:
                url = canonicalize(link.url)
            except ValueError:
                continue
            if not self._in_scope(url):
                continue
            run = item.run + 1 if pattern(url) == parent_pattern else 0
            if run > MAX_PATTERN_RUN:
                continue
            # Only rules already cached: reading robots.txt for every host a page links to
            # would cost more than the refusals it saves. The fetch checks again regardless.
            rules = fetcher.cached_robots_rules(url)
            parts = urlsplit(url)
            target = parts.path + (f"?{parts.query}" if parts.query else "")
            if rules is not None and not rules.allows(target):
                continue
            score = link_score(self._goal, link.text, url, rel, item.depth + 1)
            new.append(NewUrl(url, item.depth + 1, score, item.url, link.text or None, run))
        if new:
            await self._frontier.add(new)
