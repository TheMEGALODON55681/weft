# Phases

Build order is fixed. Each phase is finished, tested and documented before the next one starts.

| # | Phase | Status |
| --- | --- | --- |
| 1 | Skeleton: config, logging, SQLite schema, `fetch_page` over plain HTTP, stdio MCP server, tests | Done |
| 2 | Safety and politeness: robots.txt, rate limits, SSRF guard, retries, circuit breaker | Next |
| 3 | Rendering: Playwright pool, auto-escalation policy | Not started |
| 4 | Crawling: frontier, goal scoring, dedupe, `crawl_site`, `map_site`, jobs, resume | Not started |
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

Left for later phases:

- HTTP/2 is configured but no test exercises it. The fixture server speaks cleartext HTTP/1.1, so HTTP/2 needs a TLS fixture. Phase 7.
- `render="always"` returns `render_unavailable`. Phase 3.
- The `jobs`, `frontier` and `pages` tables exist and nothing uses them. Phases 4 and 5.
- Conditional requests and the TTL page cache. Phase 4, when crawls repeat fetches.

## Phase 2: next

Scope: robots.txt (cached per host, crawl-delay honoured), per-domain rate limit and concurrency cap, a global concurrency cap, SSRF guard, retries with backoff and jitter, circuit breaker per host.

Starting points left by Phase 1:

- The redirect loop in `Fetcher._fetch` is the place for the per-hop address check. Resolve DNS, check the resolved IP against private, loopback, link-local and metadata ranges, and connect to that IP so the check and the connection cannot disagree.
- The test suite fetches `127.0.0.1`. The SSRF guard will block that, so tests need an explicit setting that allows loopback, and SSRF tests must run with it off.
- MCP clients can call several tools at once. Nothing limits parallel fetches to one host yet.
- Extraction has no timeout. A pathological page could hold a worker thread. Phase 2 or 7 should bound it.
