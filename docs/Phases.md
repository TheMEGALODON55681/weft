# Phases

Build order is fixed. Each phase is finished, tested and documented before the next one starts.

| # | Phase | Status |
| --- | --- | --- |
| 1 | Skeleton: config, logging, SQLite schema, `fetch_page` over plain HTTP, stdio MCP server, tests | Done |
| 2 | Safety and politeness: robots.txt, rate limits, SSRF guard, retries, circuit breaker | Done |
| 3 | Rendering: Playwright pool, auto-escalation policy | Done |
| 4 | Crawling: frontier, goal scoring, dedupe, `crawl_site`, `map_site`, jobs, resume | Next |
| 5 | Retrieval: passage ranking, `search_in_site`, schema `extract` | Not started |
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
- The `jobs`, `frontier` and `pages` tables exist and nothing uses them. Phases 4 and 5.
- Conditional requests and the TTL page cache. Phase 4, when crawls repeat fetches.

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
- Per-host state in the limiter and breaker is never evicted. Needs an LRU once crawls span many hosts. Phase 4.
- Limits are per host name. Two names for one server are not merged. Phase 4 can key on the resolved address if that matters.
- robots.txt matching compares paths as sent, without percent-encoding normalisation. Rare in practice. Phase 7.
- The `Sitemap:` lines in robots.txt are ignored. `map_site` will want them. Phase 4.
- A wait longer than `total_timeout_s` is refused. A long crawl job will want its own, longer budget. Phase 4.

## Phase 3: done

Delivered:

- `GuardedProxy` (`fetch/proxy.py`), a local HTTP proxy that Chromium is forced through. Every connection it opens goes through the same `GuardedBackend` as plain HTTP, so sub-resources, redirects between them, WebSockets and workers get the same address check and the same DNS rebinding protection.
- `BrowserPool` (`fetch/browser.py`): one headless Chromium reused across renders, a fresh context per page, at most `browser_max_pages` pages at once, replacement after `browser_recycle_after` pages, a JavaScript heap cap per tab, a timeout per render, recovery from crashed and spinning tabs, and a clean shutdown that leaves no process behind.
- Browser navigations go through the Fetcher one hop at a time, so robots.txt, host spacing and the circuit breaker apply to frames, script navigations and every redirect hop.
- Chromium cannot resolve names itself, cannot bypass the proxy for loopback, cannot send WebRTC UDP around it, and cannot register service workers.
- The render policy. `render="auto"` renders a page that has a script and either thin text or a `<noscript>` asking for JavaScript, and falls back to the HTTP result if rendering fails. `render="always"` renders every HTML page and reports failures. `render="never"` never starts a browser.
- 361 tests, ruff and strict mypy clean, wheel builds with the new modules and the Playwright dependency.

Left for later:

- Sub-resources are not paced and have no byte budget per render, only a time budget. Phase 7 load tests will show whether that matters.
- The proxy on loopback has no authentication. It only reaches addresses the guard allows. Phase 6 should check this inside the container, where other processes share the loopback interface.
- `browser_js_heap_mb` caps JavaScript, not DOM, image or GPU memory. A child-process memory limit (a job object on Windows, cgroups in Docker) would be the hard cap. Phase 6 for Docker.
- The path that replaces a browser whose context will not close has no test, because nothing in the fixture makes `close` hang.
- A rendered page runs without the origin's response headers, Content-Security-Policy included, so its DOM can differ from what a real visitor sees.
- Rendering repeats the HTTP/2 gap: the proxy speaks HTTP/1.1 to Chromium, while upstream HTTPS is a byte tunnel that Chromium negotiates itself.

## Phase 4: next

Scope: the frontier, goal scoring, dedupe, `crawl_site`, `map_site`, jobs and resume.

Starting points left by Phases 2 and 3:

- Per-host limiter and breaker state is never evicted. A crawl needs an LRU.
- A wait longer than `total_timeout_s` is refused. A crawl job wants its own, longer budget.
- `map_site` will want the `Sitemap:` lines of robots.txt, which are ignored today.
- `get_page` already reports `fetch_method`, so the page cache can store which path produced a page. Rendering costs seconds, so the cache should keep rendered results.
- A crawl that renders should share the one `BrowserPool`, whose `browser_max_pages` then caps how many crawl pages render at once.
