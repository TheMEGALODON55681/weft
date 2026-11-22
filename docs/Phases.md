# Phases

Build order is fixed. Each phase is finished, tested and documented before the next one starts.

| # | Phase | Status |
| --- | --- | --- |
| 1 | Skeleton: config, logging, SQLite schema, `fetch_page` over plain HTTP, stdio MCP server, tests | Done |
| 2 | Safety and politeness: robots.txt, rate limits, SSRF guard, retries, circuit breaker | Done |
| 3 | Rendering: Playwright pool, auto-escalation policy | Done |
| 4 | Crawling: frontier, goal scoring, dedupe, `crawl_site`, `map_site`, jobs, resume | Done |
| 5 | Retrieval: passage ranking, `search_in_site`, schema `extract`, optional rerank and model extraction | Done |
| 6 | Remote: streamable HTTP transport, bearer auth, Docker, `/healthz` | Done. CI builds the image and runs the suite in it, and its first run is still to come (see Phases 6 and 7) |
| 7 | Hardening: load test, fuzzing, HTTP/2 test, docs polish | Done |

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
- HTTP/2 is configured but no test exercises it. Phase 2 added a TLS fixture, but it speaks HTTP/1.1 over TLS, so HTTP/2 still needs a server that negotiates `h2`. Closed in Phase 7.
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

- HTTP/2 through the guarded transport is untested. The TLS fixture exists but does not negotiate `h2`. Closed in Phase 7.
- A page that hangs extraction frees the caller but not the thread. Running extraction in a child process that can be killed is the fix. Phase 7 found a page that did this (deep nesting) and capped that case. The rest is still open: see Phase 7.
- Per-host state in the limiter and breaker is never evicted. Closed in Phase 4: an LRU that never drops a busy host.
- Limits are per host name. Two names for one server are not merged. Phase 4 did not need it, and the Phase 7 load tests did not look at it. Still open.
- robots.txt matching compares paths as sent, without percent-encoding normalisation. Rare in practice. Closed in Phase 7.
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
- The proxy on loopback has no authentication. It only reaches addresses the guard allows. Phase 4 made the per-render proxies short-lived, but the browser-wide one remains. Phase 6 should check this inside the container, where other processes share the loopback interface. Phase 6 reasoned about it and could not test it: the container's network namespace holds only Weft and Chromium, and a published port does not reach loopback.
- `browser_js_heap_mb` caps JavaScript, not DOM, image or GPU memory. A child-process memory limit (a job object on Windows, cgroups in Docker) would be the hard cap. Closed for Docker in Phase 6: the README's run command sets `--memory` and `--pids-limit`. A bare host still has no hard cap.
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
- Near-duplicate detection compares against every kept page: linear in the size of the crawl. Fine for thousands of pages. Bucketing by bit bands is the upgrade. The Phase 7 load test saw crawl throughput fall from 43 pages a second at 400 pages to 32 at 5,000 and did not profile why. This scan is one candidate.
- The pattern-run cap stops long legitimate pagination after ten pages in a row. A crawl that needs page 50 of a listing has to seed it.
- Jobs, frontier rows and cache rows are never deleted. Needs a retention setting. Still open after Phase 7, which measured it: about 5 KB of database per crawled page.
- The number of queued jobs is unbounded. Phase 6 needs a cap per caller before the server goes on a network. Still open after Phase 6, with a reason: see below.
- The page cache is shared by all callers, which is safe only while Weft sends no cookies. Authenticated crawls must partition or skip it.
- `Cache-Control: no-store` is not honoured by the page cache.
- Sitemaps with namespace prefixes on their tags (`<s:loc>`) are not read.
- `same_domain_only` treats `www.` as the same site and every other subdomain as a different one. A public-suffix-aware site key would be more exact.
- Two servers on one data directory would both resume the same jobs.
- The anchor-text weight was not tested end to end. The old goal-order test passed with the weight set to zero, because the goal also appears in the page text and URLs. Closed in Phase 5: `test_anchor_text_alone_decides_which_link_is_fetched_first` uses four links that differ only by anchor text, and fails with the weight zeroed, with the scorer given no anchor, and with the crawler passing no anchor.

## Phase 5: done

Delivered:

