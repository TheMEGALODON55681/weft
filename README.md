# Weft

Weft is a web crawler that AI agents can call. It runs as an MCP server, fetches pages, and returns clean Markdown with the title, metadata, links and the URL each result came from, so the agent can cite its sources.

It also crawls. Give it seed URLs and a goal in plain language ("find pricing pages"), and it follows the links most likely to lead there first, skips duplicates and crawler traps, and ranks what it found against the goal. Crawls run as background jobs that survive a restart. `map_site` lists a site's URLs from its sitemaps without crawling.

This repository is at Phase 4 of 7. Six tools run over stdio: `fetch_page`, `crawl_site`, `get_job`, `cancel_job`, `list_jobs` and `map_site`. Passage search and schema extraction come next. [docs/Phases.md](docs/Phases.md) lists what each phase adds.

## Install

Weft needs Python 3.12 or newer.

```bash
python -m venv .venv
.venv/bin/pip install -e .          # Windows: .venv\Scripts\pip install -e .
```

For development, install the extras: `pip install -e ".[dev]"`.

Rendering JavaScript pages needs a headless Chromium, which is a separate download of about 290 MB on disk:

```bash
.venv/bin/playwright install chromium-headless-shell   # Windows: .venv\Scripts\playwright.exe
```

Without it Weft still works. `render="auto"` returns the page as fetched, and `render="always"` fails with `render_unavailable`.

## Run

```bash
.venv/bin/weft                      # Windows: .venv\Scripts\weft.exe
```

`python -m weft` does the same. The server speaks MCP over stdio, so it prints nothing and waits for a client. It accepts two flags: `--config PATH` for a TOML settings file and `--version`.

stdout carries the protocol and nothing else. Logs are JSON lines on stderr.

## Connect an MCP client

Add Weft to any client that launches stdio servers. Use the absolute path to the executable, because clients start servers from an arbitrary working directory:

```json
{
  "mcpServers": {
    "weft": {
      "command": "/absolute/path/to/weft/.venv/bin/weft",
      "env": { "WEFT_CONTACT": "you@example.org" }
    }
  }
}
```

On Windows the command is `C:\\path\\to\\weft\\.venv\\Scripts\\weft.exe`. Set `WEFT_CONTACT` to an address or URL where site operators can reach you. Weft puts it in the User-Agent header.

## Tools

### `fetch_page(url, render="auto")`

Fetches one page and returns its main content as Markdown.

| Argument | Meaning |
| --- | --- |
| `url` | Absolute `http` or `https` URL. Credentials in the URL are rejected. |
| `render` | `"auto"` (the default) fetches over plain HTTP and renders in a headless browser only when the page needs it: it has a script, and either its text is under `render_min_words` words or a `<noscript>` asks for JavaScript. `"never"` skips the browser. `"always"` renders every HTML page. |

The result is structured JSON:

| Field | Meaning |
| --- | --- |
| `url` | The URL you asked for. |
| `final_url` | The URL after redirects, including any a script made while rendering. Cite this one. |
| `status` | HTTP status code. Error statuses such as 404 come back as results. |
| `fetch_method` | `"http"`, or `"browser"` when the content came from a rendered page. |
| `content_type` | Lowercased media type of the response. |
| `title` | Page title, or `null`. |
| `markdown` | Main content with navigation and footers removed. Empty for pages with no extractable text. |
| `metadata` | `description`, `author`, `published`, `language`, `site_name`, `canonical_url`. Each can be `null`. |
| `links` | Outgoing links as `{url, text, nofollow}`: absolute, no fragments, `http` and `https` schemes, one entry per URL. |
| `links_total` | Link count before the `max_links` cap. |
| `body_truncated` | `true` when the response or the rendered page passed `max_body_bytes`, or the render ran out of `render_max_bytes`. |
| `content_truncated` | `true` when the Markdown passed `max_content_chars`. |
| `redirects` | URLs requested before `final_url`. |
| `elapsed_ms` | Time for the whole call: any wait for the host's turn, the fetch, extraction and rendering. |
| `body_bytes` | Bytes received for the document. `0` when the page came from the cache. |
| `cached` | `true` when the page came from the cache, either fresh or confirmed unchanged by the site. |

Error statuses come back as results whatever their content type. A `404` that answers with plain text has `status: 404` and empty `markdown`.

When a fetch yields nothing usable, the tool returns an error whose message starts with a stable code:

| Code | Cause | Worth retrying? |
| --- | --- | --- |
| `invalid_url` | Empty, over 8192 characters, not `http(s)`, no host, or credentials in the URL. A redirect to such a URL raises it too. | No |
| `blocked_address` | The host name, or a redirect target, resolves to an address Weft does not fetch. See [Safety and politeness](#safety-and-politeness). | No |
| `robots_disallowed` | The site's robots.txt does not allow Weft to fetch this path. | No |
| `robots_unavailable` | robots.txt answered with a server error or `429`, so Weft treats the site as off limits for a minute. | Later |
| `rate_limited` | The host's turn would come after more than `total_timeout_s`. The message says how long to wait. | After the wait |
| `circuit_open` | The host has failed several times in a row. The message says how long Weft will leave it alone. | After the wait |
| `redirect_loop` | A redirect leads back to a URL already visited. | No |
| `too_many_redirects` | More than `max_redirects` hops. | No |
| `timeout` | The fetch passed `read_timeout_s`, `connect_timeout_s` or `total_timeout_s`, or rendering passed `render_timeout_s`. | Maybe |
| `network_error` | The name does not exist, or DNS, connection, TLS or protocol failed. | Maybe |
| `unsupported_content_type` | The response is not HTML or XHTML. | No |
| `extraction_timeout` | Reading the page took longer than `extract_timeout_s`. | No |
| `render_unavailable` | `render="always"`, and headless Chromium is not installed or would not start. | No |
| `render_failed` | `render="always"`, and the page crashed the browser tab, for example by passing `browser_js_heap_mb`. | No |
| `unknown_job` | `get_job` or `cancel_job` got an id that no job has. | No |

### `crawl_site(seed_urls, goal="", max_pages=50, max_depth=3, include_patterns=[], exclude_patterns=[], same_domain_only=true, render="auto", wait_s=20)`

Starts a crawl as a background job and returns the job, its metrics so far and its 20 most relevant pages.

| Argument | Meaning |
| --- | --- |
| `seed_urls` | One to twenty absolute `http(s)` URLs. Seeds are always fetched, whatever the patterns say. |
| `goal` | What you are looking for, in plain words. Empty means breadth-first. |
| `max_pages` | Pages to fetch, 1 to 5000. Failures and duplicates count. |
| `max_depth` | Link hops from a seed, 0 to 20. `0` fetches the seeds only. |
| `include_patterns` | Glob patterns matched against the full URL, such as `*/docs/*`. When given, a discovered link must match one of them. |
| `exclude_patterns` | Glob patterns for links to skip. |
| `same_domain_only` | Stay on the seeds' hosts. A leading `www.` does not count, so `www.example.com` and `example.com` are one site. Other subdomains are separate. |
| `render` | The render policy for each page, as in `fetch_page`. |
| `wait_s` | How long the call waits for the crawl before it returns, 0 to 300 seconds. While it waits it sends MCP progress notifications. The job keeps running after the call returns. |

**How it chooses what to fetch.** The crawl is best-first. Each link gets a score before it is fetched: how many goal words appear in its anchor text, how many appear in its URL, and how relevant the page that linked to it was. A fetched page is scored with BM25 against the goal, and the result is squashed to a relevance between 0 and 1. Results are ranked by that relevance. Common words that describe the task rather than the topic ("find", "every", "page") are dropped from the goal, and plurals and `-ing` forms are folded, so "pricing" matches "price".

**What it skips.**

- Links marked `rel="nofollow"`, links robots.txt disallows (once Weft has read that site's file), and links outside the seeds' sites when `same_domain_only` is on.
- Duplicates. URLs are canonicalised first: case, default ports, dot segments, percent-encoding, query order and tracking parameters such as `utm_*` and `gclid` are normalised. A page whose text matches one already kept is a duplicate. So is a near match, judged by a 64-bit simhash, and so is a page whose `<link rel="canonical">` names another page on the site. A redirect target counts as the page that redirected to it. Duplicates are recorded but not expanded.
- Traps. Calendars, endless archives and pagination look the same from link to link: the URL keeps its shape and only the numbers change. At most ten links in a row may share their parent's shape. URLs over 2048 characters, and paths that repeat a segment more than twice (`/a/b/a/b/a/b`, the mark of a relative-link loop), are not queued. `max_depth` and `max_pages` bound everything else.

**Limits on a job.** A job stops after `crawl_timeout_s` of running time, summed across restarts. Each fetch inside a crawl gets `crawl_fetch_timeout_s` instead of `total_timeout_s`, so a site with a long `Crawl-delay` is waited for, not refused. At most `crawl_max_jobs` jobs run at once and the rest wait their turn. Each job runs `crawl_concurrency` fetches at a time, under the same per-host pacing as everything else.

**Restarts.** Every step of a crawl is committed to SQLite as it happens. If the server stops, or is killed, its running jobs carry on when it next starts. Pages already fetched are not fetched again, except the few that were in flight at the moment it stopped.

### `get_job(job_id, limit=50, offset=0)`

Returns the job and its pages, most relevant first.

| Field | Meaning |
| --- | --- |
| `job.status` | `queued`, `running`, `done`, `failed` or `cancelled`. |
| `job.stop_reason` | Once done: `exhausted` (nothing left to fetch), `max_pages` or `time_budget`. |
| `job.error` | Why a `failed` job failed. |
| `job.params` | The arguments the job was started with. |
| `job.stats` | `pages_fetched`, `pages_ok`, `duplicates`, `failed`, `pending`, `cached`, `bytes`, `errors` (failures counted by code, with `http_404` and the like for error statuses), `latency_ms_p50`, `latency_ms_p95`, `latency_ms_max` and `run_s`. |
| `pages` | `{url, final_url, title, status, depth, relevance, snippet, cached}` for each page kept. Duplicates and failures are counted in `stats` but not listed. |
| `pages_total` | Pages kept, before `limit` and `offset`. |

A snippet is the first 280 characters of the page's Markdown, with whitespace collapsed. `fetch_page` returns the full text, and for a page the crawl just fetched it comes from the cache, provided both use the same `render` mode.

### `cancel_job(job_id)`

Stops a queued or running job and returns it. Its pages stay readable. A cancelled job is never resumed. Cancelling a finished job changes nothing.

### `list_jobs(status=null, limit=20)`

Returns `{jobs: [...]}`, newest first, optionally only those with one status.

### `map_site(url, max_urls=500)`

Lists a site's URLs without crawling it. Weft reads the `Sitemap:` lines in the site's robots.txt, or `/sitemap.xml` if there are none, follows sitemap indexes and unpacks gzip files, reading at most 20 files. It adds the links on the page you gave it.

| Field | Meaning |
| --- | --- |
| `url` | The start page, after redirects. |
| `sitemaps` | Each file read: `{url, status, urls, error}`. `error` is a code from the table above, or `not_a_sitemap`. |
| `urls` | `{url, source, lastmod, allowed}`: canonical, on the same site as the start page, the start page and its links first. `source` is `sitemap` or `link`. `allowed` says whether robots.txt lets Weft fetch the URL, and is `null` for an origin whose robots.txt Weft has not read. |
| `truncated` | `true` when `max_urls` was reached or sitemap files were left unread. |

Only a failure to fetch the start page is an error. Sitemap failures are reported per file.

## The page cache

Pages are stored in the SQLite file after each successful fetch, by canonical URL and render mode. For `cache_ttl_s` (an hour by default) the stored page is served without contacting the site. After that, if the site sent an `ETag` or `Last-Modified`, Weft asks with `If-None-Match` or `If-Modified-Since`, and a `304` reuses the stored page, a rendered one included. Otherwise the page is fetched again. Only `200` responses are stored. `cache_ttl_s = 0` turns the cache off.

## Safety and politeness

Weft fetches whatever URL an agent gives it, and the agent may have picked that URL up from a hostile page. The address check covers every connection Weft opens: the first fetch, each redirect hop, each retry and each robots.txt read. robots.txt, pacing and the circuit breaker apply to each redirect hop too.

**Addresses.** Weft resolves the host name itself, checks every address that comes back, and connects to one it checked. It refuses private, loopback, link-local, carrier-grade NAT, multicast and reserved ranges, the cloud metadata addresses (AWS, Google Cloud, Azure, Alibaba, Oracle), and IPv6 forms that wrap those. If any address in an answer is refused, the whole name is refused, and the error never says where the name pointed. Weft judges what a name resolves to, so odd spellings of an address, such as `2130706433` or `[::ffff:7f00:1]`, cannot slip past the check. The check and the connection share one lookup, so a DNS answer that changes between them cannot get a request through.

**robots.txt.** Weft obeys it, using the name `weft`, and has no switch to turn that off. The rules follow RFC 9309: the longest matching rule wins, `Allow` wins a tie, `*` and `$` work in paths, and a group that names `weft` beats the `*` group. A missing or forbidden file (any 4xx except `429`) means no restrictions. A server error or `429` means off limits for the next minute. Weft reads at most the first 500 KiB, caches the result for an hour per origin, and honours `Crawl-delay`. The `/robots.txt` URL itself can always be fetched.

**Pacing.** By default Weft starts at most one request per second to a host, keeps at most two in flight to it, and at most sixteen overall. A longer `Crawl-delay` wins. Redirect hops and retries count as requests. If a call's turn would come after more than `total_timeout_s`, it fails at once with `rate_limited` and says how long to wait, instead of blocking the agent. Reading robots.txt is not spaced, so the first page on a new host does not wait for it.

**Retries.** Weft retries connection failures, timeouts, dropped connections and `429`, `502`, `503` and `504` responses, twice by default, with exponential backoff and jitter. It honours `Retry-After` when the wait fits inside `total_timeout_s`. When retries run out, a transport failure becomes an error and an overloaded response comes back as a normal result with its status. A host name that does not exist is not retried.

**Circuit breaker.** After five failures in a row, Weft stops contacting that host for 30 seconds, then lets one request through to test it. Failures are transport errors and the four statuses above. A plain `404` or `500` is not a failure, so one broken page does not close a site. Requests already waiting for the host are stopped too.

**Proxies.** Weft ignores `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY`. A proxy looks names up for itself, which would put it outside the address check. This build cannot be used behind a mandatory proxy.

**Browser.** Chromium never reaches the network on its own. Weft fetches the page over plain HTTP first, with every check above, and hands that body to the browser. Weft also fetches every other navigation the page makes, such as a frame, a script redirect or a popup, one redirect hop at a time, so robots.txt, pacing, the breaker and the address check apply to it too. Everything else the page loads (scripts, images, XHR, fetches, WebSockets, workers) goes through a proxy inside Weft that resolves each name and checks every address before it connects. robots.txt, host pacing and the breaker apply to navigations only, not to the scripts and images a page loads. Those get the address check, the byte budget and their own, faster spacing.

Each render gets a proxy of its own. It stops relaying once the page and everything it loads have used `render_max_bytes`, and refuses new connections after that. It also spaces new connections to each host by `render_host_interval_s`, so one page cannot open a burst of connections to a site.

Chromium cannot resolve names itself, cannot skip the proxy for loopback addresses, and cannot send WebRTC traffic around it. Service workers are blocked. Each render gets a fresh browser context, so no cookie or storage carries over to the next page. The browser sends the same honest User-Agent as plain fetches.

Page content is still untrusted. These controls limit where Weft can be pointed. They do not stop a fetched page from carrying text meant to steer the agent that reads it.

## Configuration

Settings come from, in order of precedence: `WEFT_*` environment variables, a TOML file, then the defaults below. Weft reads `weft.toml` in the working directory, or the file named by `--config` or `WEFT_CONFIG`. Unknown keys in the file are an error, so a typo fails at startup instead of being ignored.

```toml
contact = "you@example.org"
max_links = 100
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `data_dir` | `~/.weft` | Holds `weft.db`. |
| `contact` | empty | Added to the User-Agent as `weft/VERSION (contact: ...)`. |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `http2` | `true` | Offer HTTP/2 on HTTPS connections. |
| `connect_timeout_s` | `10` | Time allowed to open a connection. |
| `read_timeout_s` | `20` | Longest silence allowed while reading. |
| `total_timeout_s` | `30` | Ceiling for one fetch: waits for the host's turn, retries, redirects and the body. |
| `extract_timeout_s` | `10` | Longest a page may spend in decoding and extraction. |
| `max_body_bytes` | `5000000` | Response size cap, measured after decompression. |
| `max_redirects` | `10` | Redirect hops followed. |
| `max_links` | `200` | Links returned per page. |
| `max_content_chars` | `100000` | Markdown characters returned per page. |
| `allow_loopback` | `false` | Let Weft fetch `127.0.0.0/8` and `::1`. For local development only. With it on, an agent can reach anything listening on your machine. Every other refused range stays refused. |
| `host_min_interval_s` | `1` | Seconds between request starts to one host. `0` turns spacing off. |
| `host_concurrency` | `2` | Requests in flight to one host. |
| `global_concurrency` | `16` | Requests in flight in total. |
| `max_retries` | `2` | Retries after the first attempt. `0` turns retries off. |
| `retry_backoff_s` | `0.5` | Base delay before the first retry. It doubles each time, and each wait is between half and all of that. |
| `breaker_threshold` | `5` | Consecutive failures that open a host's circuit breaker. |
| `render_min_words` | `50` | Under this many words of extracted text, a page with a script counts as thin and `render="auto"` renders it. |
| `render_timeout_s` | `20` | Ceiling for one render, including the wait for a free page and starting the browser. It comes after `total_timeout_s`, which covers the plain fetch. |
| `browser_max_pages` | `2` | Pages rendering at once. Further calls wait their turn inside `render_timeout_s`. |
| `browser_recycle_after` | `50` | Pages one Chromium process renders before Weft replaces it, which caps slow memory growth. |
| `browser_js_heap_mb` | `256` | JavaScript heap limit per page. A page that passes it is killed and the call fails with `render_failed`. |
| `render_max_bytes` | `20000000` | Bytes one render may download for its scripts, images and other sub-resources. Past it, loading stops and `body_truncated` is `true`. |
| `render_host_interval_s` | `0.05` | Seconds between new sub-resource connections to one host during a render. `0` turns it off. |
| `cache_ttl_s` | `3600` | How long a cached page is served without asking the site. `0` turns the cache off. |
| `crawl_timeout_s` | `1800` | Running time a crawl job may use, summed across restarts. |
| `crawl_fetch_timeout_s` | `120` | Ceiling for one fetch inside a crawl, including the wait for the host's turn. It replaces `total_timeout_s` there. |
| `crawl_concurrency` | `4` | Fetches one job runs at a time. |
| `crawl_max_jobs` | `2` | Jobs running at once. Others are `queued` until a slot frees. |
| `max_tracked_hosts` | `4096` | Hosts whose pacing and breaker state is kept. The least recently used idle host is dropped first. A host that is mid-request, still owed spacing or has an open breaker is never dropped. |

Each setting has an environment variable named `WEFT_` plus the uppercase setting name, for example `WEFT_MAX_LINKS`.

## Limits

- Weft reads HTML and XHTML. PDFs, JSON and images return `unsupported_content_type`, and a crawl records them as failures under that code.
- The browser starts on the first page that needs it, which adds a second or two to that call. It runs headless, and Weft closes it when the server stops.
- Rendering limits are per page: `browser_js_heap_mb` caps a page's JavaScript, not its images or layout memory. Memory for one Chromium is roughly `browser_max_pages` pages plus the browser's own overhead.
- The browser's internal proxy listens on a loopback port. Another program on the same machine could use it, but only to reach addresses Weft would fetch anyway.
- Cookies are neither stored nor sent. Authenticated crawling will take cookies per job.
- Pacing is per host name and per process. `example.com` and `www.example.com` are separate hosts, and two Weft processes do not share a limit. Two servers on one data directory would also both resume the same jobs, so run one per directory.
- A page that hangs the extractor frees the caller after `extract_timeout_s`, but the worker thread cannot be stopped. Four stuck pages switch extraction off until they finish or the server restarts.
- Jobs, their frontier rows and cached pages are never deleted. Expired cache rows are kept for their validators. A long-lived data directory grows until you remove `weft.db`.
- Canonicalisation sorts query parameters and drops tracking parameters, and the crawl fetches the canonical form. A site that depends on parameter order would see a different request.
- Near-duplicate detection is tuned for pages of a few hundred words. Pages under 30 words are compared by exact text only.
- Relevance scores use corpus statistics from the pages a job has fetched. A resumed job starts those statistics again, so scores before and after a restart are not exactly comparable.
- The page cache is shared by every caller of one server. That is safe while Weft sends no cookies. Authenticated crawls, when they arrive, must keep their pages out of it.

## Development

```bash
pytest                  # about four minutes, includes a real stdio round trip and a killed crawl
ruff check .
ruff format --check .
mypy                    # strict, covers src/weft
```

Tests never touch the internet. They run against a small local web server in `tests/fixture_site.py`, so the suite runs with `allow_loopback` on and spacing off. The tests for the address guard and the limiter turn those back to their defaults. The browser tests start a real headless Chromium and need `chromium-headless-shell` installed. Without it they are skipped, in `tests/test_browser.py` and `tests/test_page.py` alike.

Playwright is pinned to one minor release in `pyproject.toml`, because each release drives its own Chromium build. After raising the pin, run `playwright install chromium-headless-shell` again.

## More

- [docs/Architecture.md](docs/Architecture.md): modules, data flow, schema
- [docs/Phases.md](docs/Phases.md): what is done and what comes next
- [docs/BuildLog.md](docs/BuildLog.md): decisions and problems, phase by phase
