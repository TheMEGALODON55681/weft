# Architecture

State as of Phase 2. Later phases add modules and tables; each phase updates this file.

## Layout

```
src/weft/
  config.py        typed settings: env, TOML file, defaults
  models.py        shapes returned to the agent (PageResult, Link, PageMetadata)
  db.py            SQLite connection, migrations, the single database thread
  obs/logging.py   JSON-lines logging to stderr, secret redaction, safe_url
  fetch/errors.py  FetchError, the one expected failure type
  fetch/http.py    Fetcher: client, redirect loop, and the order of robots, breaker, limits, retries
  fetch/ssrf.py    address check and the guarded connect, installed under the HTTP client
  fetch/robots.py  robots.txt parser, matcher and per-origin cache
  fetch/limits.py  per-host spacing and concurrency, global cap, circuit breaker
  fetch/retry.py   backoff with jitter, Retry-After parsing
  fetch/page.py    get_page: fetch, decode, extract under a timeout, apply output caps
  extract/decode.py   bytes to text for mislabelled or undeclared encodings
  extract/content.py  HTML to Markdown, title, metadata
  extract/links.py    outgoing links
  server/app.py    FastMCP server, tool definitions, lifespan
  server/cli.py    `weft` entry point
tests/             unit tests per module, plus tests/fixture_site.py
docs/
```

Planned directories (`rank/`, `frontier/`, `jobs/`) appear with the phase that needs them.

## Data flow: `fetch_page`

1. The tool receives `url` and `render` from the MCP client. `server/app.py` pulls the shared state (settings, database, fetcher) from the server lifespan.
2. `get_page` rejects `render="always"` with `render_unavailable`. No browser exists yet.
3. `Fetcher.fetch` validates the URL, then runs everything below under one `asyncio.timeout(total_timeout_s)`. Waits for a host's turn, retries, backoff sleeps, robots.txt reads and redirects all spend from that one budget.
4. The redirect loop in `Fetcher._fetch` handles each hop the same way:
   1. **robots.txt.** `RobotsCache.rules_for` returns the origin's cached rules, or reads `/robots.txt` on a miss. That read goes through steps b to e itself, with the robots check off and no spacing, so it gets the address guard, the breaker, the host's slots and retries. The hop is refused with `robots_disallowed` if the rules say so. The rules' `Crawl-delay` raises the spacing for this hop. The `/robots.txt` URL itself is never checked.
   2. **Breaker, first look.** `CircuitBreaker.peek` refuses at once if the host's breaker is open, so a caller does not queue for a host that is down.
   3. **Limits.** `HostLimiter.slot` takes one of the host's concurrency slots, reserves the next start time (refusing with `rate_limited` if the wait would exceed `total_timeout_s`), sleeps until then, and takes a global slot. A host that is being made to wait never holds global slots.
   4. **Breaker, second look.** `CircuitBreaker.check` runs inside the slot, just before sending. The breaker may have opened while the request waited, and this is where the single probe after a cooldown is admitted.
   5. **Request.** `_attempt` streams the response. A redirect's body is not read. Any other body is read through a byte cap that counts decompressed bytes. When the client opens a connection, `GuardedBackend` resolves the name, checks every address and connects to a checked one (see Security posture).
   6. **Outcome.** A transport failure, or a `429`, `502`, `503` or `504`, is recorded as a failure against the host. If retries remain, the loop sleeps for the backoff (or the `Retry-After`, if it fits in the remaining budget) outside the slot and goes back to step b. When retries run out, a transport failure is raised and an overloaded response is returned as a result. Any other response is recorded as a success.
   7. **Redirect.** A 3xx with a `Location` is validated (http or https, a host, no credentials, not a URL already visited, within `max_redirects`) and the loop starts again at step a for the new URL.
5. `get_page` refuses non-HTML content types. An absent `Content-Type` counts as HTML.
6. Decoding and extraction run in a worker thread so the event loop stays free, under `extract_timeout_s`. The thread comes from a pool of four that belongs to extraction alone. A thread cannot be interrupted, so a timeout frees the caller with `extraction_timeout` and leaves the thread to finish. The separate pool keeps stuck pages from using up the default executor, which also runs DNS lookups. `decode_body` tries a byte-order mark, the header charset, a `<meta>` charset, UTF-8, statistical detection and, as a last resort, UTF-8 with replacement characters. It never raises.
7. `extract_page` runs trafilatura for Markdown and metadata, and selectolax for links, `<title>`, `lang` and the canonical URL. A parser crash is logged and yields empty Markdown, not an exception.
8. `get_page` applies `max_content_chars` and `max_links`, records the totals, and returns a `PageResult`.
9. `server/app.py` converts any `FetchError` into an MCP tool error whose message starts with the error code. Anything else is logged with a traceback and reaches the client as a generic failure, with details masked.

