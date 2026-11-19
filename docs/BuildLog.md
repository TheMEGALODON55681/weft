# Build log

## Phase 1: skeleton (2026-11-19)

### Done

Project scaffold (`pyproject.toml`, `.gitignore`, `docs/`), settings, JSON logging, the SQLite layer, the HTTP fetcher, content extraction, `fetch_page`, the stdio server and CLI, and 96 tests. The README and the other docs are written. Nothing was committed, tagged or pushed.

### Verification

- `pytest`: 96 passed (20 to 40 seconds)
- `ruff check .` and `ruff format --check .`: clean
- `mypy` (strict, `src/weft`): clean
- `uv build`: sdist and wheel built. The wheel installed into an empty virtual environment, and `weft --version` ran from it.
- Manual run of the installed `weft` command with stdin closed: exit code 0, nothing on stdout, a JSON `ready` line on stderr.
- The stdio test spawns `python -m weft` and calls `fetch_page` over real pipes.

### Decisions

- **Redirects by hand.** `follow_redirects` is off. Phase 2's SSRF guard has to vet every hop, and a hand-written loop gives a clean `redirect_loop` code instead of a generic error.
- **One database thread.** I considered `asyncio.to_thread` behind a lock. A cancelled caller releases the lock while its thread keeps running, which lets a second thread onto the same connection mid-transaction. A single-thread executor removes that case, and sqlite3's own thread check then guards the connection.
- **Schema created before any consumer.** The handoff puts the SQLite schema in Phase 1. The server opens the database at startup, so an unwritable data directory or a newer schema fails at startup. The tables stay unused until Phase 4 and will need a version 2 migration.
- **Decode order.** BOM, header charset, `<meta>` charset, UTF-8, statistical detection, then replacement characters. The labels `iso-8859-1`, `latin1` and `us-ascii` decode as windows-1252, as browsers do.
- **Output caps.** `max_links` and `max_content_chars` stop one large page from flooding an agent's context. The result reports the pre-cap link total and a truncation flag for each cap.
- **No cookies.** The shared HTTP client would otherwise keep a jar for the life of the process and carry one caller's session into another's. The jar policy now accepts and returns nothing. Authenticated crawls will pass cookies per job.
- **Errors.** Expected failures are `FetchError` with a stable code, surfaced as tool errors. HTTP 4xx and 5xx are results. Unexpected exceptions are masked from the client and logged with a traceback.
- **Thin pages are not errors.** trafilatura returns text from navigation when a page has no article, so a JavaScript shell comes back thin, not empty. Phase 3's render policy will key on word count.
- **Scope kept narrow.** No cache, no metrics and no rendering in this phase, in line with the phase list.

### Problems hit

- The first editable install ran before `src/weft` held any files and left no `.pth` entry, so imports failed. Reinstalling after the package existed fixed it.
- fastmcp 4 renamed protocol fields to snake_case (`read_only_hint`, `output_schema`, `input_schema`). The camelCase names still work but warn.
- mypy reported seven missing arguments on `Settings(...)`. `Field(10.0, gt=0)` passes the default as the first positional argument, and the type checker does not read that as a default. `Field(default=10.0, gt=0)` fixed it.
- Building an `httpx` client costs 0.4 to 0.7 seconds here because of TLS context setup. Production builds one. The tests build one per case, which is most of the suite's runtime. I left it alone.
- My first JS-shell test asserted empty Markdown and failed, because trafilatura falls back to navigation text. The test now asserts fewer than five words.
- Scripted edits on Windows wrote CRLF into nine files. They are normalised to LF and `ruff format` is pinned to `line-ending = "lf"`.
- The `urllib3` SOCKS warning printed to stderr on every start. `weft/__init__.py` filters that one message.

### Review

I reviewed the code in this session from four angles (correctness, security, concurrency, maintainability) and reconciled the findings, then ran a separate YAGNI pass.

Fixed:

- **Cookie jar persisted across fetches.** Found by the security pass. Test added first and watched failing.
- **Log redaction skipped lists.** A list of header dicts logged cookies in the clear. Redaction now recurses into lists and tuples.
- **Connection leak on a failed migration, and lifespan cleanup order.** The connection closes on any open failure, and the server closes what it opened if a later step raises.
- **`content.py` imported a private helper from `links.py`.** It is now public as `resolve_url`.
- **Server instructions now tell the agent that page content is untrusted.**
- **Test files carried `type: ignore` comments** although mypy only checks `src/weft`. Removed.
- **YAGNI pass:** the charset regex became `Response.charset_encoding`, three duplicate failure logs became one, and the redirect loop lost its counter and an unreachable assertion. The untested "image link first, text link later" branch in link extraction got its test.