- Passage splitting and ranking (`rank/passages.py`) on the existing BM25 (`rank/bm25.py`): cut at headings, short lines gathered up to a size, long ones cut at sentence ends, title and heading counted as words of the passage, exact repeats dropped, a snippet around the query words. The tokenizer now reads words in any script that separates them with spaces, where it used to drop everything outside ASCII.
- `search_in_site`: a crawl job with the query as its goal, then a passage search over the pages it kept, returning hits with URL, title, heading, snippet and passage, and saying whether the crawl finished.
- `extract` (`extract/schema.py`, `retrieval.py`): a JSON Schema with `x-css`, `x-xpath` and `x-attr` selectors, run on one lxml tree so CSS and XPath mix, validated with `jsonschema`, over a URL or the pages of a job. Errors name the path and the problem. Limits on schema size, depth, selector length and the text a run may collect.
- A provider interface (`providers.py`) with one HTTP implementation for embeddings and chat, both off by default behind config flags, with the endpoint set by the operator only.
- Optional embedding rerank (`rank/rerank.py`): reciprocal rank fusion of the keyword rank and the similarity rank over the best 50 hits. A provider failure keeps the keyword order.
- Optional model extraction (`extract/llm.py`): asked only when the rules fall short, at most 10 pages per call, and its answer is used only if it validates.
- The page cache now keeps each page's HTML (compressed), and `get_document` returns it, so rules can read the rendered DOM. Schema version 3.
- The Phase 4 anchor-text gap, closed (see Phase 4).
- No network in tests: providers are in-memory fakes or an in-memory HTTP transport.
- Tests, lint, types and the hand-made mutants: see BuildLog.

Corrections to the notes Phase 4 left for this phase:

- `extract` cannot read a job's pages from the cache as Markdown. CSS and XPath need the DOM, and the cache held only the Markdown. The cache now stores the HTML too, at the price of rows about ten times larger. Migration 3 dropped the cached pages to add the column.
- A search that renders differently from the crawl does fetch again. `search_in_site` uses the job's own render mode, so it never does. `extract` over a job does the same.

Left for later:

- No local embedding model, only the HTTP endpoint. A `sentence-transformers` provider would fit behind the same interface, at the price of a large dependency.
- Rerank orders the keyword hits and does not add recall. Passages that share no word with the query are not found.
- Stemming and stop words are English only. Chinese, Japanese and Thai are not segmented. Accents are not folded.
- A passage is dropped as a repeat only when the whole passage repeats. Boilerplate that differs by a word is kept.
- Each `search_in_site` call starts a job and jobs are never deleted. Search with the cache off fetches every page twice, and with `cache_ttl_s = 0` extraction over a job fetches every page again.
- The `pages` table is about ten times larger and is never swept.
- Ranking a very large search happens in one pass under `extract_timeout_s`.
- Agent-supplied XPath and JSON Schema `pattern` values can hold one of the four extraction threads, the same ceiling as Phase 2.
- A model cannot be stopped from putting misleading page text into a field that takes any string. Validation against the schema is the only bound.
- Model and embedding endpoints are not covered by the address guard. They are operator-set. If Weft ever takes endpoints from a caller, they need it.
- The extraction calls of one `extract` call run in sequence.
- A page with an error status is run through the selectors, and through the model if that is on, like any other page. Closed in Phase 6: the selectors still run, and the model is never asked about a page with a status of 400 or more.
- Phase 4 carry-overs are unchanged.

## Phase 6: done

Delivered:

- The streamable HTTP transport beside stdio: `weft --transport http` serves `/mcp` and `/healthz` through fastmcp and uvicorn. The flags `--transport`, `--host` and `--port` set the new settings `transport`, `http_host` and `http_port`, and the same settings come from `WEFT_*` variables and the file.
- Bearer-token auth (`server/auth.py`). The token comes from `WEFT_AUTH_TOKEN`. Weft compares SHA-256 digests with `hmac.compare_digest`. A missing, wrong or malformed token gets `401` and a `WWW-Authenticate: Bearer` header before any tool code runs, on `POST`, `GET` and `DELETE`. A token in the URL is ignored. A rejected token is logged without its value.
- The server refuses to start beyond loopback without a token, at settings validation and with exit code 2. An empty token counts as none, a token under 16 characters or with a space is refused, and no error repeats the value.
- A server bound to loopback with no token checks the `Host` and `Origin` headers, which stops DNS rebinding and cross-origin requests from a web page. A server with a token does not, so a proxy on the same machine can forward under another host name.
- `/healthz`, open, with a fixed body: `ok`, or `503` when `SELECT 1` does not finish in two seconds or the server is not ready.
- One JSON format on stderr for Weft, uvicorn, fastmcp and the MCP SDK. The access log is off.
- A `Dockerfile` with three stages (`base`, `test`, `runtime`). `runtime` is the default, runs as the unprivileged user `weft`, installs `chromium-headless-shell` with its libraries, installs only what `uv.lock` says, exposes 8000, keeps data in `/data`, has a health check and exits with code 2 when started without a token. `.dockerignore` keeps the repository's local state and secrets out of the context. The `test` stage runs the suite against the installed package.
- `uv.lock`, and `fastmcp` held to `>=4.2.2,<5`. The local environment was synced to the lock, which moved `fastmcp` from 4.2.2 to 4.2.4 and eleven other packages by a patch release, and the suite passed on that set.
- README steps for running the server, putting TLS in front, attaching a client, and running the image.
- Closed a Phase 5 gap: an error page is never sent to the model.
- 685 tests, 84 of them new, and strict types and lint clean. 37 hand-made mutants of the Phase 6 code are all caught. Details are in BuildLog.

