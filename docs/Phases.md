# Phases

Build order is fixed. Each phase is finished, tested and documented before the next one starts.

| # | Phase | Status |
| --- | --- | --- |
| 1 | Skeleton: config, logging, SQLite schema, `fetch_page` over plain HTTP, stdio MCP server, tests | Done |
| 2 | Safety and politeness: robots.txt, rate limits, SSRF guard, retries, circuit breaker | Done |
| 3 | Rendering: Playwright pool, auto-escalation policy | Done |
| 4 | Crawling: frontier, goal scoring, dedupe, `crawl_site`, `map_site`, jobs, resume | Done |
| 5 | Retrieval: passage ranking, `search_in_site`, schema `extract` | Next |
| 6 | Remote: streamable HTTP transport, bearer auth, Docker, `/healthz` | Not started |
| 7 | Hardening: load test, fuzzing, docs polish | Not started |

## Phase 1: done

Delivered:

- Typed settings from environment, TOML file and defaults (`config.py`)
- JSON-lines logging to stderr with secret redaction (`obs/logging.py`)
- SQLite in WAL mode with versioned migrations and the version 1 schema (`db.py`)
- A fetcher with manual redirect handling, byte and time caps, and per-hop URL validation (`fetch/http.py`)
- Encoding recovery, HTML-to-Markdown extraction, metadata and link extraction (`extract/`)
- `fetch_page` as an MCP tool over stdio, with stable error codes (`server/`)
- 96 tests, ruff and strict mypy clean, wheel builds and installs into a fresh environment

Gaps Phase 1 left, and what became of them:

- Extraction had no timeout. Closed in Phase 2, with one ceiling: see below.
- The README warned that there was no SSRF guard. Removed in Phase 2, once the guard and its tests were in.
- HTTP/2 is configured but no test exercises it. Phase 2 added a TLS fixture, but it speaks HTTP/1.1 over TLS, so HTTP/2 still needs a server that negotiates `h2`. Still open, Phase 7.
- `render="always"` returned `render_unavailable`. Closed in Phase 3.
- The `jobs`, `frontier` and `pages` tables exist and nothing uses them. Closed in Phase 4, which migrated them to version 2.
- Conditional requests and the TTL page cache. Closed in Phase 4.

## Phase 2: done

Delivered:

- SSRF guard (`fetch/ssrf.py`). Names are resolved in a wrapper around the HTTP client's connect step, every address is checked, and the connection goes to a checked address. Redirect hops, robots.txt reads and DNS rebinding are covered by the same code path. Non-public ranges, cloud metadata addresses and the IPv6 forms that wrap them are refused. `allow_loopback` is the one explicit opening.
- robots.txt (`fetch/robots.py`): its own RFC 9309 parser and matcher, a per-origin cache, `Crawl-delay`, checked on every redirect hop.
- Pacing and caps (`fetch/limits.py`): per-host spacing and concurrency, a global concurrency cap, and fast refusal when a wait would outlast the call.
- Retries with exponential backoff, jitter and `Retry-After` (`fetch/retry.py`, `fetch/http.py`).
- A circuit breaker per host that also stops requests already queued for it.
- A timeout on page extraction, with a worker pool of its own.
- A TLS fixture and tests showing that HTTPS still verifies the certificate against the host name while the connection goes to a pinned address.
- 312 tests, ruff and strict mypy clean, wheel builds and installs into a fresh environment, and the installed server refuses metadata and private addresses with default settings.
- Proxy environment variables are ignored, because a proxy would route around the address check.

Left for later:

- HTTP/2 through the guarded transport is untested. The TLS fixture exists but does not negotiate `h2`. Phase 7.
- A page that hangs extraction frees the caller but not the thread. Running extraction in a child process that can be killed is the fix. Phase 7, if it shows up in load tests.
- Per-host state in the limiter and breaker is never evicted. Closed in Phase 4: an LRU that never drops a busy host.
- Limits are per host name. Two names for one server are not merged. Phase 4 did not need it. Still open, Phase 7 if load tests show aliases matter.
- robots.txt matching compares paths as sent, without percent-encoding normalisation. Rare in practice. Phase 7.
- The `Sitemap:` lines in robots.txt are ignored. Closed in Phase 4.
- A wait longer than `total_timeout_s` is refused. Closed in Phase 4: crawl fetches get `crawl_fetch_timeout_s`, and the job has `crawl_timeout_s`.

## Phase 3: done

Delivered:

- `GuardedProxy` (`fetch/proxy.py`), a local HTTP proxy that Chromium is forced through. Every connection it opens goes through the same `GuardedBackend` as plain HTTP, so sub-resources, redirects between them, WebSockets and workers get the same address check and the same DNS rebinding protection.
- `BrowserPool` (`fetch/browser.py`): one headless Chromium reused across renders, a fresh context per page, at most `browser_max_pages` pages at once, replacement after `browser_recycle_after` pages, a JavaScript heap cap per tab, a timeout per render, recovery from crashed and spinning tabs, and a clean shutdown that leaves no process behind.
- Browser navigations go through the Fetcher one hop at a time, so robots.txt, host spacing and the circuit breaker apply to frames, script navigations and every redirect hop.
- Chromium cannot resolve names itself, cannot bypass the proxy for loopback, cannot send WebRTC UDP around it, and cannot register service workers.
- The render policy. `render="auto"` renders a page that has a script and either thin text or a `<noscript>` asking for JavaScript, and falls back to the HTTP result if rendering fails. `render="always"` renders every HTML page and reports failures. `render="never"` never starts a browser.
- 361 tests, ruff and strict mypy clean, wheel builds with the new modules and the Playwright dependency.

