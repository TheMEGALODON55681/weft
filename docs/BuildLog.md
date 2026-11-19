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