What was not done, and why:

- **The image was never built and the suite never ran in a container.** Phase 7 wrote the workflow that does both on Linux. It has not run yet. There is no Docker, Podman or Docker Desktop on the machine this phase was built on, and installing one was out of scope. What stands in for it: the Dockerfile's two `uv sync` steps ran in a scratch environment on Windows and installed the project as a wheel, the `HEALTHCHECK` command is run by a test against a real server, and tests pin each requirement of the file (non-root user, headless shell, locked install, no secret). None of that proves the Debian package step, the browser download or `--cap-drop ALL`. Build the image on a machine with Docker before relying on it. `docker build --target test -t weft-test . && docker run --rm weft-test` runs the suite there.
- No hosted connector or third-party MCP client was tried. The HTTP side was exercised with a `fastmcp` client and with raw requests. The README's client steps follow the common configuration shape and have not been run.
- Chromium and Linux behaviour of the whole suite is unverified beyond what ran on Windows.

Left for later:

- A cap on queued jobs. Phase 4 asked for one per caller. The server has one shared token and no caller identity, so a cap would be global, and one token holder could fill it for everyone. It waits until there is more than one holder to protect.
- Per-client tokens, expiry and revocation. Rotating the one token means a restart, which ends every session.
- Weft serves plain HTTP. TLS is the reverse proxy's job.
- No rate limit or lockout on wrong tokens. A proxy can add one.
- `/healthz` queries the database without a token, and a wedged database makes each probe queue behind it.
- Playwright starts Chromium with its sandbox off. The container is the boundary in the image.
- The image names its base by tag, and Debian's packages are not pinned.
- The loopback check trusts the name `localhost` without resolving it. A hosts file that maps it elsewhere would expose a tokenless server to that address.
- Phase 7 did the HTTP/2 test, the load test and fuzzing. Retention for jobs and cache rows, and the carry-overs from Phases 2 to 5 that Phase 7 does not name, stay open.

## Phase 7: done

Delivered:

- A GitHub Actions workflow (`.github/workflows/ci.yml`) with three independent jobs on `ubuntu-latest`. `lint` runs `ruff check` (with GitHub annotations), `ruff format --check --diff`, `uv lock --check` and `mypy`. `test` installs headless Chromium and runs the whole suite. `docker` builds the `test` stage and runs the suite inside it, then builds the runtime image and checks that it exits with code 2 without a token, turns healthy with one, answers `401` on `/mcp` without a token, and stops on `SIGTERM` before Docker's kill. Both actions are pinned to a commit, no secret is used, the token is read-only, and a missing Chromium fails the run instead of skipping tests. `tests/test_ci.py` pins those properties. The workflow has not run: Docker is not installed on the build machine, and the first run is the push.
- `starlette`, `uvicorn` and `mcp` are direct dependencies, with ranges inside `fastmcp`'s own, and `uv.lock` is refreshed. `sniffio` is a direct dependency too, for a reason the load test found (below). `hypothesis` is a dev dependency.
- Fuzzing (`tests/test_fuzz.py`), with hypothesis, over URL canonicalisation, link extraction, page extraction, encoding recovery, robots.txt parsing and matching, sitemaps and the nesting cap. Each test states one property. The default run draws 1,000 examples per property (a tenth of that for the slow HTML ones) from a fixed seed. `WEFT_FUZZ_EXAMPLES=20000` draws that many from a random one. It found five defects, all fixed, each with a plain regression test. The sitemap parser took 12 seconds on 100 KB of `<url>` tags with no close and 26 seconds on 4,000 spaces inside `<loc>`, and it runs on the event loop. `http://xn--/` made `Fetcher.fetch` raise idna's own error, so a link or a redirect to it killed a crawl. `canonicalize` returned URLs whose hosts cannot be names (an empty label, a space, a control character). `decode_body` raised on a charset name with a NUL in it. And robots.txt lines were split at characters RFC 9309 does not name.
- A cap on how deeply a document may nest (`extract/nesting.py`). The HTML parser's time is quadratic in depth: 0.1 s at 10,000 levels, 1.3 s at 20,000, and a 500 KB page of nested `<div>` took two minutes. The parser releases the GIL, so the event loop stayed free, but the extraction thread cannot be stopped and there are four. The cap cuts a document where more than 5,000 elements are open at once. See the first item under Left for later.
- An HTTP/2 test through the guarded transport (`tests/test_http2.py`, with a server fixture in `tests/h2_site.py` that records the protocol each connection negotiated). It shows that `h2` is negotiated, that one connection carries robots.txt and the page, that `http2 = false` gives the same server HTTP/1.1, that the certificate is checked against the name, that the address guard refuses before any connection and on a redirect, that a body larger than the flow-control window arrives whole, that the byte cap cuts a stream and the connection survives, that gzip is decoded, that parallel fetches share connections, and that a reset stream is a `network_error` after which the next request works. Four mutants of the guard and the transport are caught.
- robots.txt percent-encoding, per RFC 9309 section 2.2.2 (see Architecture), and line endings at CR, LF or CRLF.
- Load tests (`tests/test_load.py`) against a generated site on the fixture server, 400 pages by default and `WEFT_LOAD_PAGES` for more. They run a burst of fetches through one Fetcher, a crawl of every page, and eight MCP clients at once over the HTTP transport with `/healthz` polled throughout. On the build machine (a Windows laptop), the 5,000-page crawl took 154 seconds (32 pages a second, against 43 at 400 pages), fetched every page once with no failure or duplicate, grew resident memory by 6 MB, used 27 MB of database and left no task behind. Few requests overlapped at the server (at most two in flight with 16 workers), so extraction and database work pace a crawl, not the network. A burst of 5,000 fetches ran at 152 a second. Eight clients making 24 fetches kept to the host's two slots, and every `/healthz` answer was a 200 within 350 ms.
- The load test also found that httpcore imports `sniffio` inside a function that runs several times per request, and that a missing package is searched for again on every call. Fetches ran at 40 to 50 a second without it and 200 to 240 with it. A test checks that it is installed.
- 843 tests, 158 more than Phase 6, and strict types and lint clean. 31 hand-made mutants of the Phase 7 code are all caught. Details are in BuildLog.

Left for later:

- Extraction still runs in threads that cannot be stopped. The nesting cap closes the case that was quadratic. It is a scan, not a parser, so a page written against it can still get past (the comment in `extract/nesting.py` says how). The complete answer is a child process that can be killed. Separately, extraction is linear but slow on big pages: about 4 microseconds a byte on plain paragraphs and about 100 microseconds a link on a link-dense page. A 2 MB page of paragraphs took 30 seconds and a 6 MB page of 200,000 links 96 seconds. A page that size outlasts `extract_timeout_s`, the caller gets `extraction_timeout`, and the thread finishes in the background.
- Nothing deletes jobs, frontier rows or cache rows. Measured: about 5 KB of database per crawled page, 27 MB for 5,000. Needs a retention setting.
- robots.txt matching checks every rule on every request. A file at the 500 KiB read limit (about 25,000 rules) costs 5 to 10 ms per check on the event loop. A real file is a few hundred rules.
- The first CI run is still to come, and the Docker image has never been built. The `SIGTERM` branch of the child-process tests, the Debian package step and the browser download have run on no machine yet, and the suite has never run on Linux.
- Crawl throughput fell from 43 to 32 pages a second between 400 and 5,000 pages. Where the time goes is not known.
- Carry-overs from Phases 2 to 6 that this section does not name are unchanged: limits per host name, one token and no caller identity, no cap on queued jobs, the shared page cache, `Cache-Control: no-store`, sitemaps with namespace prefixes, `same_domain_only` and `www.`, two servers on one data directory, English-only stemming, and the rest of the lists above.