Accepted and recorded, not fixed:

- No SSRF guard, robots.txt, rate limit or global concurrency cap. All are Phase 2, and the README says so before the install section.
- Decompression can spike to about 1000 times one network chunk before the byte cap cuts it.
- Extraction has no timeout.
- The page is parsed four times (trafilatura twice, selectolax twice). Fine until profiling says otherwise.
- HTTP/2 is untested.

The CLI tests were written after the CLI code. I checked them by mutation: adding the rejected value to the error message makes `test_invalid_setting_names_the_field_and_hides_the_value` fail. All other modules were written test-first and watched failing.

The jury was one reviewer playing four roles, not four independent reviewers. An outside review would catch what I share blind spots on.

### Commit message draft

```
Phase 1: skeleton with fetch_page over stdio

Add typed settings, JSON logging to stderr, a versioned SQLite layer in WAL
mode, an HTTP fetcher with manual redirect handling and byte, time and hop
caps, encoding recovery, Markdown and link extraction, and a stdio MCP server
exposing fetch_page. Tests run against a local fixture site. Docs cover
architecture, phases, build log and usage.
```

## Phase 2: safety and politeness (2026-11-19)

### Done

The SSRF guard, robots.txt, per-host pacing and concurrency caps, a global cap, retries with backoff and jitter, a circuit breaker per host, and a timeout on extraction. The README's SSRF warning is gone. Five new modules in `fetch/` (`errors`, `ssrf`, `robots`, `limits`, `retry`), a restructured request path in `http.py`, eight new settings, a TLS fixture, and 312 tests. Nothing was committed, tagged or pushed.

### Verification

- `pytest`: 312 passed (about 100 seconds, up from 22: every end-to-end test builds an httpx client, and Windows takes about two seconds to refuse a loopback connection)
- `ruff check .` and `ruff format --check .`: clean
- `mypy` (strict, `src/weft`): clean
- `uv build`: sdist and wheel built into a scratch directory. The wheel holds the five new modules and installed into an empty virtual environment, and `weft --version` ran from it.
- The installed server, run over real stdio with default settings, refused four targets with `blocked_address`: the cloud metadata address, loopback, an IPv4-mapped private address and a plain private address. No outbound traffic was possible, and no process was left running.
- Mutation checks. I made about fifty-five small breaking changes to the guard, the parser, the limiter, the breaker, the retry loop, the extraction change and the TLS setup, one at a time, in a scratch copy of the source, and ran each against the tests that should catch it. Five survived their first run. Four exposed weak tests, which I fixed (see Problems and Review). The fifth is accepted: removing the IPv4-mapped unwrap in `is_blocked` changes nothing on this Python, because `ipaddress` already handles mapped addresses here. The unwrap stays because that handling has differed between releases.
- Not verified: HTTP/2 (the TLS fixture speaks HTTP/1.1), and any browser behaviour (Phase 3).

### Decisions