Left for later:

- Sub-resources are not paced and have no byte budget per render, only a time budget. Closed in Phase 4: each render has its own proxy, with a byte budget and connection spacing per host.
- The proxy on loopback has no authentication. It only reaches addresses the guard allows. Phase 4 made the per-render proxies short-lived, but the browser-wide one remains. Phase 6 should check this inside the container, where other processes share the loopback interface.
- `browser_js_heap_mb` caps JavaScript, not DOM, image or GPU memory. A child-process memory limit (a job object on Windows, cgroups in Docker) would be the hard cap. Phase 6 for Docker.
- The path that replaces a browser whose context will not close has no test, because nothing in the fixture makes `close` hang.
- The render tests in `tests/test_page.py` failed instead of skipping without Chromium. Closed in Phase 4.
- Playwright was allowed to float across minor releases, each of which needs its own Chromium build. Closed in Phase 4: pinned to 1.65.
- A rendered page runs without the origin's response headers, Content-Security-Policy included, so its DOM can differ from what a real visitor sees.
- Rendering repeats the HTTP/2 gap: the proxy speaks HTTP/1.1 to Chromium, while upstream HTTPS is a byte tunnel that Chromium negotiates itself.

## Phase 4: done

Delivered:

- URL canonicalisation (`frontier/canon.py`): case, trailing dots, IDN, default ports, dot segments, percent-encoding, query order, tracking parameters and fragments.
- A SQLite frontier (`frontier/store.py`), best score first, where every step commits on its own.
- Goal scoring (`frontier/score.py`, `rank/bm25.py`): links scored by anchor text, URL words and the parent page's relevance, pages scored by BM25 against the goal. Breadth-first when there is no goal.
- Dedupe: exact text hash, 64-bit simhash for near duplicates, `<link rel="canonical">`, and redirect targets as aliases.
- Trap limits: depth, page budget, at most ten links in a row with their parent's URL pattern, no repeated path segments, a URL length cap.
- `crawl_site`, `get_job`, `cancel_job`, `list_jobs` over MCP, with progress notifications, per-job metrics (pages, bytes, errors by code, latency percentiles, running time), a slot limit on running jobs, and resume after a stop or a kill.
- `map_site`: robots.txt `Sitemap:` lines or `/sitemap.xml`, sitemap indexes, gzip sitemaps, links on the start page, and a robots verdict per URL.
- A page cache in SQLite with a TTL and `ETag` and `Last-Modified` revalidation, used by `fetch_page`, crawls and `map_site`.
- Carried gaps closed: an LRU for limiter and breaker state, a crawl time budget separate from `total_timeout_s`, render tests that skip without Chromium, Playwright pinned to its minor release, and a byte budget and connection spacing for each render's sub-resources.
- 461 tests, ruff and strict mypy clean, wheel builds. 26 hand-made mutants of the Phase 4 code are all caught by the tests.

Left for later:

- Relevance uses BM25 corpus statistics gathered during the crawl. A resumed job starts them again, so scores before and after a restart differ a little.
- Near-duplicate detection compares against every kept page: linear in the size of the crawl. Fine for thousands of pages. Bucketing by bit bands is the upgrade.
- The pattern-run cap stops long legitimate pagination after ten pages in a row. A crawl that needs page 50 of a listing has to seed it.
- Jobs, frontier rows and cache rows are never deleted. Needs a retention setting. Phase 7.
- The number of queued jobs is unbounded. Phase 6 needs a cap per caller before the server goes on a network.
- The page cache is shared by all callers, which is safe only while Weft sends no cookies. Authenticated crawls must partition or skip it.
- `Cache-Control: no-store` is not honoured by the page cache.
- Sitemaps with namespace prefixes on their tags (`<s:loc>`) are not read.
- `same_domain_only` treats `www.` as the same site and every other subdomain as a different one. A public-suffix-aware site key would be more exact.
- Two servers on one data directory would both resume the same jobs.

## Phase 5: next

Scope: passage ranking, `search_in_site`, schema `extract`.

Starting points left by Phase 4:

- `rank/bm25.py` holds BM25 with a growing corpus and a small stemmer. Passage ranking can reuse it on passages instead of pages.
- `search_in_site` can run as a crawl job with the query as its goal. The pages it keeps are in the frontier, and their full text is in the page cache.
- `extract(url_or_job, schema)` can read a job's pages from the cache by URL, as `fetch_page` does after a crawl.
- The cache is keyed by render mode. A search that renders differently from the crawl that filled the cache will fetch again.
