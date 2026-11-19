# Weft

Weft is a web crawler that AI agents can call. It runs as an MCP server, fetches pages, and returns clean Markdown with the title, metadata, links and the URL each result came from, so the agent can cite its sources.

The finished tool will crawl outward from a seed URL toward a goal you describe in plain language ("find pricing pages"), rank what it finds, and return structured, cited results. This repository is at Phase 1 of 7: one tool, `fetch_page`, over stdio. [docs/Phases.md](docs/Phases.md) lists what each phase adds.

## Before you attach it

This build has no SSRF guard, no robots.txt handling and no rate limiting. Those arrive in Phase 2. Until then an attached agent can ask Weft to fetch any URL your machine can reach, including `localhost` services and cloud metadata addresses, and a fetched page can contain text that tells the agent to do so. Attach it to agents you control, on your own machine.

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
| `elapsed_ms` | Time spent fetching. |

When a fetch yields nothing usable, the tool returns an error whose message starts with a stable code:

| Code | Cause |
| --- | --- |
| `invalid_url` | Empty, over 8192 characters, not `http(s)`, no host, or credentials in the URL. A redirect to such a URL raises it too. |
| `redirect_loop` | A redirect leads back to a URL already visited. |
| `too_many_redirects` | More than `max_redirects` hops. |
| `timeout` | The fetch passed `read_timeout_s`, `connect_timeout_s` or `total_timeout_s`. |
| `network_error` | DNS, connection, TLS or protocol failure. |
| `unsupported_content_type` | The response is not HTML or XHTML. |
| `render_unavailable` | `render="always"` in a build without a browser. |

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
| `total_timeout_s` | `30` | Ceiling for one fetch, redirects and body included. |
| `max_body_bytes` | `5000000` | Response size cap, measured after decompression. |
| `max_redirects` | `10` | Redirect hops followed. |
| `max_links` | `200` | Links returned per page. |
| `max_content_chars` | `100000` | Markdown characters returned per page. |

Each setting has an environment variable named `WEFT_` plus the uppercase setting name, for example `WEFT_MAX_LINKS`.

## Limits

- Weft reads HTML and XHTML. PDFs, JSON and images return `unsupported_content_type`.
- No JavaScript execution until Phase 3. A page that builds its content in the browser comes back thin.
- Cookies are neither stored nor sent. Authenticated crawling will take cookies per job.
- Weft stores nothing from a fetch yet. The SQLite file is created and migrated at startup, and later phases fill it.

## Development

```bash
pytest                  # about 30 seconds, includes one real stdio round trip
ruff check .
ruff format --check .
mypy                    # strict, covers src/weft
```

Tests never touch the internet. They run against a small local web server in `tests/fixture_site.py`.

## More

- [docs/Architecture.md](docs/Architecture.md): modules, data flow, schema
- [docs/Phases.md](docs/Phases.md): what is done and what comes next
- [docs/BuildLog.md](docs/BuildLog.md): decisions and problems, phase by phase