- **The address check lives at connect time.** httpx follows redirects and opens connections on its own schedule, so a check before the request leaves a gap: the client looks the name up again and may get a different answer. `GuardedBackend` wraps httpcore's network backend. It resolves once, checks every address, and connects to a checked address by IP. One code path covers the first request, each redirect hop, each robots.txt read and DNS rebinding. The cost is a private attribute: httpx offers no hook, so `guarded_transport` assigns `pool._network_backend`. If an upgrade moves it, the SSRF tests fail, so the guard cannot turn off unnoticed.
- **`ipaddress.is_global` is not enough.** A probe showed that it calls multicast addresses, the Azure wireserver (`168.63.129.16`) and NAT64 addresses global. The check adds explicit rules, refuses Teredo and 6to4, and judges the IPv4 address inside mapped and NAT64 addresses. NAT64 is unwrapped, not blocked, because IPv6-only networks with DNS64 use it for every IPv4-only site.
- **One bad address refuses the whole name.** The operating system would be free to choose it. The error says the name resolves to a non-public address and nothing more, so a caller cannot map internal DNS. The addresses are in the log.
- **`allow_loopback`, not `allow_private`.** The tests need loopback. Nothing needs the rest, and a wider opening is easier to add than to remove. The default is off. The setting is for local development, and the remote transport must never enable it.
- **Proxy variables are ignored.** A proxy resolves names itself, so honouring `HTTP_PROXY` would put requests outside the address check. This build cannot be used behind a mandatory proxy. `SSL_CERT_FILE` still works, which the TLS tests confirm.
- **robots.txt has its own parser.** The standard library's matches rules in file order rather than by length and has no `*` or `$`. Matching is plain string search over the `*`-separated parts, not a regex, because the site controls the file and chained `.*` can backtrack for a very long time. A test covers a 2000-wildcard pattern.
- **robots.txt statuses follow RFC 9309.** Weft parses a 2xx, reads any other 4xx as no restrictions, and treats anything else as off limits for a minute. It applies the rules on every redirect hop. `/robots.txt` itself is always fetchable. There is no setting to ignore the file. Errors from the read itself pass through unchanged (see Problems).
- **A turn at the host, or a fast refusal.** Spacing is per host name: reserve a start time, sleep until it, then take a global slot. The host's slots come first so a host that is being made to wait never holds global capacity. If the wait would exceed `total_timeout_s`, the call fails at once with `rate_limited` and the wait time, instead of blocking an agent. `Crawl-delay` uses the same mechanism, so there is no separate cap on it. A crawl delay of an hour gives one fetch, then fast refusals.
- **robots.txt reads are not spaced.** Otherwise every first page on a new host would wait a full interval for the file that was just read.
- **The breaker has no probe flag.** After the cooldown the first caller through pushes the deadline out by another cooldown. A probe that is cancelled or times out costs one extra cooldown and cannot wedge the breaker. Only transport failures and `429`, `502`, `503`, `504` count. A plain `500` or `404` does not, so one broken page cannot close a site. Refusals by the guard or by robots.txt never count, because no request was made.
- **Retries cover what can pass by itself.** Transport failures and the same four statuses, with half-to-full jitter. `Retry-After` is honoured when the wait fits in what is left of `total_timeout_s`; otherwise the response is returned as it is. A name that does not exist is permanent and is not retried. A temporary DNS failure is.
- **Extraction timeout frees the caller, not the thread.** Python cannot stop a thread. Extraction gets a pool of four threads of its own so that stuck pages cannot use up the default executor, which also runs DNS lookups.
- **Two settings were cut.** The robots.txt cache time and the breaker cooldown were settings until the YAGNI pass showed that nothing set them. They are parameter defaults now.
- **Test defaults come from the environment.** `conftest.py` sets `WEFT_ALLOW_LOOPBACK=1`, no host spacing and a 10 ms backoff, because the fixture site is on 127.0.0.1. Tests of the guard, the limiter and the retry timing override them. The stdio test sets the same variables for its child process itself, because the child gets a minimal environment, not the test's.

### Problems hit

- A test for the new positive-only settings passed before the settings existed. `extra="forbid"` raises the same `ValidationError` that the test expected for a zero. It now sets a valid value first.
- Importing `weft` from `conftest.py` made the urllib3 SOCKS warning reappear. pytest throws away warning filters that are installed while it loads conftest, so the filter in `weft/__init__.py` was gone by the time trafilatura imported urllib3. The filter is now also in the pytest config.
- The first run of the numeric-host tests (`http://2130706433/` and friends) failed on Windows. Windows' resolver refuses those spellings, after about three seconds, where glibc turns them into `127.0.0.1`. My test had assumed one behaviour. It now expects `blocked_address` on Linux and macOS and `network_error` on Windows. Nothing connects either way.
- Those same tests then failed once retries existed, because a failed lookup took three tries and ran past the test's time budget. That exposed a real policy problem: retrying a name that does not exist triples the delay for every mistyped or invented host name, which agents produce often, and can never succeed. A name that does not resolve is now a plain `network_error` that the retry loop does not see, and only a temporary DNS failure is retried. I made that change before writing its test. That is backwards for test-first work. I wrote the tests afterwards and checked them by putting the old behaviour back in a scratch copy: each one fails against it.
- Tracing that also showed a bug in my robots design. `RobotsCache` converted a `timeout` or `network_error` raised by the read into `robots_unavailable`, but the HTTP layer raises httpx errors, which only become `FetchError` at the top of `fetch()`. The branch could never run, and my unit tests were exercising a path that does not exist. I removed it. A dead host now reports `network_error`, which is more useful to the caller anyway.
- Mutation checks found weak tests, each fixed. A fixture with equal-length rules meant that merging the `*` group into a named group changed nothing. No pattern had overlapping segments, so not advancing past a matched segment went unnoticed. Three timing tests started their clock before building the fetcher, so the half-second cost of the client satisfied a "waited at least this long" assertion with no waiting at all. One of them could also have failed on a slow machine.
- The extraction pool test failed in the full run and passed alone. The previous test's hung thread still held one of the four workers. The hung thread now waits on an event that the test sets, and the pool test asserts a range.
- A test that opened the breaker while requests were queued failed with three hits where one was expected. See Review.
- Windows takes about two seconds to refuse a connection to a closed loopback port, and `localhost` tries `::1` first on every new connection. Both add up now that failures get retries. I set `max_retries=0` in tests that check error mapping, moved the cross-host global cap check into a unit test with no network, and cut the suite from about 110 seconds to about 90 before adding TLS.
- I wrote in the README that Weft ignores `SSL_CERT_FILE`, because I set `trust_env=False` on the client. The TLS transport has its own `trust_env`, which defaults to true, and reads the variable. I read the httpx source, corrected the README and the code comment, and the TLS tests now cover it.
- Every end-to-end test before the TLS ones was cleartext. If connecting to a pinned address had broken certificate checking, no HTTPS site would have worked and nothing would have noticed. `tests/test_tls.py` serves a certificate valid for `localhost` only, so `https://localhost:PORT/` succeeds only if the name in the URL is what gets verified. A second test with a certificate for another name must fail. Switching verification off, or stopping the transport reading `SSL_CERT_FILE`, makes them fail. `cryptography` is now a dev dependency, and `httpcore` is declared as a direct dependency.

