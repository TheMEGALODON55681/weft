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

## Phase 3: rendering (2026-11-19)

### Done

A guarded local proxy for Chromium, a browser pool with reuse and hard limits, navigation routing through the Fetcher, and the render policy for `fetch_page`. `render="always"` works now. Two new modules (`fetch/proxy.py`, `fetch/browser.py`), a one-hop mode on `Fetcher.fetch`, five new settings, Playwright as a dependency, and 361 tests. The session started from tests for the proxy and fixture additions that an interrupted earlier session had left uncommitted. I kept those tests, fixed one and added four. Nothing was committed, tagged or pushed.

No Chromium was downloaded. Playwright 1.65.2 was already in the virtual environment, and the headless shell it drives (build 1255) was already in the local Playwright cache.

### Verification

- `pytest`: 361 passed, none skipped (about 165 seconds, up from 100: each browser test starts its own Chromium)
- `ruff check .` and `ruff format --check .`: clean
- `mypy` (strict, `src/weft`): clean
- `uv build`: sdist and wheel built into a scratch directory. The wheel holds `fetch/proxy.py` and `fetch/browser.py` and declares `playwright>=1.65,<2`.
- A real stdio server (`python -m weft`) fetched the JavaScript shell with `render="auto"` and with `render="always"`. Both came back as `fetch_method: "browser"` with the rendered text.
- Processes. Before the first test there were no `chrome-headless-shell.exe` processes, and one `node.exe` that was not mine. After every browser run, the full suite, every mutation run and the stdio check, I checked again with `tasklist`: no Chromium, no Playwright driver and no stray Python were left.
- Mutation checks. I removed each defence in turn and ran the tests that should catch it. Eight of nine were caught: the proxy skipping the address check, removing `<-loopback>` (with Playwright's own default turned off by its environment variable), the WebRTC policy, service worker blocking, routing navigations through the Fetcher, the refresh used in place of a 3xx, the page cap and recycling. The survivor is the resolver rule. It is a second layer behind the proxy, and no test can bypass the proxy to exercise it.
- One mutation run hung until its 300 second limit. The same mutation passed in five seconds when run again, and no process was left behind either time. I could not reproduce it and have no cause for it.
- Not verified: Linux and Docker (Phase 6), HTTP/2 through the proxy, and memory use under load (Phase 7).

### Decisions

- **The browser never fetches the document.** Every mode starts with a plain fetch, and the browser is handed that body. Robots.txt, the address guard and every redirect hop of the document are handled once, by code that already has tests, and a render costs no second request to the site.
- **The address check sits in a proxy.** Playwright's routing does not see WebSockets, service worker traffic or redirects that follow a fulfilled response. An HTTP proxy sees every TCP connection Chromium makes for the web. It reuses `GuardedBackend`, so there is one address check in the codebase, not two.
- **Navigations go through the Fetcher; sub-resources do not.** The handoff asked for robots.txt, limits and the breaker on navigations. Pacing every image at one per second would make rendering useless, so sub-resources get the address check only.
- **Redirects become a refresh.** Chromium follows a fulfilled 3xx without asking the route again (see Problems). The route answers a redirect with a 200 page that refreshes to the target, so each hop is a new navigation and reaches the Fetcher.
- **Belt and braces in Chromium.** `--host-resolver-rules` stops Chromium resolving anything but the proxy's address. `<-loopback>` is passed explicitly even though Playwright adds it, because Playwright lets an environment variable switch its default off. WebRTC is limited to proxied traffic.
- **One browser, many contexts.** A process per render would cost a second or more each time. A context per render keeps cookies and storage from crossing calls. Recycling after `browser_recycle_after` pages caps slow growth. A retired browser closes when its last page finishes, so recycling never cuts off a page in flight.
- **Hard limits that Chromium enforces.** The page cap is a semaphore, and the heap cap is V8's own `--max-old-space-size`, which kills a tab that passes it. A tab spinning in a loop is cut off by `render_timeout_s`, and the browser is then reused.
- **The render policy keys on scripts.** A page with no `<script>` cannot gain content in a browser, so `"auto"` never renders it however thin it is. With a script, it renders under `render_min_words` words, or when a `<noscript>` mentions JavaScript, the usual mark of a single-page app that has navigation text.
- **`auto` is best effort.** If the browser is missing or the render fails, `"auto"` returns the HTTP result, and `fetch_method` says so. Only `"always"` reports render errors.
- **The browser starts lazily.** A server that never meets a JavaScript page never starts Chromium, and a machine without Chromium still serves plain fetches.
- **`render_timeout_s` is its own budget,** after `total_timeout_s`. The plain fetch already spends that budget, and rendering includes a cold start. The worst case for one call is the sum of the two plus extraction.

### Problems hit

- **The resolver rule cut off the proxy.** My first launch used `--host-resolver-rules=MAP * ~NOTFOUND`, and every sub-resource failed. The rule also maps `127.0.0.1`, the proxy's own address. The positive controls in the SSRF test caught it: they require an allowed request to arrive, so a browser that could reach nothing did not pass as safe. `EXCLUDE 127.0.0.1` fixed it.
- **Playwright's service worker block is a stub.** `service_workers="block"` replaces `navigator.serviceWorker.register` with a function that resolves, so the page believes it registered a worker. My first test expected a rejection and failed. The test now checks the network instead (`/sw.js` is never requested), and it also calls `register` through `ServiceWorkerContainer.prototype`, which would get past a stub on the navigator object. Both stay blocked, and setting `"allow"` makes the test fail.
- **Redirects after a fulfilled response skipped robots.txt.** The robots test caught a frame whose redirect target, under `/private/`, reached the fixture. A debug run showed the route handler called once for the first hop and never for the second: Chromium followed the fulfilled 302 itself, through the proxy. The address check still applied, but robots.txt and pacing did not. The refresh page fixed it, and the test now fails if a 3xx is fulfilled again.
- **The WebRTC test read the page too early.** Its two-second timer outlasted the network-idle wait, so the page was captured before the script finished. It now marks the page once gathering starts and waits a second on the UDP socket.
- **WebTransport.** It runs over QUIC, which is UDP, so I expected a second hole. With every Weft flag removed, Chromium still refused it behind a proxy and sent no datagram. A test keeps that true.
- **A fixture file had CRLF endings** from the interrupted session's edits. It is LF again.
- **A hit count on the shared fixture site.** The interrupted session's proxy test asserted `hits("/ua") == 1` on the session-wide site, so it depended on test order. It now compares before and after.

### Review

I reviewed the code in this session from four angles (correctness, security, concurrency, maintainability) and reconciled the findings, then ran an over-engineering review.

Fixed:

- **A closed pool could relaunch Chromium.** Found by the concurrency pass. A render queued behind `aclose` would find no browser, start the driver again and launch one, and nobody would close it. The pool now marks itself closed under the same lock, and a later render gets `render_unavailable`. Test written first and watched failing.
- **Redirect hops escaped the Fetcher.** See Problems. Found by a test, then confirmed by the security pass as the only path where a navigation reached the proxy.
- **Error logging could raise.** `str(exc).splitlines()[0]` fails on an empty message. A small helper takes the first line or nothing.
- **The `<noscript>` branch of the render policy had no test.** `wants_render` now has a table test covering all five cases.
- **The proxy forwarded the client's `Host` header.** It now takes `Host` from the URL, whose address was the one checked. The old behaviour was not an SSRF, because the connection still went to the checked address, but it sent a misleading request upstream. Test added.
- **Over-engineering review.** Skipped: proxy authentication, an init script that removes `RTCPeerConnection`, `--disable-quic`, and a byte counter in the proxy. The flags already in place cover the first three, and the tests show it. The counter has no caller until a crawl needs per-job byte budgets. Kept: the `_Instance` record (it carries the use and in-flight counts that make recycling safe) and `open_browsers` (three lines, and the only way a test can show that retired browsers closed).

Accepted and recorded, not fixed:

- Sub-resources are unpaced and have no byte budget, only a time budget.
- The loopback proxy has no authentication.
- The heap cap covers JavaScript only.
- A rendered page runs without the origin's headers, Content-Security-Policy included.
- `status` in a rendered result is the document's status, even after a script navigates elsewhere.
- The branch that replaces a browser after a context will not close has no test.
- `render="auto"` may render pages that have a `<noscript>` JavaScript notice beside a full article. That costs time, not correctness.

The jury was again one reviewer playing four roles. The proxy and the browser flags are the parts most worth an outside read before Phase 6 puts this on a network.

### Commit message draft

```
Phase 3: headless rendering behind a guarded proxy

Add a browser pool that renders pages in headless Chromium when plain HTTP
is not enough. render="auto" renders a page that has a script and thin text,
or a noscript notice asking for JavaScript, and falls back to the HTTP result
if rendering fails. render="always" renders every HTML page and reports
render_unavailable or render_failed. render="never" never starts a browser.

Chromium makes no connection of its own. The document is the body the Fetcher
already fetched. Every other navigation is fetched by the Fetcher one hop at a
time, so robots.txt, host spacing, the circuit breaker and the address guard
apply to frames, script navigations and each redirect hop. All other traffic
goes through a local proxy that connects through the same GuardedBackend as
plain HTTP. Chromium is launched with no loopback bypass, no name resolution
of its own and no WebRTC UDP outside the proxy, and service workers are
blocked.

One browser is reused with a fresh context per render, capped at
browser_max_pages pages at once, replaced after browser_recycle_after pages,
with a JavaScript heap cap per tab and a per-render timeout. Shutdown closes
every browser and the driver, and a closed pool launches nothing.

Add a one-hop mode to Fetcher.fetch, five settings, Playwright as a
dependency, proxy and browser tests that prove blocked hosts are never
contacted, and docs.
```

## Phase 4: crawling (2026-11-21)

### Done

The frontier, goal scoring, dedupe, trap limits, `crawl_site`, `map_site`, the job tools and resume, conditional requests and the page cache. Five carried gaps from Phases 2 and 3 are closed: an LRU for limiter and breaker state, a crawl time budget of its own, render tests that skip without Chromium, Playwright pinned to its minor release, and a byte budget and connection spacing for each render's sub-resources. Three new packages (`frontier/`, `jobs/`, `rank/`), `fetch/cache.py`, schema version 2, eight settings, five MCP tools, and 461 tests. Nothing was committed, tagged or pushed.

### Verification

- `pytest`: 461 passed in 176 seconds, no warnings. An earlier full run of 456 tests passed in 171 seconds with no warnings too. A run before those reported "2 warnings" that were never identified (see Problems hit).
- `ruff check .` and `ruff format --check .`: clean
- `mypy` (strict, `src/weft`): clean
- `uv build`: sdist and wheel built. The wheel holds `frontier/` (6 files), `jobs/` (3), `rank/` (2) and `fetch/cache.py`, and its metadata requires `playwright>=1.65.2,<1.66`.
- Real stdio. `python -m weft` started as a child process, driven by a client script against the fixture site. Six tools listed. A 15-page `crawl_site` for "pricing plans" ended `done` with `max_pages`: 10 pages ok, 3 duplicates, 2 failed (`http_404` and `unsupported_content_type`). The pricing section came first (`/site/pricing/enterprise` 0.54, `/site/pricing` 0.51, the home page 0.0). One progress notification arrived, because the crawl took less than the one-second reporting interval. `fetch_page` on a crawled page returned `cached: true`. `map_site` returned 16 URLs from 4 sitemap files, and `list_jobs` showed the job.
- Kill and resume. `tests/test_crawl.py` starts a crawl in a child Python process, kills it (`TerminateProcess` on Windows) once three pages are in, opens the same database in the test, resumes the job and checks that it finishes all twelve pages with at most two fetched twice. A second test does the same through the MCP server: stop mid-crawl, start a new server on the same data directory, watch the job finish.
- Traps. The fixture's endless archive and endless calendar were crawled with `max_pages=500` and `max_depth=20`, so neither budget could be what stopped them. The crawl ended by itself (`exhausted`) after eleven archive pages and at most 21 calendar pages, under 60 pages in all. The trap pages carry text that differs for every page, so dedupe could not be what ended them either.
- Near duplicates. Of the pair one date apart and the copy with other spacing, exactly one is kept and the other two point to it.
- Mutation checks. 25 hand-written mutants of the crawl, frontier, cache, limiter and proxy code, each applied in a scratch copy of `src` and `tests` and run against the tests meant to catch it. 21 were caught by the first run. Four survived, and each was a gap in the tests, not an equivalent mutant: the crawler never had its use of `repeats_segments` tested (only the function was); the exact text hash was redundant for every page the tests used, because simhash also catches an exact copy once a page has 30 words, so only a short page needs it; the time budget carried across restarts, a stated decision, had no test; and nothing reached the `WHERE status = 'running'` guard that keeps a late `done` from overwriting `cancelled`. Five tests close them (and a fixture page `/site/edge` with a looping path and two short pages of identical text). A fifth mutant, the saved running time dropping earlier runs, was added for one of them. All five are now caught, 26 of 26. The score test caught the removal of anchor text from link scoring, but `test_the_goal_is_crawled_first` does not.
- Processes. After the last full run there is no `python` or `chrome-headless` process. Three `node.exe` processes remain, the same three that were running before this session started any test.
- Not verified: Linux and Docker (Phase 6), crawls of more than a few hundred pages, and memory use under load (Phase 7).

### Decisions

- **Canonical form is what gets fetched.** The crawler fetches the canonical URL, not the link as written. One URL per page in the frontier, the cache and the results is worth more than the rare site that cares about query order. Path case, trailing slashes and `www.` are left alone, because servers do treat those differently.
- **Every frontier step commits on its own.** A kill can leave only rows marked `fetching`, and the next run puts them back. There is no in-memory queue to lose. The cost is one small transaction per state change, which is nothing next to a fetch.
- **Statistics are computed, not stored.** `get_job` aggregates the frontier rows on each call. Counters kept in memory or in the `jobs` row would need their own crash handling. The one exception is `run_s`, the running time, because it cannot be derived: it is written after every page.
- **The job's time budget spans restarts.** `crawl_timeout_s` is running time summed across runs, so a job that keeps being restarted still ends.
- **Crawl fetches get their own budget.** `Fetcher.fetch` takes `timeout_s`, which replaces `total_timeout_s` for both the request and the longest wait for the host's turn. An agent calling `fetch_page` still gets a fast `rate_limited`. A crawl gets `crawl_fetch_timeout_s`, two minutes by default, so a site with a `Crawl-delay` of a minute is crawled at that pace instead of failing every other page.
- **Link scores use what is known before fetching.** Anchor text, URL words and the parent's relevance. No embeddings, no second fetch. Page relevance is BM25 against the goal with corpus statistics that grow as the crawl does. Those statistics are not saved. Rebuilding them on resume would mean storing term counts per page for a small gain in score stability.
- **A small stemmer of my own.** Six suffix rules, enough for `pricing` to meet `price` and `salaries` to meet `salary`. A real stemmer would be a dependency for the same result on goal words.
- **Traps are ended by shape, not by count per pattern.** I first meant to cap URLs per pattern, which would have cut off a blog with more than a few dozen posts. A cap on links in a row that share their parent's pattern stops calendars and endless archives, which chain, and leaves sibling links alone, which do not. Depth and the page budget bound the rest.
- **The simhash threshold is set for short pages.** A test with random text showed a one-word change moving a 60-word page about 6 bits and a 300-word page about 3. The classic 3-bit threshold missed the fixture's near-duplicate pair at 5 bits. Unrelated texts sit around 32 bits apart, so 6 leaves a wide margin. Pages under 30 words use the exact hash only.
- **A canonical link makes the page an alias, unless the target is an alias already.** Two pages naming each other would otherwise both be thrown away.
- **robots.txt is checked at enqueue only from cache.** For a same-site crawl the rules are always cached after the first page, so disallowed links never take a slot. Reading robots.txt for every new host a page links to would cost more than it saves. The fetch checks again regardless.
- **Cache keyed by render mode.** A page cached from a plain fetch must not answer a call that asked for a browser. The cost: a crawl and a `fetch_page` call share entries only when their `render` matches, which it does with the defaults.
- **Error pages are results whatever their type.** A `404` with a plain-text body used to raise `unsupported_content_type`. That broke the rule that HTTP error statuses come back as results, and it made crawl metrics report missing pages as the wrong kind of failure.
- **Sitemaps are read by pattern.** `<loc>` and `<lastmod>` by regular expression, never through an XML parser. There are no entities to expand, so a billion-laughs file is just text. The cost is sitemaps that prefix their tags with a namespace.
- **A proxy per render.** Byte budgets and pacing need to know which render a connection belongs to. Playwright can give each context its own proxy, so each render starts a `GuardedProxy` on a fresh port and closes it when the render ends. The browser-wide proxy stays, because Chromium on Windows needs one for per-context proxies to work. It now only sees traffic that belongs to no context.
- **Eviction never forgets a host that matters.** The limiter drops a host only when no request holds or waits for its slots and it is owed no spacing. The breaker drops only closed breakers. The cap can be passed while more hosts than that are busy, which is better than a crawl over many hosts quietly losing its pacing.
- **Workers run in a TaskGroup.** See Review.
- **A proxy's shutdown does not wait on idle clients.** See Problems.

### Problems hit

- **My scripted edits wrote CRLF again.** Python's `write_text` on Windows translates newlines. `ruff format` fixed the Python files it touched, but `ruff format --check` caught one module, and a byte scan found two docs. All three are LF again, and my later edits write bytes.
- **Closing a proxy could hang until the render timed out.** `test_an_allowed_page_cannot_reach_blocked_hosts` failed in two of four full runs and about one run in four on its own, always as a 20 second render timeout. Attaching Playwright event listeners made it pass every time, so I logged plain timestamps instead. Every proxied connection had finished within half a second, the page had loaded and its context had closed, and then `GuardedProxy.aclose()` never returned. The likely trigger is an idle connection: Chromium can open sockets to a proxy before it needs them. When one had been accepted but its handler task had not started yet, `aclose` cancelled the handlers it knew about, the late one then started and waited for a request head that never came, and Python 3.12's `Server.wait_closed()` waited for it. Phase 3 never saw this inside a render, because its one proxy closed only at shutdown. The one mutation run in Phase 3 that hung for 300 seconds with no cause found was most likely this. A proxy test with idle raw sockets reproduces it every time. The fix: `aclose` sets a flag that a late handler checks first, yields once so late handlers start and leave, then cancels the rest, and bounds `wait_closed` at a second as a fallback. The fallback never fires in the tests (six rounds take 0.02 seconds). The browser pair then passed ten runs out of ten.
- **A new host could evict itself.** The first LRU inserted the host and then evicted, so with every older host busy, the newest entry was the idle one dropped, and its reservation was lost. The test for "a host still owed spacing is kept" caught it. Eviction now makes room before inserting.
- **Dedupe ended the trap before the trap limit did.** The first trap pages shared one paragraph, so page two was a near duplicate of page one and was never expanded. The test passed for the wrong reason. The trap pages now carry text that differs for every page, and the test shows the pattern-run cap at work.
- **Two jobs created in the same clock tick sorted at random.** `time.time()` on Windows moves in steps of about 15 ms. `list_jobs` now breaks ties by insertion order.
- **The kill test's child could not start.** It got a minimal environment, and `Path.home()` failed at import with no `USERPROFILE`. The child now inherits the test's environment, which conftest has already cleaned of the host's `WEFT_*` variables.
- **A refused download looked like a success.** The byte budget test counted fetches that resolved, and a fetch the proxy refuses with a 403 still resolves. It now counts only `ok` responses. The proxy had been right all along.
- **BM25 ranked the enterprise page above the pricing page** for the goal "pricing". It says "pricing" three times in 35 words. The test now checks that the pricing section leads, not which page of it.
- **The first mutation harness could have reported a perfect score by accident.** It counted any non-zero pytest exit as a caught mutant, so a collection error, a timeout or an import from the real tree instead of the copy would all have looked like successes. It now checks that the scratch copy is what gets imported, requires every test selection to pass unmutated first, counts only a failing test as caught, and reports a hang or an error on its own line. It also reads and writes bytes, so the copy keeps its line endings.
- **Two pytest warnings were never identified.** One full run reported "2 warnings". The run that would have named them was interrupted, and the two full runs since reported none. Nothing in `src` prints or warns on its own. The cause is unknown and the warnings did not come back. `addopts` already has `-ra`, so a run that shows them will name them.
- **A test asserted a running time to the wrong precision.** `JobStats.run_s` is rounded to a tenth of a second, so a short crawl after 20 seconds of earlier runs reports exactly 20.0. The test checks `>= 20.0`, which still fails if the earlier time is dropped.

### Review

I reviewed the code in this session from four angles (correctness, security, concurrency, maintainability) and reconciled the findings, then ran an over-engineering review.

Fixed:

- **Workers outlived a failed job.** Found by the concurrency pass. The workers ran under `asyncio.gather`, which does not cancel the others when one raises. A crash in one worker marked the job failed while the rest kept fetching and writing rows. They now run in a `TaskGroup`. A test makes the second page raise and checks that the site sees nothing more. The job's error names the first exception, not `ExceptionGroup`.
- **Any `TimeoutError` looked like the time budget.** The crawl caught `TimeoutError` around its whole run. It now asks the timeout whether it expired and re-raises otherwise.
- **A shorter path did not help a pending URL.** Found by the correctness pass. A URL first found deep in the site kept its depth when a link from nearer the seed found it again, so `max_depth` could stop it from being expanded. A pending row now takes the higher score, the smaller depth and the shorter pattern run. Test added.
- **Glob patterns had no length limit, and a seed could pass validation and then kill its job.** Found by the security pass. Patterns are capped at 500 characters and 50 per list. Seeds are canonicalised in the tool, so a URL the canonicaliser refuses is an `invalid_url` error at the call, not a failed job.
- **`map_site` judged `allowed` by the wrong file.** It used the start origin's robots.txt for every URL on the site, including the `www.` sibling and the other scheme, which have files of their own. `allowed` now comes from the URL's own origin, and is `null` when that file has not been read.
- **Over-engineering review.** Cut a per-pattern URL counter that the pattern-run design made unnecessary, a connection counter on the proxy that nothing read, and a `Frontier.patterns` method written for it. Kept: `CrawlDeps` (it is what a resumed job and the server share), the injected clock on `PageCache` (it replaces sleeping tests), `cached_robots_rules` next to `robots_rules` (one never touches the network), and `__len__` on the limiter and breaker (two lines, and the eviction tests need them).

Accepted and recorded, not fixed:

- Relevance statistics restart when a job resumes.
- The near-duplicate scan is linear in the pages kept.
- The pattern-run cap ends long pagination after ten pages in a row.
- Jobs, frontier rows and cache rows are never deleted. Queued jobs are unbounded.
- The page cache is shared by all callers and ignores `Cache-Control: no-store`.
- Two servers on one data directory would both resume the same jobs.
- Sitemaps with namespace prefixes are not read.

The jury was again one reviewer playing four roles. The crawl's resume path and the per-render proxy are the parts most worth an outside read.

### Commit message draft

```
Phase 4: goal-directed crawling, jobs, resume and the page cache

Add a SQLite frontier taken best score first, with every step committed on
its own so a killed crawl resumes where it stopped. Score links by anchor
text, URL words and the parent page's BM25 relevance to the goal, and fall
back to breadth-first with no goal. Canonicalise URLs before they are
queued or cached.

Skip exact duplicates by text hash, near duplicates by a 64-bit simhash,
canonical aliases and redirect targets. Bound crawler traps by depth, page
budget, a cap on links in a row that share their parent's URL pattern, and
a refusal of repeated path segments and very long URLs.

Add crawl_site, get_job, cancel_job and list_jobs over MCP, with progress
notifications, per-job metrics, a limit on running jobs, a time budget that
spans restarts, and resume at startup. Add map_site, which reads sitemaps
from robots.txt Sitemap lines or /sitemap.xml, follows indexes and gzip
files, and adds the start page's links.

Add a page cache in SQLite with a TTL and ETag / Last-Modified
revalidation, used by fetch_page and by crawls. Return error pages as
results whatever their content type. Migrate the schema to version 2.

Close gaps from Phases 2 and 3: evict per-host limiter and breaker state
with an LRU that keeps busy hosts, give crawl fetches their own time
budget, skip the render tests in test_page.py without Chromium, pin
Playwright to 1.65, and give each render its own guarded proxy with a byte
budget and connection spacing for sub-resources.

Extend the fixture site with a crawlable site, two infinite link traps, a
near-duplicate pair, a canonical alias and sitemaps, and test a real kill
and resume. Docs updated.
```

## Phase 5: retrieval (2026-11-21)

### Done

Passage ranking, `search_in_site` and `extract`, with embedding rerank and model extraction as optional extras behind config flags. Also the Phase 4 gap: an end-to-end test that fails when anchor text stops steering the crawl. New modules: `rank/passages.py`, `rank/rerank.py`, `extract/schema.py`, `extract/llm.py`, `providers.py` and `retrieval.py`. Schema version 3, ten settings, two MCP tools, and 601 tests. Three dependencies were added (`lxml`, `cssselect`, `jsonschema`). Nothing was committed, tagged or pushed.

The task was not in the PRD. It was given as: passage splitting and BM25 reusing `rank/`, `search_in_site` returning ranked passages with source URL and snippet, `extract(url_or_job, schema)` validating against a user JSON Schema with CSS and XPath rules first, optional embedding rerank and model extraction off by default behind a provider interface, no network in tests.

### Verification

- `pytest`: 601 passed in 250 seconds, no warnings. The suite was 461 tests at the end of Phase 4. The Phase 5 tests need no browser except one in `test_page.py`, which launched a real headless Chromium and left nothing behind.
- `ruff check .` and `ruff format --check .`: clean
- `mypy` (strict, `src/weft`): clean
- `uv build`: sdist and wheel built. The wheel holds `retrieval.py`, `providers.py`, `rank/passages.py`, `rank/rerank.py`, `extract/schema.py` and `extract/llm.py`, and its metadata requires `lxml`, `cssselect` and `jsonschema`.
- Real stdio. `python -m weft` started as a child process and driven by a client script against the fixture site. Eight tools listed. `search_in_site` for "pricing team plan seat" crawled 8 pages, searched 14 passages and returned the pricing page's team-plan passage first (score 6.0), then its annual billing passage, then the enterprise page. `extract` over a crawl of the job board returned both roles with their titles and salaries, checked three pages, matched two, and sent no request to the site (the pages came from the cache). A schema with `"type": "strng"` and one with the selector `div[` were both refused as `invalid_schema`, with the place and the problem.
- Mutation checks. 118 hand-written mutants of the Phase 5 code in five groups: the schema extractor (26), providers, rerank, the model fallback and settings (28), passages and the tokenizer (24), the HTML cache (11), and the retrieval code and server tools (29). Each is applied in a scratch copy of `src` and run against the tests meant to catch it, and the unmutated selection must pass first. The first run covered 117 and caught 85. The 32 survivors were read one by one: 31 were gaps in the tests and are closed (see Problems hit), and one is equivalent. A mutant for the trimming fix was added after. Final: 117 of 118 caught, and the survivor is the equivalent one.
- Phase 4 gap. `test_anchor_text_alone_decides_which_link_is_fetched_first` fails with the anchor weight at zero, with the scorer given an empty anchor and with the crawler passing none (3 of 3). The old `test_the_goal_is_crawled_first` catches none of them.
- No network. Providers are in-memory fakes, or `httpx.MockTransport`. The server tests give settings whose endpoint cannot resolve, so a call that reached for it would fail.
- Processes. After the last full run there is no `python`, `chrome` or `playwright` process. Two `node.exe` processes remain, which no test started (the count moved between one and four while I worked).
- Environment. `cssselect` was installed into `.venv` (`uv pip install cssselect`) while the code was written, since it was not a dependency before. It is in `pyproject.toml` now. There is no lockfile.
- Not verified: a real model or embedding endpoint (no key, and the tests must not touch the network), Linux and Docker (Phase 6), extraction over crawls of thousands of pages, and memory use under load (Phase 7).

### Decisions

- **The cache stores the HTML.** The Phase 4 notes said `extract` could read a job's pages from the cache. The cache held only Markdown, and CSS and XPath need the DOM, so each row now holds the page's HTML, compressed, and `get_document` returns it. It is the rendered DOM for a page that went through the browser, so rules see what a visitor sees. Rows are about ten times larger. The alternative was to fetch every page again for `extract`, which breaks the promise that a search costs few requests.
- **Rules live inside the JSON Schema.** `x-css`, `x-xpath` and `x-attr` are annotations, which JSON Schema ignores, so the user's schema is still an ordinary schema that any validator accepts. The other design was a second document of selectors next to the schema, which would have to be kept in step.
- **One lxml tree for CSS and XPath.** `cssselect` translates CSS to XPath, so both kinds run on the same tree and mix in one schema. Running CSS on selectolax and XPath on lxml would have parsed every page twice and made a schema that mixes them impossible.
- **`jsonschema` validates the rule output.** Rules build data and validation says whether it is acceptable, so one mechanism serves the rules, the model fallback and the error messages. Errors are cut to ten, each 300 characters, as `path: problem`.
- **Rules first, the model only when they fall short.** A model call costs money and sends page text off the machine. A page the rules satisfy never reaches it. A model answer replaces the rules' only if it validates against the same schema, so a wrong shape can never get through, and an invalid one leaves the rules' result with a note. One call asks about at most ten pages.
- **The flag is the switch.** A fake provider given to `build_server` is ignored when its flag is off, as a configured one would be. A test shows it, because otherwise a feature could run while its setting said off.
- **A provider failure means carry on.** Rerank falls back to the keyword order with `reranked: false`. Model extraction leaves the rules' result with an `llm:` error. A provider error never fails the tool, and never holds a key or a response body.
- **Rerank by rank fusion.** Reciprocal rank fusion needs no common scale for the two scores, no weight and no numpy (`math.sumprod`). It reorders the best 50 keyword hits and cannot add recall. Adding recall would mean embedding every passage of every page.
- **HTTP only, no local model.** One client speaks the chat-completions and embeddings API that hosted and local servers offer, so a local server needs only a base URL. A `sentence-transformers` provider would add a dependency of gigabytes for one flag. The Protocol leaves room for it.
- **Vendor and model names stay out of code and docs.** The README describes the API by what it is. The model names in the config sample are placeholders.
- **The endpoints are operator-set and not behind the address guard.** An operator who points Weft at a server on their own network means it. No tool argument can name an endpoint, which is what keeps the guard out of the picture. If that changes, the guard comes first.
- **`search_in_site` is a crawl job.** It reuses the job machinery, progress, politeness and resume, at the price of one job per call, which is never deleted. It reads the pages back through the cache with the job's own render mode, so the read costs no request.
- **Extraction over a job returns only the pages that match.** On a site of mixed pages most are not the kind asked for, and hundreds of invalid items would bury the few good ones. The counters (`pages_checked`, `pages_matched`, `pages_unreadable`) say how far it got. It reads most relevant first and stops at `limit` matches.
- **Passages carry their heading, and the page title counts as words.** A passage that says "twelve dollars per seat" under the heading "Team plan" is a hit for "team plan pricing", though none of those words is in the sentence.
- **A tokenizer for words, not for ASCII.** `[^\W_]+` keeps `für` and `цена` whole. The ASCII-only one split them into single letters and dropped them, which would have made every German, French or Russian page unsearchable. It also changes the crawl's scoring for such pages. CJK and Thai still come out as one long token.
- **What a run may collect is bounded by an existing limit.** `max_content_chars`, the cap on what one page may return, is also the cap on the text a rule run gathers.
- **A page with an error status is run through the selectors like any other.** The fetch tools return error pages as results, so `extract` does too. The cost is a wasted model call on a 404 when the model is on. A one-line status check would remove it. It was not added because nothing asked for it.

### Problems hit

- **The first mutation run caught 85 of 117.** The tests for the new modules passed on their first run, written before the code and seen red only for a missing module. That showed they ran. It did not show that each assertion could fail. The 32 survivors were read one by one. Most were boundary tests that stopped one character short or never reached the branch: the 7-hash heading, a selector of exactly the longest length, a negative number, a `src` attribute, the order of validation errors, a model reply nested too deep for `json`, an upper-case `HTTPS` in an endpoint, and a snippet that is cut or not. Some tests passed for the wrong reason. The zero-vector rerank test gave the same order with or without the guard. The redirect test supplied its own HTTP client, so it never touched the one the code builds. Two tests of key handling never read the key. A snippet test passed with the lead-in set to zero. Two mutants changed a limit that the test imported (the node count and the schema size), so the test moved with them. Each now has a test that fails on its mutant, and the two limit tests use the published numbers. One survivor is equivalent: it makes `search_in_site` read pages in the `auto` render mode whatever the job says, and `search_in_site` has no `render` argument, so the job always says `auto`.
- **A test counted hits on a session-wide fixture.** The "refused before any request" tests asserted `site.total_hits == 0`, but the fixture site is shared by the whole session, so the count included earlier tests. They compare with a count taken at the start now.
- **A cap test crawled too little.** It meant to show that a job with more than ten pages asks the model about ten. At depth 1 the fixture site yields seven pages, so the test checked nothing. It crawls deeper now.
- **A scripted edit broke a test module.** A Python heredoc turned the `\n` in a test string into a real newline. `ruff check` caught the unterminated string at once, and the edit was redone with raw strings. A large shell heredoc with apostrophes had failed the same way before, so file content goes through the editor tools.
- **A selector could build an answer far bigger than the page.** Found in review, not by a test. See Review.
- **A provider that drips bytes would never time out.** Found in review. See Review.
- **`tests/test_server.py` could not be collected for most of the build.** It was written first and imported a module that did not exist yet, so the whole file, the Phase 1 to 4 tests included, errored at collection until `retrieval.py` and the server wiring landed. That was the intended red state, and the reason none of it could be run in between.

### Review

I reviewed the code in this session from four angles (correctness, security, concurrency, maintainability) and reconciled the findings, then ran an over-engineering review.

Fixed:

- **Unbounded output from a selector.** Found by the security pass. `div` on a page of nested divs matches the same text at every level, so a few kilobytes of hostile HTML could produce a response hundreds of times larger. The run now counts characters as it collects them and stops at `max_content_chars`. A test shows 50 nested divs and the exact boundary.
- **No wall-clock limit on a provider call.** Found by the concurrency pass. The HTTP client's timeouts apply per read, so a server that sends a byte at a time would hold an `extract` call as long as it liked. Fixed, with a test using a transport that never answers.
- **`url_or_job` was not trimmed, and an upper-case scheme was not a URL.** Found by the correctness pass and by a surviving mutant. `HTTPS://example.org` and a URL with a leading space were taken for job ids and answered `unknown_job`. The value is trimmed and the scheme compared in lower case. A test covers both.
- **An unused property on `AppState`.** Found by the over-engineering pass. Deleted.
- **Over-engineering review.** Kept: the `_Extraction` class (it holds the call's model budget, which a bare function would pass around as a parameter), `Document` (a pair, but the two always travel together), the two Protocols (tests inject fakes through them), and `get_page` next to `get_document` (many callers). Declined with a reason: a per-page token budget for the model (the character limit does the job), a `render` argument on `search_in_site` (`crawl_site` then `extract` covers it), concurrent model calls (the cap of ten makes sequential acceptable), and a status check that skips error pages (see Decisions).

Accepted and recorded, not fixed:

- No local embedding model. Rerank cannot add recall.
- English-only stemming and stop words. CJK and Thai are not segmented. No accent folding.
- A passage is a repeat only if the whole passage repeats.
- Every `search_in_site` call starts a job, and jobs and cache rows are never deleted. The `pages` table is about ten times larger.
- Ranking happens in one pass under `extract_timeout_s`.
- An agent's `pattern` or XPath can hold one of the four extraction threads.
- A model can be made to copy misleading page text into a free-text field. Validation is the only bound.
- The model calls of one `extract` call run in sequence.
- Phase 4 carry-overs unchanged.

The jury was again one reviewer playing four roles. The parts most worth an outside read are the schema extractor (`extract/schema.py`, which runs the agent's selectors over a stranger's HTML) and the provider client's handling of a hostile or slow endpoint.

### Commit message draft

```
Phase 5: passage search, schema extraction and optional model providers

Add search_in_site, which crawls toward a query as a job, reads the pages
back through the cache and returns the best passages with the URL, title,
heading and a snippet. Passages are cut at headings and sentence ends and
ranked with the BM25 the crawl already uses, with the title and heading
counted as words of the passage. The tokenizer now reads words in any
script that separates them with spaces.

Add extract, which returns JSON that validates against a user JSON Schema,
read from a URL or from the pages of a job. Selectors are written into the
schema as x-css, x-xpath and x-attr, run on one lxml tree so CSS and XPath
mix, and the result is validated with jsonschema. Schema size, nesting and
selector length are bounded, a reference may not leave the schema, and a
run stops at max_content_chars of collected text.

Add a provider interface with one HTTP client for embeddings and chat. Both
features are off by default behind config flags, with the endpoint set by
the operator only, and never called in tests. Embedding rerank fuses the
keyword and similarity ranks of the best 50 hits and falls back to the
keyword order on any provider error. Model extraction runs only when the
rules fall short, for at most ten pages per call, and its answer is used
only if it validates.

Store each cached page's HTML, compressed, so extraction rules can read
the rendered DOM. Migrate the schema to version 3, which drops cached pages.

Close the Phase 4 gap: an end-to-end crawl test that fails when anchor text
stops steering the order. Add cssselect, lxml and jsonschema as
dependencies. Docs updated.
```
