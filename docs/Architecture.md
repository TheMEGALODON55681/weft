# Architecture

State as of Phase 7, the last planned phase. Each phase updated this file.

## Layout

```
src/weft/
  config.py        typed settings: env, TOML file, defaults
  models.py        shapes returned to the agent: pages, jobs, crawl params, site maps, search hits, extraction items
  providers.py     optional model providers: the two Protocols, one HTTP client for each, ProviderError
  retrieval.py     what search_in_site and extract do with a job's pages: read back, rank, rerank, extract
  db.py            SQLite connection, migrations, the single database thread
  obs/logging.py   JSON-lines logging to stderr, secret redaction, safe_url
  fetch/errors.py  FetchError, the one expected failure type
  fetch/http.py    Fetcher: client, redirect loop, and the order of robots, breaker, limits, retries
  fetch/ssrf.py    address check and the guarded connect, installed under the HTTP client
  fetch/robots.py  robots.txt parser, matcher and per-origin cache
  fetch/limits.py  per-host spacing and concurrency, global cap, circuit breaker
  fetch/retry.py   backoff with jitter, Retry-After parsing
  fetch/proxy.py   GuardedProxy: the local HTTP proxy Chromium is forced through
  fetch/browser.py BrowserPool: headless Chromium, navigation routing, reuse and limits
  fetch/page.py    get_document and get_page: cache lookup, fetch, decode, extract, render policy, output caps
  fetch/cache.py   PageCache: finished pages and their HTML in SQLite, TTL and conditional revalidation
  extract/decode.py   bytes to text for mislabelled or undeclared encodings
  extract/content.py  HTML to Markdown, title, metadata
  extract/links.py    outgoing links
  extract/nesting.py  cap_nesting: cuts a document that nests too deep, before any HTML parser sees it
  extract/schema.py   Extractor: CSS and XPath rules written into a JSON Schema, validation, limits
  extract/llm.py      the model fallback: prompt, call, parse
  frontier/canon.py   URL canonicalisation, site keys, URL patterns for trap detection
  frontier/dedupe.py  content hash, simhash, Hamming distance
  frontier/score.py   link scores and page relevance against the goal
  frontier/store.py   Frontier: one job's URLs in SQLite, best score first
  frontier/sitemap.py map_site: robots.txt Sitemap lines, sitemap parsing, link inventory
  rank/bm25.py        BM25 with corpus statistics that grow during a crawl, the tokenizer, a small stemmer
  rank/passages.py    passage splitting, passage ranking over many pages, snippets
  rank/rerank.py      reciprocal rank fusion of keyword rank and embedding rank
  jobs/crawl.py       Crawl: the workers, dedupe, trap limits, the time budget
  jobs/manager.py     JobManager: start, cancel, list, stats, resume at startup
  server/app.py    FastMCP server, tool definitions, lifespan, /healthz
  server/auth.py   BearerToken: the token check for the HTTP transport
  server/cli.py    `weft` entry point, transport flags, how each transport is started
tests/             unit tests per module, plus tests/fixture_site.py
docs/
Dockerfile         three stages: base, test, runtime (the default)
.dockerignore      keeps the repository's local state and secrets out of the build context
.github/workflows/ci.yml  three jobs on Linux: lint and types, the suite, and the image with the suite inside it
uv.lock            every dependency pinned, including the transitive ones
```

`rank/` holds everything that scores text: the BM25 the crawl uses for pages is the BM25 that search uses for passages.

## Data flow: `fetch_page`

1. The tool receives `url` and `render` from the MCP client. `server/app.py` pulls the shared state from the server lifespan: a `CrawlDeps` (settings, database, fetcher, browser pool, page cache), the job manager and the optional providers. The pool starts nothing until a page needs it.
2. **Cache.** `get_page` is `get_document` without the HTML. It canonicalises the URL and looks up `(url, render)` in the page cache. A fresh entry is returned at once, marked `cached`, with the URL as the caller wrote it. A stale entry's `ETag` and `Last-Modified` become `If-None-Match` and `If-Modified-Since` headers on the fetch. A `304` renews the entry and returns it. Anything else carries on below, and a `200` result is stored at the end.
3. Every mode starts with a plain fetch. The browser never fetches the document itself.
4. `Fetcher.fetch` validates the URL, then runs everything below under one `asyncio.timeout(total_timeout_s)`. Waits for a host's turn, retries, backoff sleeps, robots.txt reads and redirects all spend from that one budget. A caller can pass `timeout_s` to replace it for one call. Crawl jobs do, with `crawl_fetch_timeout_s`, so a long `Crawl-delay` is waited out instead of refused. Extra `headers` are sent on every hop, which is how the cache's validators travel.
5. The redirect loop in `Fetcher._fetch` handles each hop the same way:
   1. **robots.txt.** `RobotsCache.rules_for` returns the origin's cached rules, or reads `/robots.txt` on a miss. That read goes through steps b to e itself, with the robots check off and no spacing, so it gets the address guard, the breaker, the host's slots and retries. The hop is refused with `robots_disallowed` if the rules say so. A rule and the request target are compared after one normalisation of percent-encoding (RFC 9309 section 2.2.2: non-ASCII characters as UTF-8 escapes, upper-case hex, escapes of unreserved characters decoded, escapes of reserved characters kept), and a file's lines end at CR, LF or CRLF only. The rules' `Crawl-delay` raises the spacing for this hop. The `/robots.txt` URL itself is never checked.
   2. **Breaker, first look.** `CircuitBreaker.peek` refuses at once if the host's breaker is open, so a caller does not queue for a host that is down.
   3. **Limits.** `HostLimiter.slot` takes one of the host's concurrency slots, reserves the next start time (refusing with `rate_limited` if the wait would exceed `total_timeout_s`), sleeps until then, and takes a global slot. A host that is being made to wait never holds global slots.
   4. **Breaker, second look.** `CircuitBreaker.check` runs inside the slot, just before sending. The breaker may have opened while the request waited, and this is where the single probe after a cooldown is admitted.
   5. **Request.** `_attempt` streams the response. A redirect's body is not read. Any other body is read through a byte cap that counts decompressed bytes. When the client opens a connection, `GuardedBackend` resolves the name, checks every address and connects to a checked one (see Security posture).
   6. **Outcome.** A transport failure, or a `429`, `502`, `503` or `504`, is recorded as a failure against the host. If retries remain, the loop sleeps for the backoff (or the `Retry-After`, if it fits in the remaining budget) outside the slot and goes back to step b. When retries run out, a transport failure is raised and an overloaded response is returned as a result. Any other response is recorded as a success.
   7. **Redirect.** A 3xx with a `Location` is validated (http or https, a host, no credentials, not a URL already visited, within `max_redirects`) and the loop starts again at step a for the new URL.
6. `get_page` refuses non-HTML content types with `unsupported_content_type`, unless the status is 400 or above: an error page is returned as a result with empty Markdown, whatever its type. An absent `Content-Type` counts as HTML.
7. Decoding and extraction run in a worker thread so the event loop stays free, under `extract_timeout_s`. The decoded HTML first goes through `cap_nesting` (see Nesting cap). The thread comes from a pool of four that belongs to extraction alone. A thread cannot be interrupted, so a timeout frees the caller with `extraction_timeout` and leaves the thread to finish. The separate pool keeps stuck pages from using up the default executor, which also runs DNS lookups. `decode_body` tries a byte-order mark, the header charset, a `<meta>` charset, UTF-8, statistical detection and, as a last resort, UTF-8 with replacement characters. It never raises.
8. `extract_page` runs trafilatura for Markdown and metadata, and selectolax for links, `<title>`, `lang` and the canonical URL. A parser crash is logged and yields empty Markdown, not an exception.
9. **Render policy.** With `render="never"`, or `"auto"` when `wants_render` says no, the HTTP extraction is the answer. `wants_render` runs in the same worker thread. It says yes only if the page has a `<script>` and either the extracted text is under `render_min_words` words or a `<noscript>` mentions JavaScript. A page without scripts cannot gain content from a browser. With `render="always"`, the HTTP extraction is skipped.
10. **Rendering.** `BrowserPool.render` loads the fetched body in Chromium (see Rendering) and returns the serialised DOM and the page's final URL. That HTML goes through `extract_page` in the same pool and under the same timeout. It needs no nesting cap (see Nesting cap). In `"auto"` mode, any render failure is logged as `render skipped` and the HTTP extraction is returned. In `"always"` mode it is raised.
11. `get_page` applies `max_content_chars` and `max_links`, records the totals, and returns a `PageResult`. `fetch_method` says which path produced the content. `elapsed_ms` covers the whole call.
12. `server/app.py` converts any `FetchError` into an MCP tool error whose message starts with the error code. Anything else is logged with a traceback and reaches the client as a generic failure, with details masked.