## Error model

`FetchError(code, message)` is the one expected failure type. Codes are stable strings listed in the README. HTTP error statuses are not errors: a 404 page, or a 503 that outlasted the retries, comes back as a normal result with its status.

Which layer raises each code, and what the retry loop and breaker do with it:

| Code | Raised by | Retried | Counts against the breaker |
| --- | --- | --- | --- |
| `invalid_url`, `redirect_loop`, `too_many_redirects` | `fetch/http.py` | no | no |
| `blocked_address` | `fetch/ssrf.py`, at connect | no | no |
| `network_error` for a name that does not resolve | `fetch/ssrf.py`, at connect | no | no |
| `network_error`, `timeout` from the transport | `fetch/http.py`, from httpx errors | yes | yes |
| `robots_disallowed` | `fetch/http.py` | no | no |
| `robots_unavailable` | `fetch/robots.py` | no | no, but the robots read behind it was retried and counted |
| `rate_limited` | `fetch/limits.py` | no | no |
| `circuit_open` | `fetch/limits.py` | no | no |
| `unsupported_content_type`, `extraction_timeout`, `render_unavailable` | `fetch/page.py` | no | no |

Only transport failures are retried, because only they can pass by themselves. A refusal that depends on the request, such as a blocked address or a disallowed path, would give the same answer again. A temporary DNS failure (`EAI_AGAIN`) is the one resolver error treated as a transport failure.

Errors from the robots.txt read itself (a timeout, a dead host, a blocked address, an open breaker) reach the caller unchanged and are not cached. They say nothing about the site's rules, and the page fetch would have failed the same way. Only an error status from a server that did answer becomes `robots_unavailable`.

## Storage

One SQLite file at `<data_dir>/weft.db`, in WAL mode with `synchronous=NORMAL`, `foreign_keys=ON` and a 5 second busy timeout.

All access goes through a single dedicated thread (`Database.run(fn)`). The connection never crosses threads, and callers serialise even when one is cancelled mid-call. Multi-statement work uses `with conn:` inside `fn`.

Migrations are an append-only list in `db.py`. Entry N takes the file from `user_version` N-1 to N, and the schema and the version bump commit together. A file with a newer `user_version` than the build knows is refused at startup.

Schema version 1 (created at startup, no code reads or writes these tables yet):

| Table | Purpose | Key columns |
| --- | --- | --- |
| `jobs` | Crawl and search jobs | `id`, `kind`, `status`, `params` (JSON), `error`, timestamps |
| `frontier` | URLs per job, ordered by score | primary key `(job_id, url)`, `depth`, `score`, `state`, `attempts`; cascades on job delete |
| `pages` | Page cache | primary key `url`, `final_url`, `status`, `etag`, `last_modified`, `content_hash`, `markdown`, `expires_at` |

Phase 4 will migrate these as the crawler needs. Expect a version 2. Nothing from Phase 2 is stored: the robots cache, limiter state and breaker state live in memory and start empty.

## Configuration

`Settings` (pydantic-settings). Precedence: init arguments, `WEFT_*` environment variables, TOML file, defaults. The TOML path is `WEFT_CONFIG`, falling back to `./weft.toml`. The CLI's `--config` sets `WEFT_CONFIG`. Unknown keys are rejected. The CLI prints validation problems by field name and never echoes the offending value.

Phase 2 added `allow_loopback`, `host_min_interval_s`, `host_concurrency`, `global_concurrency`, `max_retries`, `retry_backoff_s`, `breaker_threshold` and `extract_timeout_s`. Three values stay constants: the breaker's 30 second cooldown, the robots.txt cache time of an hour, and the minute that an unreadable robots.txt is remembered. Nobody had a reason to tune them, and a setting is easier to add than to take back.

## Logging

One JSON object per line on stderr, from the `weft` logger: `ts`, `level`, `logger`, `msg`, plus any `extra` fields. stdout is the MCP protocol channel under stdio, so nothing may print to it.

Secrets are redacted by field name (token, secret, password, authorization, cookie, session, signature, api key), including inside nested dicts and lists. URLs go through `safe_url`, which drops credentials and fragments and masks sensitive query values. Redaction works on field names, so keep sensitive values out of log messages.

Phase 2 adds two lines to the `weft.fetch` logger: a warning, `refused non-public address`, with the host and the addresses it resolved to (the caller is not told them), and an info line, `retrying`, with the attempt number and the reason. The existing `fetch failed` warning now also carries the new error codes.

## Security posture

In place:

