# Weft

Weft is a web crawler that AI agents can call. It runs as an MCP server, fetches pages, and returns clean Markdown with the title, metadata, links and the URL each result came from, so the agent can cite its sources.

The finished tool will crawl outward from a seed URL toward a goal you describe in plain language ("find pricing pages"), rank what it finds, and return structured, cited results. This repository is at Phase 2 of 7: one tool, `fetch_page`, over stdio, with the safety and politeness controls a crawler needs before it touches the open web. [docs/Phases.md](docs/Phases.md) lists what each phase adds.

## Install

Weft needs Python 3.12 or newer.

```bash
python -m venv .venv
.venv/bin/pip install -e .          # Windows: .venv\Scripts\pip install -e .
```

For development, install the extras: `pip install -e ".[dev]"`.

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
| `render` | `"never"` and `"auto"` use plain HTTP. `"always"` asks for a headless browser, which arrives in Phase 3 and returns `render_unavailable` for now. |

The result is structured JSON:

| Field | Meaning |
| --- | --- |
| `url` | The URL you asked for. |
| `final_url` | The URL after redirects. Cite this one. |
| `status` | HTTP status code. Error statuses such as 404 come back as results. |
| `fetch_method` | `"http"` today. `"browser"` from Phase 3. |
| `content_type` | Lowercased media type of the response. |
| `title` | Page title, or `null`. |
| `markdown` | Main content with navigation and footers removed. Empty for pages with no extractable text. |
| `metadata` | `description`, `author`, `published`, `language`, `site_name`, `canonical_url`. Each can be `null`. |
| `links` | Outgoing links as `{url, text, nofollow}`: absolute, no fragments, `http` and `https` schemes, one entry per URL. |
| `links_total` | Link count before the `max_links` cap. |
| `body_truncated` | `true` when the response passed `max_body_bytes`. |
| `content_truncated` | `true` when the Markdown passed `max_content_chars`. |
| `redirects` | URLs requested before `final_url`. |
| `elapsed_ms` | Time spent fetching, including any wait for the host's turn. |

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
| `timeout` | The fetch passed `read_timeout_s`, `connect_timeout_s` or `total_timeout_s`. | Maybe |
| `network_error` | The name does not exist, or DNS, connection, TLS or protocol failed. | Maybe |
| `unsupported_content_type` | The response is not HTML or XHTML. | No |
| `extraction_timeout` | Reading the page took longer than `extract_timeout_s`. | No |
| `render_unavailable` | `render="always"` in a build without a browser. | No |

## Safety and politeness

Weft fetches whatever URL an agent gives it, and the agent may have picked that URL up from a hostile page. The address check covers every connection Weft opens: the first fetch, each redirect hop, each retry and each robots.txt read. robots.txt, pacing and the circuit breaker apply to each redirect hop too.

**Addresses.** Weft resolves the host name itself, checks every address that comes back, and connects to one it checked. It refuses private, loopback, link-local, carrier-grade NAT, multicast and reserved ranges, the cloud metadata addresses (AWS, Google Cloud, Azure, Alibaba, Oracle), and IPv6 forms that wrap those. If any address in an answer is refused, the whole name is refused, and the error never says where the name pointed. Weft judges what a name resolves to, so odd spellings of an address, such as `2130706433` or `[::ffff:7f00:1]`, cannot slip past the check. The check and the connection share one lookup, so a DNS answer that changes between them cannot get a request through.

**robots.txt.** Weft obeys it, using the name `weft`, and has no switch to turn that off. The rules follow RFC 9309: the longest matching rule wins, `Allow` wins a tie, `*` and `$` work in paths, and a group that names `weft` beats the `*` group. A missing or forbidden file (any 4xx except `429`) means no restrictions. A server error or `429` means off limits for the next minute. Weft reads at most the first 500 KiB, caches the result for an hour per origin, and honours `Crawl-delay`. The `/robots.txt` URL itself can always be fetched.

**Pacing.** By default Weft starts at most one request per second to a host, keeps at most two in flight to it, and at most sixteen overall. A longer `Crawl-delay` wins. Redirect hops and retries count as requests. If a call's turn would come after more than `total_timeout_s`, it fails at once with `rate_limited` and says how long to wait, instead of blocking the agent. Reading robots.txt is not spaced, so the first page on a new host does not wait for it.

**Retries.** Weft retries connection failures, timeouts, dropped connections and `429`, `502`, `503` and `504` responses, twice by default, with exponential backoff and jitter. It honours `Retry-After` when the wait fits inside `total_timeout_s`. When retries run out, a transport failure becomes an error and an overloaded response comes back as a normal result with its status. A host name that does not exist is not retried.

**Circuit breaker.** After five failures in a row, Weft stops contacting that host for 30 seconds, then lets one request through to test it. Failures are transport errors and the four statuses above. A plain `404` or `500` is not a failure, so one broken page does not close a site. Requests already waiting for the host are stopped too.

**Proxies.** Weft ignores `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY`. A proxy looks names up for itself, which would put it outside the address check. This build cannot be used behind a mandatory proxy.

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

Each setting has an environment variable named `WEFT_` plus the uppercase setting name, for example `WEFT_MAX_LINKS`.

## Limits

- Weft reads HTML and XHTML. PDFs, JSON and images return `unsupported_content_type`.
- No JavaScript execution until Phase 3. A page that builds its content in the browser comes back thin.
- Cookies are neither stored nor sent. Authenticated crawling will take cookies per job.
- Pacing is per host name and per process. `example.com` and `www.example.com` are separate hosts, and two Weft processes do not share a limit.
- A page that hangs the extractor frees the caller after `extract_timeout_s`, but the worker thread cannot be stopped. Four stuck pages switch extraction off until they finish or the server restarts.
- Weft stores nothing from a fetch yet. The SQLite file is created and migrated at startup, and later phases fill it.

## Development

```bash
pytest                  # about 100 seconds, includes one real stdio round trip
ruff check .
ruff format --check .
mypy                    # strict, covers src/weft
```

Tests never touch the internet. They run against a small local web server in `tests/fixture_site.py`, so the suite runs with `allow_loopback` on and spacing off. The tests for the address guard and the limiter turn those back to their defaults.

## More

- [docs/Architecture.md](docs/Architecture.md): modules, data flow, schema
- [docs/Phases.md](docs/Phases.md): what is done and what comes next
- [docs/BuildLog.md](docs/BuildLog.md): decisions and problems, phase by phase