## Data flow: `crawl_site`

1. The tool validates and canonicalises the seeds, writes a `jobs` row with status `queued` and the arguments as JSON, and starts a task. It then waits up to `wait_s`, polling the job once a second and sending an MCP progress notification each time, and returns the job with its top 20 pages. The job does not depend on the call: it keeps running if the call returns or the client goes away.
2. The task waits for one of `crawl_max_jobs` slots, then sets the job `running`, unless it was cancelled meanwhile.
3. `Crawl.run` puts any `fetching` rows back to `pending`, rebuilds its duplicate fingerprints from the `done` rows, and inserts the seeds at depth 0 (a resumed job already has them, and the insert leaves existing rows alone). The time budget is `crawl_timeout_s` minus the `run_s` that earlier runs recorded.
4. `crawl_concurrency` workers run in a `TaskGroup`, so a crash in one stops the others. Each worker, under one shared condition:
   1. stops if `max_pages` URLs have been taken,
   2. takes the pending row with the best score (ties go to the row discovered first) and marks it `fetching`,
   3. or, if nothing is pending, stops when no other worker is busy, and otherwise waits for one to finish, since it may add links.
5. **Fetch.** `get_page` with the job's render mode, the shared browser pool and the page cache, and a timeout of `crawl_fetch_timeout_s` or the time left, whichever is smaller. A `FetchError` marks the row `failed` with its code.
6. **Aliases.** If the final URL after redirects canonicalises to something else, that URL is recorded as a `duplicate` of this one, so it is never fetched.
7. **Judge.** In order:
   - A status of 400 or more: `failed`, with error `http_<status>`.
   - A `<link rel="canonical">` that names another in-scope URL: `duplicate` of that URL, which is queued if it is new. If the target is itself already a duplicate (two pages naming each other), the link is ignored and this page is kept.
   - The SHA-256 of the Markdown's words matches a kept page: `duplicate`.
   - With at least 30 words, a simhash within 6 bits of a kept page: `duplicate`.
   - Otherwise `done`. Its relevance is BM25 of title plus Markdown against the goal, `s / (s + 2)`. The page joins the corpus statistics first.
8. **Expand.** A `done` page under `max_depth` adds its links. Each is canonicalised (a URL whose host cannot be a name, such as one with an empty label, a space or a percent escape, is refused there) and dropped if it is `nofollow`, out of scope (another site with `same_domain_only`, failing the include or exclude globs, longer than 2048 characters, repeating a path segment more than twice), more than ten in a row with its parent's URL pattern, or disallowed by robots.txt rules that are already cached. Its score is `0.45 * anchor overlap + 0.25 * URL overlap + 0.30 * parent relevance`, where overlap is the share of goal terms present. With no goal, the score is `1 / (1 + depth)`, which makes the crawl breadth-first. Links already known keep their row. A pending one takes the higher score, the smaller depth and the shorter pattern run.
9. After each page the job's `run_s` and `updated_at` are written.
10. The crawl ends when the workers stop (`exhausted` or `max_pages`) or the budget runs out (`time_budget`). Rows still `fetching` go back to `pending`, and the task marks the job `done` with the reason, but only if it is still `running`, so a cancel is never overwritten.

`cancel_job` marks the job `cancelled`, cancels the task, waits for it and puts its `fetching` rows back. Shutdown cancels every task and leaves the rows alone: jobs stay `running`, which is also what a kill leaves, and `JobManager.resume` restarts every `queued` or `running` job at the next startup. Statistics in `get_job` are computed from the frontier rows when asked, so nothing about them can go stale across a restart.

## Data flow: `map_site`

1. `get_page` fetches the start page with `render="never"`. Its failure is the tool's failure.
2. The start origin's robots.txt is read through the Fetcher. Its `Sitemap:` lines are the queue, resolved against the site root if relative. With none, the queue is `/sitemap.xml`.
3. Up to 20 files are fetched through the Fetcher. A body starting with the gzip magic bytes is unpacked to at most `max_body_bytes`. `<url>`, `<sitemap>`, `<loc>` and `<lastmod>` are found by scanning for the tags with `find`, never by an XML parser, so there are no entities to expand, and the scan is linear in the size of the file. A `<sitemapindex>` adds its children to the queue.
4. URLs are canonicalised, kept only on the start page's site, and deduplicated. The start page and its links go in first. A URL seen first as a link takes the sitemap's `lastmod`. `allowed` comes from that URL's own origin's robots rules if they are cached, and is `null` otherwise. Reading stops at `max_urls`.

## Data flow: `search_in_site`

1. The tool checks the URL like a crawl seed, then checks that the query has a searchable word (`terms(query)` is not empty), so both failures come before a job exists. `limit` is capped at `RERANK_CANDIDATES` (50), so every hit it can return is one that was reranked.
2. It starts a crawl job with the URL as the only seed, the query as the goal and `max_pages` as the budget, and waits up to `wait_s` exactly as `crawl_site` does, with the same progress notifications. The wait and the job read are the shared `_crawl` helper in `server/app.py`. A job that is still running when the wait ends is left running, and its pages so far are searched.
3. `retrieval.search` reads each kept page back with `get_document` and the job's own render mode, so the read is a cache hit and costs no request. A page that raises `FetchError` is counted in `pages_unreadable` and left out.
4. `rank_passages` runs in the extraction pool under `extract_timeout_s`. For each page, `split_passages` cuts the Markdown at ATX headings (found by string tests, since a regex over a long untrusted line can backtrack). Lines with no letter or digit are dropped. A heading sets the label of the passages below it. Short lines are gathered until a passage has 150 characters, and a line over 700 is cut at sentence ends, then at words, then into slices. All passages of all pages form one BM25 corpus. A passage's terms are its page title, its heading and its text, so the title and heading count. A passage whose text already appeared on an earlier page is dropped. Passages with a score above zero are hits, in stable order for ties.
5. If an embedding provider is configured and there are hits, the best 50 go to `rerank`. It embeds the query and each hit's title, heading and text in one call, checks that the answer has one vector per input and one dimension, and sorts by `1/(60 + keyword rank) + 1/(60 + similarity rank)`. A tie keeps the keyword order. A `ProviderError` is logged as `rerank skipped` and the keyword order stands, with `reranked: false`.
6. Each hit gets a snippet: the 240-character window with the most query words, started 40 characters before the first one and cut at word boundaries, with an ellipsis where text was left out. Scores are rounded to six places. `complete` is true when the job's status is `done`.

## Data flow: `extract`