- **Addresses.** `fetch/ssrf.py` wraps the HTTP client's connect step. On every new connection it resolves the name once, checks every returned address with `is_blocked`, refuses the whole name if any is blocked, and connects to one of the checked addresses by IP, so the client never looks the name up again. This covers redirect hops, robots.txt reads and DNS rebinding with no separate check. It also covers numeric spellings of an address, because the check runs on what the name resolves to. `is_blocked` refuses anything that is not a public unicast address. The standard library's `is_global` is not enough on its own, because it reports multicast addresses, the Azure wireserver (`168.63.129.16`) and NAT64 addresses as global. So the check adds explicit rules, refuses Teredo and 6to4 outright, and judges the IPv4 address inside IPv4-mapped and NAT64 (`64:ff9b::/96`) addresses. NAT64 is how IPv6-only networks reach IPv4-only sites, so it cannot be blocked wholesale.
- **Loopback.** Refused, unless `allow_loopback` is set. That setting opens `127.0.0.0/8` and `::1` and nothing else. The remote transport in Phase 6 must not enable it.
- **What the caller learns.** A refusal says that a name resolves to a non-public address. It does not say which one, so a probe cannot map internal DNS.
- **Proxies.** The HTTP client ignores `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY`. A proxy resolves names for itself and would sit outside the check. `SSL_CERT_FILE` still applies.
- **Politeness.** robots.txt on every hop, per-host spacing and concurrency, a global cap, retries with backoff, and a breaker per host. Weft has no setting to ignore robots.txt.
- **Existing controls.** URLs limited to http and https, no credentials in URLs, caps on time, size and redirect hops, no cookie persistence, error details masked from the client, secrets redacted from logs, and a server instruction telling the agent that page content is untrusted.

How the guard reaches the client: httpx has no public hook for how connections are opened, so `guarded_transport` replaces the network backend on the transport's connection pool, through a private attribute. If an httpx upgrade moves it, the guard fails closed, because the SSRF tests expect refusals and would fail. A dependency bump should run them.

`httpcore` is pinned to the 1.0 series in `pyproject.toml` because the guard depends on its private `_network_backend`. Raising that pin means re-running `tests/test_ssrf.py` first.

Known ceilings:

- Limits are per host name. `example.com`, `example.com.` and a second name for the same server are separate hosts. Per-host state in the limiter and breaker is never evicted. The robots cache holds 512 origins and drops the least recently used.
- robots.txt paths are compared as sent, without percent-encoding normalisation. Matching is plain string search, not regular expressions, because the file is controlled by the site.
- A hostname that resolves to several addresses is tried one address at a time, in resolver order, with no happy-eyeballs race.
- On Windows a numeric spelling such as `2130706433` fails to resolve at all, after about three seconds, so the error is `network_error` there and `blocked_address` on Linux and macOS. Nothing connects either way.
- HTTP/2 through the guarded transport has no test. The fixture can serve HTTPS, but it speaks HTTP/1.1 over it.
- A page that hangs extraction occupies one of four threads until it finishes. Four of them stop extraction.
- A gzip bomb is cut at the byte cap, but the check runs per decoded chunk, so memory can spike to about 1000 times one network chunk before the cut.

## Tests

`tests/fixture_site.py` runs a local HTTP server on a random port. It serves a sample article, redirect chains and loops, an endless redirect, oversized and gzip-bomb bodies, a slow response, a response that drips bytes, Latin-1 and meta-declared Windows-1251 pages, non-HTML types, error statuses and a cookie echo. Phase 2 added a `robots.txt` whose body (or status, or absence) each test can choose, a route that fails a set number of times before succeeding, a route that drops the connection, a route that records how many requests were in flight at once, redirects to metadata, private and IPv4-mapped addresses, and per-site counters for hits and request times, so a test can prove that a request did or did not reach the server. Later phases add a JavaScript-rendered page, a near-duplicate pair and an infinite link trap.

`tests/conftest.py` sets `WEFT_ALLOW_LOOPBACK=1`, `WEFT_HOST_MIN_INTERVAL_S=0` and a 10 millisecond retry backoff for every test, because the fixture site is on 127.0.0.1. Tests of the address guard, the limiter and the retry timing pass their own values. Init arguments beat the environment.

The address table, the robots matcher, the limiter and the breaker are tested as pure units with a fake clock or a fake resolver, with no sleeping and no network. The DNS rebinding cases use a fake resolver that gives a different answer on each call, and a fake inner backend that records what it was asked to connect to. End-to-end tests run the real `Fetcher` against the fixture site.

The fixture can also serve HTTPS. `tests/test_tls.py` makes a throwaway certificate with `cryptography`, valid for the name `localhost` and no IP address, and trusts it through `SSL_CERT_FILE`. Fetching `https://localhost:PORT/` can only succeed if the certificate is checked against the name in the URL while the socket goes to the pinned address. A second test serves a certificate for another name and expects the fetch to fail, so verification is known to be on.

`tests/test_server.py` includes a real stdio round trip: it spawns `python -m weft` and talks to it over pipes.