### Review

I reviewed the code in this session from four angles (correctness, security, concurrency, maintainability) and reconciled the findings, then ran an over-engineering review.

Fixed:

- **The breaker did not stop requests already queued for a host.** It was checked before a request queued for its turn, not before it was sent. With two slots and a burst of parallel fetches, callers three and up passed while the breaker was closed, waited, and were sent after it had opened. Found by the concurrency pass. The breaker now has `peek`, which fails fast before queuing and admits nothing, and `check`, which runs inside the slot right before sending and is where the single probe is admitted. Three tests: queued requests are not sent, an open breaker refuses at once instead of waiting for the interval, and `peek` leaves the probe alone. I watched the first and the third fail before the fix. The second came after a mutant of the fix survived: it fails when `peek` is removed.
- **A stale comment and an out-of-date tool description.** The `http.py` docstring still said the guard would arrive later. The tool description that the calling agent reads did not mention that Weft can now decline, or which codes are not worth retrying.
- **Over-engineering review.** Cut two settings (above), replaced a get-or-create helper in the limiter with a `defaultdict`, and removed four end-to-end SSRF cases that repeated the address table through a half-second client each. Kept: the `peek` and `check` split, the injected clocks (they replace sleeping tests), the bounded robots cache, the extraction pool, `errors.py` (it breaks an import cycle) and `retry.py`.

Accepted and recorded, not fixed:

- Limits are per host name. Aliases of one server are not merged, so a caller could get around spacing by alternating `example.com` and `example.com.`. Per-host state in the limiter and breaker is never evicted.
- robots.txt paths are compared as sent, without percent-encoding normalisation.
- A page that hangs extraction takes one of four threads until it finishes, and four stop extraction. A child process that can be killed is the fix.
- HTTP/2 through the guarded transport is untested.
- Redirect hops and retries each count against the host's interval, so an http-to-https redirect on one host costs a second with the default settings.
- The guard reaches into private httpx and httpcore attributes. `httpcore` is pinned to 1.0.x, and raising the pin means re-running `tests/test_ssrf.py` first.

The jury was again one reviewer playing four roles, not four independent reviewers. The shared blind spots are the same as in Phase 1, and an outside review would still be worth having before Phase 6 puts this on a network.

### Commit message draft

```
Phase 2: SSRF guard, robots.txt, rate limits, retries and circuit breaker

Resolve host names in a guarded connect step that checks every address and
connects to one it checked, so redirect hops, robots.txt reads and DNS
rebinding are covered by one code path. Refuse private, loopback, link-local,
metadata and other non-public ranges, with allow_loopback as the single
explicit opening. Ignore proxy environment variables.

Obey robots.txt on every hop with an RFC 9309 parser and matcher, a per-origin
cache and Crawl-delay. Space requests per host, cap concurrency per host and
overall, and refuse a wait longer than the call's time budget. Retry transport
failures and 429/502/503/504 with backoff, jitter and Retry-After. Add a
per-host circuit breaker that also stops requests already queued.

Bound page extraction with a timeout and a worker pool of its own. Remove the
README's SSRF warning. Add a TLS fixture and tests that HTTPS is still verified
against the host name. Docs updated.

Pin httpcore to the 1.0 series, because the SSRF guard replaces the network
backend on its connection pool through a private attribute, and note in the
architecture doc that raising the pin means re-running tests/test_ssrf.py first.
Add .gitattributes so text files are checked out with LF on every platform.
```