1. The schema is compiled into an `Extractor` before anything is fetched. `_check_structure` walks it iteratively and refuses a `$ref` that leaves the schema and nesting past 64 levels. Then the size check (50,000 characters as compact JSON), the JSON Schema meta-schema check, and one pass that compiles each selector (CSS through `cssselect`'s HTML translator, XPath through lxml) and enforces 300 nodes (the schema, its properties and its array items), 8 levels and 500 characters per selector. Any failure is an `InvalidSchema` that the tool turns into `invalid_schema`.
2. A value that starts with `http://` or `https://` is a URL. Anything else is a job id, and an unknown id is `unknown_job`.
3. For a URL, `get_document(..., "auto", with_html=True)` fetches the page as `fetch_page` would. For a job, the job's pages come from the job manager, most relevant first, and each is read with the job's render mode. HTML is read only here: `search` does not ask for it, so it never decompresses it.
4. For each page, in the extraction pool under `extract_timeout_s`: `Extractor.run` parses the HTML once with lxml, then evaluates the rules from the root. Values are collected under a budget of `max_content_chars` characters, and a run that passes it raises `DataTooLarge`, which becomes an error on the item instead of data. `Extractor.errors` then validates the result against the schema.
5. A schema with no selectors skips the rules and gets an error that says what is missing. If there are errors, a model provider is configured, fewer than 10 model calls have been made in this tool call and the page's status is below 400, the model is asked. A page with a status of 400 or more is not sent: the item keeps what the rules found and gains `llm: skipped, the page answered with status N`. When the model is asked: the schema without its selectors, the URL and up to `llm_max_input_chars` of Markdown, with a system message that calls the text untrusted data. The reply is parsed (a code fence is tolerated) and validated, in the pool, against the same schema. A valid reply makes the item `method: "llm"`. A provider error, a reply that is not JSON or a reply that does not validate leaves the item as the rules left it with one more error: `llm: <code>` or `llm: its answer did not satisfy the schema`.
6. A URL returns its one item whatever it holds. A job returns only the items that are valid and not empty (`None`, `{}` and `[]` are empty), and stops reading once `limit` pages have matched. A page that cannot be read, or whose extraction times out, is counted in `pages_unreadable`.

## Data flow: the remote transport

1. `weft --transport http` (or `WEFT_TRANSPORT=http`) loads `Settings`. A model validator refuses `transport = "http"` with no `auth_token` unless `http_host` is `localhost` or a loopback address. A host that is a name, an empty string or `0.0.0.0` counts as reachable from elsewhere, so a doubtful value fails closed. The validator runs when settings load, so the process exits with code 2 and a message that names `auth_token`, before it has bound anything. An empty or whitespace token is read as no token. A token must be 16 or more printable ASCII characters, and the error never repeats the value.
2. `cli.main` calls `FastMCP.run(transport="http", ...)`. `host_origin_protection` is `"auto"` when there is no token and off when there is one (step 5). The uvicorn configuration leaves logging alone (`log_config: None`) and turns the access log off. fastmcp builds the Starlette app and runs uvicorn on it. The server's lifespan is entered once at startup, so the database opens, jobs resume and the `ready` line is logged before the port accepts a connection. `ctx.lifespan_context` in a tool is the same `AppState` that stdio gives it.
3. **Routing.** `build_server` passes a `BearerToken` as the FastMCP `auth` when a token is set. fastmcp then puts the SDK's bearer middleware in front of `/mcp` only. The route wraps the endpoint in `RequireAuthMiddleware`, so a request with no `Authorization` header, with another scheme or with a token that `verify_token` refuses gets `401` and `WWW-Authenticate: Bearer`, and the MCP session code never sees it. `GET` and `DELETE` on `/mcp` are covered with `POST`. `/healthz` is a custom route and is outside that wrapper.
4. **The token check.** `BearerToken.verify_token` hashes the presented token and the configured one with SHA-256 and compares the digests with `hmac.compare_digest`. Equal-length digests mean the comparison time does not depend on how many leading bytes matched or on the token's length. Hashing first also lets any text through, where `compare_digest` raises on a non-ASCII `str`. The configured token is held only as its digest. `BearerToken` will not be built with an empty token, since `Bearer` followed by nothing would then match. A refusal logs `bearer token rejected` and nothing else. A success returns an `AccessToken` with client id `weft` and no scopes.
5. **Host and Origin.** Without a token, `"auto"` makes fastmcp check both headers on a loopback bind. It answers `421` for a `Host` it does not own and `403` for an `Origin` that does not match, so a page in the owner's browser cannot reach a tokenless local server by rebinding its own name to `127.0.0.1` or by sending a cross-origin request. With a token the check is off. The token is the defence then, and a reverse proxy on the same machine forwards requests under the public host name, which the check would answer with `421`. A server bound beyond loopback never checks them, since it requires a token.
6. **`/healthz`.** It reads the `AppState` that the lifespan stores in a dict while it runs. With none (before startup finishes, or after shutdown starts) it answers `503`. Otherwise it runs `SELECT 1` through `Database.run` under `asyncio.timeout(2)`. A database that does not answer in time, or raises, gives `503 {"status":"unavailable"}` and a warning with the traceback in the log. The response never names the cause. Both answers carry `Cache-Control: no-store`.
7. **Sessions.** The transport is stateful streamable HTTP, fastmcp's default. A session lives in memory, so a restart ends it and the client initialises again. Progress notifications from `crawl_site` and `search_in_site` travel on the response stream of the call that is waiting, which is why a proxy in front must not buffer.
8. **Shutdown.** uvicorn stops on `SIGINT` or `SIGTERM` (`SIGBREAK` on Windows), runs the lifespan's cleanup (jobs, the model clients, the browser pool, the fetcher, the database) and then raises the signal again, so the process exit status shows the signal. `docker stop` sends `SIGTERM`.

## Docker

`Dockerfile` has three stages. `base` starts from `python:3.12-slim-bookworm`, installs uv at a pinned version with pip, installs the dependencies from `uv.lock` with `uv sync --frozen --no-install-project --no-editable` (a layer that changes only when the lock does), copies the source and installs the project the same way, runs `playwright install --with-deps chromium-headless-shell` as root with `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`, and creates the user `weft` (uid 10001) and `/data`. `test` adds the `dev` extra and `tests/`, runs as `weft` and defaults to `pytest`. `runtime` is last, so a plain `docker build` makes it. It sets `WEFT_DATA_DIR=/data`, `WEFT_TRANSPORT=http` and `WEFT_HTTP_HOST=0.0.0.0`, runs as `weft`, exposes 8000 and starts `weft`.

The token is not in the image. Because the image binds `0.0.0.0`, a container started without `WEFT_AUTH_TOKEN` fails at settings validation and exits with code 2. The health check is a Python one-liner that opens `http://127.0.0.1:$WEFT_HTTP_PORT/healthz` with an empty proxy map (so a proxy variable in the container cannot redirect it) and fails on any non-2xx answer or on a refused connection. `tests/test_docker.py` takes that command from the file and runs it against a real server.

The installed copy of Weft is a wheel in the virtual environment, so the `test` stage tests what the `runtime` stage runs, not the source tree.

## Continuous integration

`.github/workflows/ci.yml` has three jobs on `ubuntu-latest` that do not depend on each other, so one red job hides no other. Each ends with a step that, on failure, writes the commands that reproduce it to the job summary.

- `lint`: `uv sync --frozen --extra dev`, `uv lock --check`, `ruff check --output-format=github` (annotations on the diff), `ruff format --check --diff`, and `mypy`.
- `test`: the same sync, `playwright install --with-deps chromium-headless-shell`, then `pytest -ra --tb=short`. `CI=true` is set, and `tests/skips.py` turns a missing Chromium from a skip into a failure, so a broken browser install cannot pass as a green run.
- `docker`: builds the `test` stage and runs the suite in it (`--init`, `CI=true`), builds the runtime image, and checks three things only a container shows. Without a token it exits with code 2. With a random one, started with the README's flags (`--init`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--memory 2g`, a named volume for `/data`), it turns healthy on `/healthz` and answers `401` to `POST /mcp` without a token. Then `docker stop --time 15` must end it with an exit code other than 137, which would mean it was killed after the grace period instead of stopping on `SIGTERM`. The container's log is printed when a step fails, and the container is removed either way.

The two actions (`actions/checkout`, `astral-sh/setup-uv`) are pinned to commits with their release in a comment. uv is pinned to the version that wrote `uv.lock` and that the Dockerfile installs. No secret is used, the token is `contents: read`, the checkout keeps no credentials, and a newer push to the same ref cancels the older run. `tests/test_ci.py` reads the file as text and fails if an action is unpinned, a secret or a write permission appears, a job is missing or a command is dropped. The workflow had not run when this was written.

In the image's `test` stage, `tests/` holds the tests and the package is installed, but the repository's `Dockerfile` and `.github` are not there, so `test_docker.py` and `test_ci.py` skip themselves.

## Error model

`FetchError(code, message)` is the one expected failure type. Codes are stable strings listed in the README. HTTP error statuses are not errors: a 404 page, or a 503 that outlasted the retries, comes back as a normal result with its status, whatever its content type. A crawl records them as failures with the code `http_<status>`.

Which layer raises each code, and what the retry loop and breaker do with it:

| Code | Raised by | Retried | Counts against the breaker |
| --- | --- | --- | --- |
| `invalid_url`, `redirect_loop`, `too_many_redirects` | `fetch/http.py` (`invalid_url` also for a host name IDNA cannot decode, such as `xn--`, whether in the URL given or in a redirect) | no | no |
| `blocked_address` | `fetch/ssrf.py`, at connect | no | no |
| `network_error` for a name that does not resolve | `fetch/ssrf.py`, at connect | no | no |
| `network_error`, `timeout` from the transport | `fetch/http.py`, from httpx errors | yes | yes |
| `robots_disallowed` | `fetch/http.py` | no | no |
| `robots_unavailable` | `fetch/robots.py` | no | no, but the robots read behind it was retried and counted |
| `rate_limited` | `fetch/limits.py` | no | no |
| `circuit_open` | `fetch/limits.py` | no | no |
| `unsupported_content_type`, `extraction_timeout` | `fetch/page.py` | no | no |
| `render_unavailable` | `fetch/browser.py` (Chromium missing or will not start, pool closed), `fetch/page.py` (no pool) | no | no |
| `render_failed`, `timeout` from rendering | `fetch/browser.py` | no | no |
| `unknown_job`, `invalid_query`, `invalid_schema` | `server/app.py` | no | no |

A `ProviderError` (`provider_unreachable`, `provider_error`, `provider_bad_response`) is not a tool error. `search_in_site` and `extract` catch it and carry on without the model, and the agent sees `reranked: false` or an `llm:` line in an item's errors.

The two render rows reach the caller only with `render="always"`. In `"auto"` mode they fall back to the HTTP result. A navigation that the Fetcher refuses inside a render does not fail the render: the browser sees a blocked request and the page carries on without it.

Only transport failures are retried, because only they can pass by themselves. A refusal that depends on the request, such as a blocked address or a disallowed path, would give the same answer again. A temporary DNS failure (`EAI_AGAIN`) is the one resolver error treated as a transport failure.

Errors from the robots.txt read itself (a timeout, a dead host, a blocked address, an open breaker) reach the caller unchanged and are not cached. They say nothing about the site's rules, and the page fetch would have failed the same way. Only an error status from a server that did answer becomes `robots_unavailable`.

## Rendering

`fetch/browser.py` holds one `BrowserPool` per server. It owns a Playwright driver, a `GuardedProxy` and at most one current Chromium process.

**Lifecycle.** The first render starts the proxy on a free loopback port, then the Playwright driver, then a headless Chromium. Chromium is launched with the proxy, `--proxy-bypass-list=<-loopback>`, `--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1`, `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` and `--js-flags=--max-old-space-size=<browser_js_heap_mb>`. A browser serves `browser_recycle_after` pages and is then retired. New renders go to a fresh browser, and the old one is closed when its last page finishes. A browser that disconnects, or whose context would not close within five seconds, is replaced the same way. `aclose` marks the pool closed, closes every browser, stops the driver (which kills anything it still owns, including a browser whose launch was cancelled) and closes the proxy. A render that arrives after that gets `render_unavailable` and launches nothing.

**One render.** Under `asyncio.timeout(render_timeout_s)`:

1. Take one of `browser_max_pages` slots, then a browser.
2. Start a `GuardedProxy` for this render alone, on a fresh loopback port, with a byte budget of `render_max_bytes` and connection spacing of `render_host_interval_s`.
3. Open a new context with that proxy (and `<-loopback>` again), service workers blocked, downloads off and Weft's User-Agent. Contexts are never reused, so cookies and storage die with the call.
4. Route every request in the context through one handler:
   - **Not a navigation.** Continue it. Chromium sends it to the render's proxy.
   - **The first navigation.** Fulfil it with the body, status and content type the Fetcher already has. There is no second fetch.
   - **Any other navigation** (frames, script navigations, popups). Non-GET is aborted. A GET goes to `Fetcher.fetch(url, follow_redirects=False)`, so the hop gets robots.txt, the breaker, the host's slot and spacing, retries and the address guard, as a plain fetch does. A refusal aborts the request. A redirect comes back with its `location`. Chromium follows a fulfilled 3xx without asking the route again, so the next hop would skip the Fetcher. The handler answers with a 200 page holding a `<meta http-equiv="refresh">` to the target instead, and the next hop arrives as a new navigation.
5. Go to the document's final URL and wait for `load`, then up to three seconds for the network to go quiet.
6. Serialise the DOM, cap it at `max_body_bytes`, note `page.url`, close the context, then close the render's proxy, which ends any tunnel still open. A spent byte budget is reported as truncation.

A Playwright error (a crashed tab, a heap that ran out) becomes `render_failed`. Running out of time becomes `timeout`. Either way the context is closed and the browser stays usable.

**The proxies.** The pool keeps one browser-wide `GuardedProxy`, started with the first browser, because Chromium on Windows needs a browser-wide proxy before per-context proxies work. Every context overrides it, so it only sees traffic that belongs to no context. Each render's own proxy is the same class with two extras. Its byte budget counts bytes relayed from upstream. Once spent, every relay stops at its next chunk and new connections get a 403. Its pacing reserves a start time per host name, with the limiter's `reserve`, before each upstream connect. A wait longer than `read_timeout_s` is refused with a 503. Plain HTTP through the proxy is one request per connection, so for `http://` pacing is per request. An HTTPS tunnel can carry many requests, so for `https://` it is per connection. Closing a proxy never waits on idle clients. It sets a flag that any handler not yet started checks first, cancels the rest, and bounds Python 3.12's `Server.wait_closed()` at one second, because a socket Chromium opened ahead of need would otherwise hold it until the render timed out.

`GuardedProxy` speaks the part of HTTP/1.1 that Chromium uses with a proxy. `CONNECT host:port` (HTTPS and WebSockets) opens an upstream through `GuardedBackend.connect_tcp`, the same code that guards plain HTTP, answers 200 and copies bytes both ways. An absolute-form request (plain `http://`) is rewritten to origin form, with `Host` taken from the URL, hop-by-hop headers dropped and `Connection: close` added, and only the request's own `Content-Length` body is forwarded. A request with `Transfer-Encoding` gets 411, because the proxy cannot tell where it would end. Anything else the client sends on that connection is read and dropped, so a second request can never reuse an upstream checked for another host. A blocked address answers 403 with the same message as plain HTTP, which does not say where the name points. A name that does not resolve answers 502, and a connect timeout 504. Heads over 64 KiB and malformed requests get 400. A tunnel ends when either side closes or goes quiet for `read_timeout_s`. Closing the proxy ends every open tunnel.

## Nesting cap

`extract/nesting.py`. lexbor, the parser behind selectolax, takes time quadratic in how deep a document nests: 0.1 s at 10,000 levels, 1.3 s at 20,000, about 30 s at 100,000, and Weft parses a page three times (links and title, the extractor, the render check). The parser releases the GIL, so the event loop stays free, but the thread cannot be stopped. A 500 KB page of nested `<div>` held one of the four extraction threads for two minutes.

`cap_nesting(html)` runs before the parsers, on the decoded HTML of a plain fetch. The DOM Chromium returns does not need it: the headless shell cannot serialise a DOM nested more than a few hundred levels (the tab crashes, between 100 and 300 levels on the build machine), so a script that nests deeper fails the render with `render_failed` and the pool carries on, which a test checks.

The cap finds tags with one regular expression that stops at the next `<`, so it is linear even on a document full of tags that never end. It counts each start tag and takes one off for each end tag whose name it has counted, so a stray end tag cannot get it ahead of the opens. Void elements, elements whose end tag is optional and the document's own `html`, `head` and `body` are not counted, and neither are SVG shapes written with `/>`. When more than `MAX_NESTING` (5,000) elements are open at once, it returns the document up to that tag. Browsers stop at 512. The returned HTML is also what the cache stores and what `extract` reads.

It is a scan, not a parser. It does not know what the real parser does with misnested tags, so it never needs to, and it cannot be fooled by a stray end tag. It can be fooled by a page written against it: end tags hidden in a comment, which it reads as end tags, or `<path/>` outside an SVG, which the parser opens and the scan does not. The cap stops pages that nest by accident or in the obvious way. The complete answer is extraction in a child process that can be killed.

## Storage

One SQLite file at `<data_dir>/weft.db`, in WAL mode with `synchronous=NORMAL`, `foreign_keys=ON` and a 5 second busy timeout.

All access goes through a single dedicated thread (`Database.run(fn)`). The connection never crosses threads, and callers serialise even when one is cancelled mid-call. Multi-statement work uses `with conn:` inside `fn`.

Migrations are an append-only list in `db.py`. Entry N takes the file from `user_version` N-1 to N, and the schema and the version bump commit together. A file with a newer `user_version` than the build knows is refused at startup.

Schema version 3. Version 1 created the three tables in Phase 1 with nothing using them. Migration 2 adds `stop_reason` and `run_s` to `jobs`, and drops and rebuilds `frontier` and `pages`, which had never held a row. Migration 3 drops and rebuilds `pages` with an `html` column, so every page cached before it is lost and fetched again. Jobs and frontier rows are kept.

| Table | Purpose | Key columns |
| --- | --- | --- |
| `jobs` | Crawl jobs | `id` (random hex), `kind`, `status` (`queued`, `running`, `done`, `failed`, `cancelled`), `params` (JSON of `CrawlParams`), `stop_reason`, `error`, `run_s`, timestamps |
| `frontier` | One row per URL a job knows | primary key `(job_id, url)` with the canonical URL; `depth`, `score`, `parent_url`, `anchor_text`, `run`; `state` (`pending`, `fetching`, `done`, `duplicate`, `failed`); after the fetch `final_url`, `status`, `title`, `snippet`, `relevance`, `content_hash`, `simhash` (signed 64-bit), `duplicate_of`, `error`, `bytes`, `elapsed_ms`, `cached`, `fetched_at`. Index `(job_id, state, score DESC, discovered_at)` serves `claim`. Rows cascade on job delete. |
| `pages` | Page cache | primary key `(url, render)`; `result` (a `PageResult` as JSON), `html` (zlib-compressed: the rendered DOM for a browser page, else the decoded body), `etag`, `last_modified`, `fetched_at`, `expires_at` |

Every frontier change commits on its own, which is what makes a kill safe: the worst case is a row left `fetching`, and the next run puts it back. An alias row (a redirect target or a canonical name) has `state = 'duplicate'` and no `fetched_at`, so it does not count against `max_pages`.

The robots cache, limiter state, breaker state, browser pool, BM25 statistics and the duplicate fingerprints of a running crawl live in memory. The fingerprints are rebuilt from the `done` rows when a job resumes. The BM25 statistics start again.

## Configuration

`Settings` (pydantic-settings). Precedence: init arguments, `WEFT_*` environment variables, TOML file, defaults. The TOML path is `WEFT_CONFIG`, falling back to `./weft.toml`. The CLI's `--config` sets `WEFT_CONFIG`. Unknown keys are rejected. The CLI prints validation problems by field name and never echoes the offending value.

Phase 2 added `allow_loopback`, `host_min_interval_s`, `host_concurrency`, `global_concurrency`, `max_retries`, `retry_backoff_s`, `breaker_threshold` and `extract_timeout_s`. Phase 3 added `render_min_words`, `render_timeout_s`, `browser_max_pages`, `browser_recycle_after` and `browser_js_heap_mb`. Phase 4 added `render_max_bytes`, `render_host_interval_s`, `cache_ttl_s`, `crawl_timeout_s`, `crawl_fetch_timeout_s`, `crawl_concurrency`, `crawl_max_jobs` and `max_tracked_hosts`.

Phase 5 added `embedding_rerank`, `embedding_base_url`, `embedding_model`, `embedding_api_key`, `llm_extraction`, `llm_base_url`, `llm_model`, `llm_api_key`, `llm_max_input_chars` and `provider_timeout_s`. The keys are `SecretStr`, so they never print. An endpoint must start with `http://` or `https://`, and a flag cannot be on without its endpoint and model. Both are checked when settings load, so a bad combination stops the server at startup. The check for the pair relies on the flag being declared after its endpoint fields, which a comment in `config.py` says.

Phase 6 added `transport` (`stdio` or `http`), `http_host`, `http_port` (1 to 65535) and `auth_token` (a `SecretStr`). The CLI's `--transport`, `--host` and `--port` are init arguments to `Settings`, so they beat the environment and the file and need no field of their own. Two checks run when settings load: `auth_token` is trimmed, read as unset when empty and refused when it is shorter than 16 characters or holds a space or a non-ASCII character (a `SecretStr` passed in code gets the same checks as a string), and `transport = "http"` beyond loopback needs a token. `_is_loopback_host` lives in `config.py` and returns false for anything it cannot parse as `localhost` or a loopback address. The CLI prints a model-level error without a field prefix, since the message names its own field.

Retrieval constants that are not settings: passages of 150 to 700 characters, 240 characters per snippet, 50 rerank candidates, a reciprocal rank constant of 60, at most 10 model calls per `extract` call, a 50,000 character schema, 300 nodes, 8 levels and 500 characters per selector, and 10 validation errors of 300 characters each.

Crawl constants that are not settings: ten links in a row per URL pattern, 2048 characters per URL, a path segment at most twice, a 6-bit simhash threshold over at least 30 words, 280 characters of snippet, 20 sitemap files per `map_site`, 50 `Sitemap:` lines per robots.txt, and the score weights in `frontier/score.py`. The three-second settle wait after `load` and the five-second limit on closing a context are constants.

Three values stay constants: the breaker's 30 second cooldown, the robots.txt cache time of an hour, and the minute that an unreadable robots.txt is remembered. Nobody had a reason to tune them, and a setting is easier to add than to take back.

## Logging

One JSON object per line on stderr, from the `weft` logger: `ts`, `level`, `logger`, `msg`, plus any `extra` fields. stdout is the MCP protocol channel under stdio, so nothing may print to it.

Secrets are redacted by field name (token, secret, password, authorization, cookie, session, signature, api key), including inside nested dicts and lists. URLs go through `safe_url`, which drops credentials and fragments and masks sensitive query values. Redaction works on field names, so keep sensitive values out of log messages.

Phase 2 adds two lines to the `weft.fetch` logger: a warning, `refused non-public address`, with the host and the addresses it resolved to (the caller is not told them), and an info line, `retrying`, with the attempt number and the reason. The existing `fetch failed` warning now also carries the new error codes.

Phase 4 adds, on `weft.jobs`, `job queued`, `job resumed`, `job done` (with the stop reason) and `job failed` (an error with the traceback), and on `weft.crawl` an info line `crawled` per page, with the job, URL and state. `ready` now carries `jobs_resumed`. The browser adds `render byte budget spent` (info, with the bytes).

Phase 5 adds `page unreadable` (info, with the URL and code), `rerank skipped` and `model extraction failed` (warnings, with the provider error code and nothing else). `ready` now says whether each optional feature is on.

Phase 6 puts the loggers of the HTTP stack (`uvicorn`, `fastmcp` and `mcp`) behind the same JSON handler as `weft`, because each of them formats its own lines otherwise and a log pipeline that parses every line would choke on the first one. uvicorn's `color_message` field, an ANSI copy of the message, is dropped. uvicorn's access log is off: it would write the full request path, query string included, and a client that puts a secret in a query string would have it logged. `weft.server` adds `bearer token rejected` (warning, no fields) and `health check failed` (warning, with the traceback). `ready` is logged once, before the port opens.

Phase 3 adds `browser started` (info, with the launch count), `browser unavailable` and `render failed` (warnings, with the first line of the Playwright error), and `render skipped` (info, when `"auto"` falls back to HTTP, with the code). A navigation the Fetcher refuses inside a render is logged as an ordinary `fetch failed`.

## Security posture

In place:

- **Addresses.** `fetch/ssrf.py` wraps the HTTP client's connect step. On every new connection it resolves the name once, checks every returned address with `is_blocked`, refuses the whole name if any is blocked, and connects to one of the checked addresses by IP, so the client never looks the name up again. This covers redirect hops, robots.txt reads and DNS rebinding with no separate check. It also covers numeric spellings of an address, because the check runs on what the name resolves to. `is_blocked` refuses anything that is not a public unicast address. The standard library's `is_global` is not enough on its own, because it reports multicast addresses, the Azure wireserver (`168.63.129.16`) and NAT64 addresses as global. So the check adds explicit rules, refuses Teredo and 6to4 outright, and judges the IPv4 address inside IPv4-mapped and NAT64 (`64:ff9b::/96`) addresses. NAT64 is how IPv6-only networks reach IPv4-only sites, so it cannot be blocked wholesale.
- **Loopback.** Refused, unless `allow_loopback` is set. That setting opens `127.0.0.0/8` and `::1` and nothing else. The remote transport and the Docker image leave it off, and the README says to keep it off.
- **Remote access.** `/mcp` needs a bearer token whenever one is configured, and the server will not bind beyond loopback without one (see the remote transport above). The token comes from `WEFT_AUTH_TOKEN`, is a `SecretStr`, is compared by digest in constant time, and is never logged, echoed in a configuration error or accepted in a URL. Authentication happens before any tool runs, so an unauthenticated request cannot start a fetch, a job or a database read. The one open endpoint, `/healthz`, runs a single `SELECT 1` and returns a fixed body. On a loopback bind without a token, fastmcp's Host and Origin check stands in for it against DNS rebinding.
- **What the caller learns.** A refusal says that a name resolves to a non-public address. It does not say which one, so a probe cannot map internal DNS.
- **Proxies.** The HTTP client ignores `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY`. A proxy resolves names for itself and would sit outside the check. `SSL_CERT_FILE` still applies.
- **Crawls.** A crawl adds no new way to reach the network. Every page, alias target, canonical target and sitemap file is fetched by the same Fetcher, with the same address check, robots.txt, pacing and breaker. Seeds and links are canonicalised first, and URLs with credentials are dropped. Glob patterns are limited to 500 characters each and 50 per list, and `fnmatch` compiles them without catastrophic backtracking. Sitemaps are read by pattern, not by an XML parser, so entity expansion cannot happen, and gzip is unpacked to `max_body_bytes` at most.
- **The browser.** Chromium never opens a connection of its own. The document comes from the Fetcher. Every other navigation is fetched by the Fetcher one hop at a time. All other traffic goes through `GuardedProxy`, which uses the same `GuardedBackend` as plain HTTP. `<-loopback>` removes Chromium's built-in rule that sends localhost traffic around a proxy. Weft passes it explicitly, so an environment variable that turns off Playwright's own default cannot undo it. Chromium's own resolver answers "not found" for every name except the proxy's address, so anything that tried to go around the proxy could not even look up a host. WebRTC may not send UDP that the proxy cannot carry. Chromium refuses WebTransport behind a proxy by itself, and a test keeps it that way. Service workers are blocked. A test also calls `register` through the prototype, which gets past a stub on the navigator object, and checks that the script is never fetched. Contexts are discarded after each render.
- **Politeness.** robots.txt on every hop, per-host spacing and concurrency, a global cap, retries with backoff, and a breaker per host. Weft has no setting to ignore robots.txt.
- **Existing controls.** URLs limited to http and https, no credentials in URLs, caps on time, size and redirect hops, no cookie persistence, error details masked from the client, secrets redacted from logs, and a server instruction telling the agent that page content is untrusted.
- **Search and extraction.** They add no way to reach the network. Every page comes from the page cache or from the same Fetcher, so the address check, robots.txt and pacing apply. Reading pages back asks the Fetcher only for entries that expired or are missing.
- **Extraction rules.** The schema is the agent's input and the page is a stranger's, so both are bounded. The schema is limited in size, properties, depth, selector length and JSON nesting, and checked before any fetch. A `$ref` may point only inside the schema, which `jsonschema` would not fetch anyway but nothing should rely on that. Selectors run on an lxml tree from the HTML parser, which loads no external entities and no network resource. XPath 1.0 has no function that opens a file or a URL. A run collects at most `max_content_chars` characters, because a selector such as `div` on nested divs returns the same text at every level, and a page can build that for a few kilobytes. Validation errors are capped at ten of 300 characters.
- **Providers.** The endpoints come from settings and no tool argument can name one. The key is a `SecretStr`, sent only in the `Authorization` header. Redirects are not followed, since one would carry the key to wherever it points. A call has a wall-clock limit, because the HTTP client's timeouts apply per read and a server that drips bytes would never trip them. An error carries a code and a short message that never holds the key or the response body. The model is told that the page is data and to ignore instructions in it, and its reply is trusted no further than its validity against the schema.

How the guard reaches the client: httpx has no public hook for how connections are opened, so `guarded_transport` replaces the network backend on the transport's connection pool, through a private attribute. If an httpx upgrade moves it, the guard fails closed, because the SSRF tests expect refusals and would fail. A dependency bump should run them. The same guarded backend carries HTTP/2: `tests/test_http2.py` shows that `h2` is negotiated through it, with the certificate checked against the name and every refusal intact.

`httpcore` is pinned to the 1.0 series in `pyproject.toml` because the guard depends on its private `_network_backend`. Raising that pin means re-running `tests/test_ssrf.py` first.

Known ceilings:

- Limits are per host name. `example.com`, `example.com.` and a second name for the same server are separate hosts. The limiter and the breaker keep state for `max_tracked_hosts` hosts and drop the least recently used idle one. A host that is mid-request, owed spacing or has an open breaker is kept even past the cap. The robots cache holds 512 origins and drops the least recently used.
- robots.txt matching is plain string search, not regular expressions, because the file is controlled by the site. Every rule is checked on every request, so a file at the 500 KiB read limit (about 25,000 rules) costs 5 to 10 ms per check on the event loop, and a crawl checks each link of each page. A real file has a few hundred rules.
- A hostname that resolves to several addresses is tried one address at a time, in resolver order, with no happy-eyeballs race.
- On Windows a numeric spelling such as `2130706433` fails to resolve at all, after about three seconds, so the error is `network_error` there and `blocked_address` on Linux and macOS. Nothing connects either way.
- A page that hangs extraction occupies one of four threads until it finishes. Four of them stop extraction. The nesting cap (below) closes the case that was quadratic and found by fuzzing. It does not close the class. Extraction is also slow on big pages, in proportion to their size: about 4 microseconds a byte of plain paragraphs and about 100 microseconds a link on a link-dense page, so a 2 MB page of paragraphs took 30 seconds and a 6 MB page of 200,000 links 96 seconds. Either outlasts `extract_timeout_s`. The caller gets `extraction_timeout` and the thread finishes in the background. A child process that can be killed is the complete answer.
- robots.txt, host pacing and the breaker apply to the browser's navigations, not to its sub-resources. Sub-resources get the address check, `render_max_bytes` and `render_host_interval_s` spacing per host. Over HTTPS the spacing is per tunnel, and one tunnel can carry many requests.
- The proxies listen on loopback ports without authentication. A per-render proxy lives only for its render, and the browser-wide one lives as long as the browser. Another local process could use either, but only to reach addresses the guard allows. In the Docker image the only other processes in the network namespace are Weft and Chromium, and a published port forwards to the container's own address, not its loopback. That reasoning is not a test, since no container could run during Phase 6.
- `browser_js_heap_mb` caps JavaScript memory per tab, not DOM, image or GPU memory. The byte budget caps what a render downloads, not what it allocates.
- The fulfilled document carries only its status and content type, not the origin's other headers, so the page runs without its own Content-Security-Policy. Nothing in a throwaway context is worth protecting, but the rendered DOM can differ from what a real visitor sees.
- `status` in a rendered result is the document's status, even if a script then navigated to another page.
- A gzip bomb is cut at the byte cap, but the check runs per decoded chunk, so memory can spike to about 1000 times one network chunk before the cut.
- The page cache is shared by every caller. That is safe only while Weft sends no cookies. Authenticated crawls must bypass or partition it.
- Jobs are not limited in number, only in how many run at once. Every queued job is a waiting task and a row. Phase 6 left this open. A per-caller cap needs a caller, and the HTTP transport has one shared token and no identity, so the only cap on offer is a global one, which a single token holder could fill for everyone. It is a decision about how many holders to expect, so it waits for a use.
- Weft does not terminate TLS. The token is a bearer credential, so it needs a TLS proxy in front on any network that is not trusted. The README says so.
- There is one token. Rotating it means restarting the server, which ends every session. Per-client tokens, expiry and revocation are out of scope.
- `/healthz` is open and queries the database. A flood of requests to it queues behind the single database thread, next to the crawl's own writes. Each query is one `SELECT 1`, and a reverse proxy can rate-limit the path.
- Playwright launches Chromium with its own sandbox off (`chromiumSandbox: false`, which adds `--no-sandbox`), and Weft keeps the default. The container is the boundary in the Docker image, which runs as an unprivileged user. On a bare host a browser exploit would run with the permissions of the user running Weft, so give it a dedicated unprivileged user.
- The Docker image names its base by tag, and the system libraries come from Debian at build time. The Python side is locked.
- The loopback test accepts the name `localhost` without resolving it. A hosts file that points `localhost` at another address would make a tokenless server reachable from that address. Use the address `127.0.0.1` when in doubt, or set a token.
- Nothing is ever deleted: jobs, frontier rows and cache rows, expired ones included. `search_in_site` starts a job per call, and a cache row now holds the page's HTML, so rows are about ten times larger.
- The provider endpoints are not behind the address guard. An operator who sets one to a server on their own network means to reach it. If endpoints ever come from a caller, they need the guard first.
- A model cannot be kept from copying misleading page text into a field that accepts any string. The schema bounds the shape of the answer, not its truth. The agent should treat `extract` data as it treats page text.
- A JSON Schema `pattern` and an XPath come from the agent and run against page text. A pathological one can hold one of the four extraction threads until it finishes, the same ceiling as a page that hangs extraction.
- A search ranks all its pages in one pass under `extract_timeout_s`. Many very long pages can exceed it.
- The model calls of one `extract` call run in sequence, so a slow endpoint can hold the call for ten times `provider_timeout_s`.

## Tests

`tests/fixture_site.py` runs a local HTTP server on a random port. It serves a sample article, redirect chains and loops, an endless redirect, oversized and gzip-bomb bodies, a slow response, a response that drips bytes, Latin-1 and meta-declared Windows-1251 pages, non-HTML types, error statuses and a cookie echo. Phase 2 added a `robots.txt` whose body (or status, or absence) each test can choose, a route that fails a set number of times before succeeding, a route that drops the connection, a route that records how many requests were in flight at once, redirects to metadata, private and IPv4-mapped addresses, and per-site counters for hits and request times, so a test can prove that a request did or did not reach the server. Phase 4 adds a small site under `/site/` (see below).

`tests/conftest.py` sets `WEFT_ALLOW_LOOPBACK=1`, `WEFT_HOST_MIN_INTERVAL_S=0` and a 10 millisecond retry backoff for every test, because the fixture site is on 127.0.0.1. Tests of the address guard, the limiter and the retry timing pass their own values. Init arguments beat the environment.

The address table, the robots matcher, the limiter and the breaker are tested as pure units with a fake clock or a fake resolver, with no sleeping and no network. The DNS rebinding cases use a fake resolver that gives a different answer on each call, and a fake inner backend that records what it was asked to connect to. End-to-end tests run the real `Fetcher` against the fixture site.

The fixture can also serve HTTPS. `tests/test_tls.py` makes a throwaway certificate with `cryptography`, valid for the name `localhost` and no IP address, and trusts it through `SSL_CERT_FILE`. Fetching `https://localhost:PORT/` can only succeed if the certificate is checked against the name in the URL while the socket goes to the pinned address. A second test serves a certificate for another name and expects the fetch to fail, so verification is known to be on.

Phase 3 adds a JavaScript shell page, `/goto` (a redirect to a target the test names), `/sw.js`, `/echo-body` and `/host` to the fixture. The fixture now counts every request method, and tests can count requests by path prefix.

`tests/test_proxy.py` drives `GuardedProxy` with raw sockets and a recording backend, with no browser: CONNECT and plain forwarding, refusals before any connect, rebinding between connections, one request per connection, request bodies, Host rewriting, malformed and oversized heads, and tunnels closing with the proxy.

`tests/test_browser.py` runs a real headless Chromium. The SSRF cases put the proxy behind a fake resolver (`cdn.test` public, `internal.test` private) and a backend that connects every address to the fixture site. If anything got past the guard, the fixture would count the request, so a zero count proves the guard refused it. The main case serves an allowed page that tries images, a stylesheet, `fetch`, WebSockets, a beacon, a worker, a redirect from an allowed host to a blocked one, and a frame, against private, loopback, `localhost` and `[::1]` targets. Positive controls (a script, a fetch, a WebSocket and a redirect on the allowed host) must reach the fixture by the same mechanisms. Other cases cover UDP (WebRTC and WebTransport, checked on a UDP socket), service workers, robots.txt, spacing and the breaker on navigations, the page cap, recycling, a heap blowout, a tab spinning forever, the render timeout, a missing browser and a closed pool. These tests skip if `chromium-headless-shell` is not installed. The rendering tests in `tests/test_page.py` skip too, through a session fixture that launches Chromium once to find out.

`tests/test_server.py` includes a real stdio round trip: it spawns `python -m weft` and talks to it over pipes. Phase 4 adds the crawl tools over an in-memory MCP client, progress notifications, a fetch served from the cache a crawl filled, `map_site`, and a server that is stopped mid-crawl and resumes the job when it starts again.

Phase 4 fixture site. `/site/` links to a pricing section, unrelated pages, a blog with a `nofollow` link, two traps (`/trap/N`, an archive that never ends, and `/site/calendar?month=N`, which links a month either way), a near-duplicate pair (`/site/dup-a` and `/site/dup-b`, one date apart), an exact duplicate with other spacing (`/site/dup-c`), a canonical alias (`/site/alias`, reached through a `utm_source` link), a path robots.txt disallows, a 404, a PDF and an off-site link. Trap pages carry text that differs for every N, so dedupe cannot be what ends the trap. There is also a sitemap index with a plain sitemap, a gzip sitemap and a missing one, routes that answer `304` to `If-None-Match` and `If-Modified-Since`, and `/blob/N/...`, which serves N bytes of image.

`tests/test_crawl.py` drives `JobManager` against that site: goal order against breadth-first order, both traps bounded with the depth and page budgets out of the way, duplicates and aliases kept once, scope rules, failure codes and metrics, the time budget, the job slot limit, cancellation, a worker crash, shutdown and resume, and a real kill. The kill test starts a crawl in a child Python process, kills it with no warning once three pages are in, then resumes the job in the test and checks that it finishes with at most two pages fetched twice.

`tests/test_cache.py` covers fresh hits, `ETag` and `Last-Modified` revalidation, refetching without validators, error pages, render modes and a zero TTL. `tests/test_canon.py`, `tests/test_dedupe_score.py` and `tests/test_sitemap.py` are mostly pure units. `tests/test_browser.py` gains the render byte budget, with a sequence of downloads that must stop at the third, and connection spacing, with an unpaced control.

Phase 5 adds no network to the tests. `tests/test_providers.py` drives the HTTP clients through `httpx.MockTransport`, so no socket opens, and checks the request each one builds (URL, body, bearer header only with a key), the three error codes, that no response body or key reaches an error, that a redirect is not followed and that a slow server is given up on. `tests/test_server.py` injects fake providers into `build_server`, with settings that point at a host that cannot resolve, so a call that reached for the network would fail. A fake embedder places passages so that the reranked order is known, and a fake model records every prompt it is sent.

`tests/test_passages.py` covers splitting (headings, gathering, sentence cuts, long unbroken lines, tables and rules), ranking across pages, repeats, ties and snippets, in several scripts. `tests/test_schema_extract.py` is pure: every selector form, numbers, attributes, records, mixed CSS and XPath, every refusal, the output budget, and one `Extractor` shared by sixteen threads. `tests/test_rerank.py` and `tests/test_llm_extract.py` use fake providers.

The fixture site gains `/site/anchors` (four links that differ only in anchor text, the matching one listed last) and `/site/jobs` with two detail pages (records with a title, a pay and a link, and a salary on each detail page). `tests/test_crawl.py` uses the first to show that anchor text alone decides the fetch order. The server tests crawl the second to extract from a URL and from a job, to show that a job's pages are read from the cache with the hit counter unchanged, and to count pages that cannot be read again by making `get_document` fail for one of them.

`tests/test_cache.py`, `tests/test_page.py` and `tests/test_db.py` cover the HTML column: a stored page's HTML round trip, the rendered DOM from a real headless Chromium, a revalidated page keeping its HTML and the migration from version 2.

Phase 6 adds `tests/test_http.py`, `tests/test_docker.py` and cases in `test_config.py`, `test_cli.py`, `test_logging.py` and `test_server.py`.

`test_http.py` runs the streamable HTTP app under uvicorn on a free loopback port inside the test's event loop, so a real socket carries every request. The verifier is also tested directly, with a spy on `hmac.compare_digest`: it must be called once per attempt with two 32-byte digests. The constant-time claim rests on that, since no test can time a comparison reliably. Raw `httpx` requests send a table of bad `Authorization` values (none, empty, `Bearer` alone, a wrong token, one character too long or short, another scheme, the bare token) to `POST`, `GET` and `DELETE` on `/mcp`, and the fixture site's hit count shows that no fetch happened for a stranger. A token in the query string, under four parameter names, does not open the endpoint. A `fastmcp` client with the token lists the tools, fetches a page and runs a crawl, and receives progress notifications over the stream. `/healthz` is checked open and healthy, then `503` when the database hangs (with a shortened timeout) and when it raises, with no detail in the body. Two tests start `python -m weft` as a child process, and they are the only tests of what the command line wires up. The first has a token in the child's environment. It waits for `/healthz` and checks `401` without the token, `200` with it, `200` with it under a foreign `Host` (a proxy on the same machine) and `401` for that `Host` without it. It then sends a request with a secret in the query string, stops the process with `CTRL_BREAK_EVENT` (`SIGTERM` elsewhere) and checks that stdout is empty, neither the token nor the query secret appears in stderr, every line of stderr parses as JSON and `ready` is logged once. The second has no token, on loopback: `/mcp` is open, a foreign `Host` gets `421` and a foreign `Origin` gets `403`. The children get a trimmed environment and are killed if they have not stopped after 15 seconds.

`test_docker.py` reads the Dockerfile. It runs the `HEALTHCHECK` command from the file, with the test's interpreter, against a healthy server, a server that answers 503 and a closed port, with a proxy variable set that the probe must ignore. It then checks as text what the file exists for: an unprivileged `USER` in the runtime stage, `runtime` as the last stage, the headless shell installed with its libraries, `--frozen` on every `uv sync`, a pinned uv, no token or secret in the image, and the repository's local state listed in `.dockerignore`. Nothing builds an image.

`test_config.py` covers the loopback table (what counts and what does not), the refusal beyond loopback, empty and weak tokens (the error text never holds the value), trimming, the environment and the port range. `test_cli.py` swaps the server for a recorder and checks which call each flag and variable produces, exit code 2 for a bad token or a non-loopback bind without one, and that nothing starts in those cases. `test_logging.py` checks that the HTTP stack's loggers write the same JSON and that `color_message` is dropped. `test_server.py` sends error pages (400, 404 and 500) to a fake model and checks that it is never called.

Phase 7 adds `tests/test_fuzz.py`, `tests/test_http2.py`, `tests/test_load.py`, `tests/test_nesting.py` and `tests/test_ci.py`, and cases in `test_robots.py`, `test_sitemap.py`, `test_canon.py`, `test_fetch_http.py` and `test_decode.py`.

`test_fuzz.py` uses hypothesis. `conftest.py` registers one profile: 1,000 examples per property from a fixed seed (the HTML tests take a tenth of that), no deadline, and no example database, so a clean checkout and CI behave alike and nothing is written into the repository. `WEFT_FUZZ_EXAMPLES=N` switches to N examples from a random seed. The properties: `canonicalize` either raises `ValueError` or returns an ASCII URL with no fragment, credentials or control characters, a name or an address for a host, and a form that canonicalises to itself, and two spellings of one URL (other case, default port, trailing dot, dot segments, escaped letters in either hex case, shuffled and tracking parameters, a fragment) share one form; `parse_http_url` raises only `FetchError`; extracted links are http(s) URLs without a fragment that canonicalise or raise `ValueError`; `extract_page`, `decode_body`, `wants_render` and the extraction rules never raise on tag soup, bytes and odd charset names; `parse_robots` and `Rules.allows` never raise, the matcher agrees with a regular expression on small alphabets, the verdict is the longest match with `Allow` winning a tie, a rule always blocks the path it was written as, and an escaped unreserved character means the character; `parse_sitemap` never raises and runs in linear time on any tag or run of spaces repeated tens of thousands of times; `_gunzip` never exceeds its cap; `cap_nesting` returns a prefix, is stable and is linear. A table of known hostile pages (200,000 nested divs, 8,000 nested tables, tags that never end, a 1 MB attribute, 50,000 comment openers) must extract in under ten seconds.

`tests/h2_site.py` is an HTTP/2 server on its own thread, built on the `h2` library that httpx needs: TLS with ALPN, flow control, a stream it resets on request, and an HTTP/1.1 answer on the same port for a client that offers nothing else. It records the ALPN protocol of every connection and every request, so `test_http2.py` can say which protocol was negotiated and how many connections carried how many requests. It shares `tests/certs.py` with `test_tls.py`.

`test_load.py` runs against `/load/<i>` on the fixture, a generated tree of up to `WEFT_LOAD_PAGES` pages (5 links each, unique text, 2 ms per request, in-flight peak recorded). The burst, the crawl and the eight MCP clients are described in Phases. `tests/rss.py` reads resident memory on Windows and Linux. The thresholds are loose: a leak, a stall or a broken cap fails them, a slow day does not.

`test_nesting.py` tests `cap_nesting` as a table of documents that must pass unchanged and documents that must be cut, an exact cut for plain divs, a hostile page extracted in under five seconds, linear time on tags that never end, and a nested page through `get_page` and the fixture's `/deep` route.

`tests/http_app.py` holds `serve` and `remote`, which `test_http.py`, `test_docker.py` and `test_load.py` share. `tests/skips.py` is `skip_or_fail`, which fails in place of skipping when `CI` is set.
