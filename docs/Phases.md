# Phases

Build order is fixed. Each phase is finished, tested and documented before the next one starts.

| # | Phase | Status |
| --- | --- | --- |
| 1 | Skeleton: config, logging, SQLite schema, `fetch_page` over plain HTTP, stdio MCP server, tests | Done |
| 2 | Safety and politeness: robots.txt, rate limits, SSRF guard, retries, circuit breaker | Done |
| 3 | Rendering: Playwright pool, auto-escalation policy | Next |
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

Gaps Phase 1 left, and what became of them:

- Extraction had no timeout. Closed in Phase 2, with one ceiling: see below.
- The README warned that there was no SSRF guard. Removed in Phase 2, once the guard and its tests were in.
- HTTP/2 is configured but no test exercises it. Phase 2 added a TLS fixture, but it speaks HTTP/1.1 over TLS, so HTTP/2 still needs a server that negotiates `h2`. Still open, Phase 7.
- `render="always"` returns `render_unavailable`. Phase 3.
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

## Phase 3: next

Scope: a Playwright pool, reuse, hard memory and page limits, and the policy that escalates from plain HTTP to the browser only when the extracted text is thin or the page is a JavaScript shell.

Starting points left by Phase 2:

- **Chromium does its own DNS and its own connections, so `fetch/ssrf.py` does not protect it.** Every request the browser makes, including sub-resources and redirects, has to go through an address check before it leaves, by request interception or a local proxy that applies `is_blocked`. This is the main safety task of the phase. Test it with the same redirect and rebinding cases used for plain HTTP.
- The browser's navigations should pass through the same robots.txt cache, host limiter and breaker, so a rendered fetch is as polite as a plain one. The `Fetcher` owns those objects. Share them, do not build second copies.
- Rendering budgets sit inside `total_timeout_s`, or get their own setting with the same refusal behaviour.
- The render policy keys on word count. Phase 1 found that trafilatura returns navigation text for a page with no article, so a JavaScript shell comes back thin, not empty.
- The fixture site needs the JavaScript-rendered page that the architecture notes already promise.
