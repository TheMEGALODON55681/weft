# Architecture

State as of Phase 1. Later phases add modules and tables; each phase updates this file.

## Layout

```
src/weft/
  config.py        typed settings: env, TOML file, defaults
  models.py        shapes returned to the agent (PageResult, Link, PageMetadata)
  db.py            SQLite connection, migrations, the single database thread
  obs/logging.py   JSON-lines logging to stderr, secret redaction, safe_url
  fetch/http.py    Fetcher: httpx client, manual redirects, size and time caps
  fetch/page.py    get_page: fetch, decode, extract, apply output caps
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
3. `Fetcher.fetch` validates the URL, then requests it. Redirects are followed in a loop that checks every hop: http(s) scheme, a host, no credentials, not a URL already visited, within `max_redirects`. The body streams through a byte cap that counts decompressed bytes. One `asyncio.timeout` bounds the whole call, redirects included.
4. `get_page` refuses non-HTML content types. An absent `Content-Type` counts as HTML.
5. Decoding and extraction run in a worker thread so the event loop stays free. `decode_body` tries a byte-order mark, the header charset, a `<meta>` charset, UTF-8, statistical detection and, as a last resort, UTF-8 with replacement characters. It never raises.
6. `extract_page` runs trafilatura for Markdown and metadata, and selectolax for links, `<title>`, `lang` and the canonical URL. A parser crash is logged and yields empty Markdown, not an exception.
7. `get_page` applies `max_content_chars` and `max_links`, records the totals, and returns a `PageResult`.
8. `server/app.py` converts any `FetchError` into an MCP tool error whose message starts with the error code. Anything else is logged with a traceback and reaches the client as a generic failure, with details masked.

## Error model

`FetchError(code, message)` is the one expected failure type. Codes are stable strings listed in the README. HTTP error statuses are not errors: a 404 page comes back as a normal result with `status: 404`.

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

Phase 4 will migrate these as the crawler needs. Expect a version 2.

## Configuration

`Settings` (pydantic-settings). Precedence: init arguments, `WEFT_*` environment variables, TOML file, defaults. The TOML path is `WEFT_CONFIG`, falling back to `./weft.toml`. The CLI's `--config` sets `WEFT_CONFIG`. Unknown keys are rejected. The CLI prints validation problems by field name and never echoes the offending value.

## Logging

One JSON object per line on stderr, from the `weft` logger: `ts`, `level`, `logger`, `msg`, plus any `extra` fields. stdout is the MCP protocol channel under stdio, so nothing may print to it.

Secrets are redacted by field name (token, secret, password, authorization, cookie, session, signature, api key), including inside nested dicts and lists. URLs go through `safe_url`, which drops credentials and fragments and masks sensitive query values. Redaction works on field names, so keep sensitive values out of log messages.

## Security posture

In place: URLs limited to http and https, no credentials in URLs, caps on time, size and redirect hops, no cookie persistence, error details masked from the client, secrets redacted from logs, and a server instruction telling the agent that page content is untrusted.

Not in place until Phase 2: SSRF protection, robots.txt, per-host rate limits, a global concurrency cap, retries and circuit breaking. The redirect loop in `Fetcher._fetch` is where the per-hop address check goes.

Known ceiling: a gzip bomb is cut at the byte cap, but the check runs per decoded chunk, so memory can spike to about 1000 times one network chunk before the cut.

## Tests

`tests/fixture_site.py` runs a local HTTP server on a random port. It serves a sample article, redirect chains and loops, an endless redirect, oversized and gzip-bomb bodies, a slow response, a response that drips bytes, Latin-1 and meta-declared Windows-1251 pages, non-HTML types, error statuses and a cookie echo. Later phases add robots.txt, a JS-rendered page, a near-duplicate pair and an infinite link trap.

`tests/test_server.py` includes a real stdio round trip: it spawns `python -m weft` and talks to it over pipes.
