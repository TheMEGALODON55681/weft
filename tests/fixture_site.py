"""A small local web server for integration tests. Nothing here touches the real internet.

Routes are registered in `ROUTES` (exact path) and `PREFIX_ROUTES`. Each handler gets the
request handler and writes the whole response itself, so odd behaviour (drips, loops,
bad encodings) stays visible in one place.
"""

import gzip
import ssl
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import parse_qs, urlsplit

DEFAULT_ROBOTS = """User-agent: *
Disallow: /private/
Allow: /private/open
Disallow: /*.pdf$
"""

ARTICLE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Pricing and plans | Acme</title>
<meta name="description" content="Plans for every team size.">
<meta name="author" content="Acme Docs">
<meta property="og:site_name" content="Acme">
<link rel="canonical" href="/pricing">
</head><body>
<nav><a href="/">Home</a> <a href="/docs">Docs</a></nav>
<article>
<h1>Pricing and plans</h1>
<p>Acme offers three plans built around how many people use the product each month. The
free plan covers individuals and small experiments, while the team plan adds shared
workspaces, audit logs and priority support for growing companies.</p>
<p>Every plan includes unlimited projects and a generous monthly allowance of API calls.
When a team outgrows its allowance, usage is billed at a flat rate per thousand calls, so
there are no surprise tiers to climb. Annual billing takes twenty percent off the list price.</p>
<h2>Team plan</h2>
<p>The team plan costs twelve dollars per seat each month. It adds single sign-on, role based
access control and a ninety day audit history. Seats can be added or removed at any time and
the invoice is prorated to the day.</p>
<ul><li>Shared workspaces</li><li>Audit logs</li><li>Priority support</li></ul>
<p>See the <a href="/pricing/enterprise#contact">enterprise page</a> for custom terms, or read
the <a href="https://other.test/review" rel="nofollow">independent review</a>.</p>
<p><a href="mailto:sales@acme.test">Email sales</a> <a href="javascript:void(0)">menu</a>
<a href="#top">Back to top</a> <a href="/pricing/enterprise#other">enterprise again</a></p>
</article>
<footer>Copyright Acme</footer>
</body></html>
"""

_GZIP_BOMB = gzip.compress(b"\0" * 20_000_000)
_LATIN1_HTML = (
    "<html><head><title>Caf\xe9</title></head><body><p>cr\xe8me br\xfbl\xe9e</p></body></html>"
)


def _send(
    h: BaseHTTPRequestHandler,
    body: bytes = b"",
    status: int = 200,
    ctype: str = "text/html; charset=utf-8",
    headers: dict[str, str] | None = None,
) -> None:
    h.send_response(status)
    if ctype:
        h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(body)


def _redirect(h: BaseHTTPRequestHandler, location: str, status: int = 302) -> None:
    _send(h, status=status, headers={"Location": location})


def _article(h: BaseHTTPRequestHandler) -> None:
    _send(h, ARTICLE_HTML.encode())


def _drip(h: BaseHTTPRequestHandler) -> None:
    # Sends a byte every 50 ms for up to 5 s: never idle long enough to trip a read timeout.
    h.send_response(200)
    h.send_header("Content-Type", "text/html")
    h.end_headers()
    with suppress(BrokenPipeError, ConnectionResetError):
        for _ in range(100):
            h.wfile.write(b"x")
            h.wfile.flush()
            time.sleep(0.05)


def _slow(h: BaseHTTPRequestHandler) -> None:
    time.sleep(2)
    with suppress(BrokenPipeError, ConnectionResetError):
        _send(h, b"<html><title>late</title></html>")


def _ua(h: BaseHTTPRequestHandler) -> None:
    _send(h, (h.headers.get("User-Agent") or "").encode(), ctype="text/plain")


def _echo_cookie(h: BaseHTTPRequestHandler) -> None:
    _send(h, (h.headers.get("Cookie") or "").encode(), ctype="text/plain")


def _endless(h: BaseHTTPRequestHandler) -> None:
    n = int(h.path.rsplit("/", 1)[1])
    _redirect(h, f"/endless/{n + 1}")


def _state(h: BaseHTTPRequestHandler) -> "_Server":
    return cast("_Server", h.server)


def _robots(h: BaseHTTPRequestHandler) -> None:
    body = _state(h).robots
    if body is None:
        _send(h, b"not found", status=404, ctype="text/plain")
    elif isinstance(body, int):
        _send(h, b"unavailable", status=body, ctype="text/plain")
    else:
        _send(h, body.encode(), ctype="text/plain")


def _flaky(h: BaseHTTPRequestHandler) -> None:
    """/flaky/<key>/<n>?s=503&ra=1: fail the first n requests for <key>, then serve the article."""
    st = _state(h)
    key, n = urlsplit(h.path).path.split("/")[2:4]
    query = parse_qs(urlsplit(h.path).query)
    with st.lock:
        st.flaky[key] += 1
        seen = st.flaky[key]
    if seen > int(n):
        _article(h)
        return
    retry_after = {"Retry-After": query["ra"][0]} if "ra" in query else None
    _send(h, b"try later", int(query.get("s", ["503"])[0]), "text/plain", retry_after)


def _reset(h: BaseHTTPRequestHandler) -> None:
    """/reset/<key>/<n>: drop the connection without a response for the first n requests."""
    st = _state(h)
    key, n = urlsplit(h.path).path.split("/")[2:4]
    with st.lock:
        st.flaky[key] += 1
        seen = st.flaky[key]
    if seen > int(n):
        _article(h)


def _hold(h: BaseHTTPRequestHandler) -> None:
    """Answer after 0.3 s, recording how many requests were in flight at once."""
    st = _state(h)
    with st.lock:
        st.active += 1
        st.peak = max(st.peak, st.active)
    try:
        time.sleep(0.3)
    finally:
        with st.lock:
            st.active -= 1
    _send(h, b"<html><title>held</title></html>")


ROUTES: dict[str, Callable[[BaseHTTPRequestHandler], None]] = {
    "/": lambda h: _send(h, b'<html><title>Home</title><a href="/article">a</a></html>'),
    "/article": _article,
    "/redirect": lambda h: _redirect(h, "/article"),
    "/chain/1": lambda h: _redirect(h, "/chain/2", 301),
    "/chain/2": lambda h: _redirect(h, "/article", 307),
    "/loop/a": lambda h: _redirect(h, "/loop/b"),
    "/loop/b": lambda h: _redirect(h, "/loop/a"),
    "/redirect-to-file": lambda h: _redirect(h, "file:///etc/passwd"),
    "/redirect-to-metadata": lambda h: _redirect(h, "http://169.254.169.254/latest/meta-data/"),
    "/redirect-to-private": lambda h: _redirect(h, "http://10.0.0.1/admin"),
    "/redirect-to-mapped": lambda h: _redirect(h, "http://[::ffff:a00:1]/"),
    "/redirect-to-disallowed": lambda h: _redirect(h, "/private/secret"),
    "/redirect-no-location": lambda h: _send(h, status=302),
    "/big": lambda h: _send(h, b"<html><body>" + b"a" * 200_000 + b"</body></html>"),
    "/bomb": lambda h: _send(h, _GZIP_BOMB, headers={"Content-Encoding": "gzip"}),
    "/gzip": lambda h: _send(
        h, gzip.compress(ARTICLE_HTML.encode()), headers={"Content-Encoding": "gzip"}
    ),
    "/pdf": lambda h: _send(h, b"%PDF-1.4 fake", ctype="application/pdf"),
    "/json": lambda h: _send(h, b'{"a": 1}', ctype="application/json"),
    "/latin1": lambda h: _send(
        h, _LATIN1_HTML.encode("latin-1"), ctype="text/html; charset=iso-8859-1"
    ),
    "/meta-charset": lambda h: _send(
        h,
        '<html><head><meta charset="windows-1251"><title>Привет</title></head>'
        "<body><p>Привет, мир</p></body></html>".encode("windows-1251"),
        ctype="text/html",
    ),
    "/status/404": lambda h: _send(h, b"<html><title>Gone</title></html>", status=404),
    "/status/500": lambda h: _send(h, b"oops", status=500, ctype="text/plain"),
    "/empty": lambda h: _send(h, b""),
    "/set-cookie": lambda h: _send(h, b"hi", headers={"Set-Cookie": "sid=abc123; Path=/"}),
    "/echo-cookie": _echo_cookie,
    "/slow": _slow,
    "/drip": _drip,
    "/ua": _ua,
    "/robots.txt": _robots,
    "/ping": lambda h: _send(h, b"<html><title>pong</title></html>"),
    "/hold": _hold,
}
PREFIX_ROUTES: dict[str, Callable[[BaseHTTPRequestHandler], None]] = {
    "/endless/": _endless,
    "/flaky/": _flaky,
    "/reset/": _reset,
    "/private/": lambda h: _send(h, b"<html><title>private</title></html>"),
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        st = _state(self)
        with st.lock:
            st.hits[path] += 1
            st.times[path].append(time.monotonic())
        handler = ROUTES.get(path)
        if handler is None:
            handler = next((f for p, f in PREFIX_ROUTES.items() if path.startswith(p)), None)
        if handler is None:
            _send(self, b"not found", status=404, ctype="text/plain")
        else:
            handler(self)

    def log_message(self, format: str, *args: object) -> None:
        pass


class _Server(ThreadingHTTPServer):
    """The server plus what the handlers record. `robots` is the body of /robots.txt, an HTTP
    status to answer it with, or None for a 404."""

    def __init__(self, robots: str | int | None) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.daemon_threads = True
        self.robots = robots
        self.lock = threading.Lock()
        self.hits: Counter[str] = Counter()
        self.times: defaultdict[str, list[float]] = defaultdict(list)
        self.flaky: Counter[str] = Counter()
        self.active = 0
        self.peak = 0

    def handle_error(self, request: object, client_address: object) -> None:
        pass  # a client that rejects our certificate is a test, not a traceback


class FixtureSite:
    def __init__(self, server: _Server, https: bool = False) -> None:
        self._server = server
        self.base = f"{'https' if https else 'http'}://127.0.0.1:{server.server_address[1]}"

    def url(self, path: str) -> str:
        return self.base + path

    def hits(self, path: str) -> int:
        """Requests received for `path` (query string ignored)."""
        return self._server.hits[path]

    def times(self, path: str) -> list[float]:
        """time.monotonic() at each request for `path`."""
        return list(self._server.times[path])

    @property
    def peak(self) -> int:
        """Most /hold requests ever in flight at once."""
        return self._server.peak


@contextmanager
def run_site(
    robots: str | int | None = DEFAULT_ROBOTS, tls: ssl.SSLContext | None = None
) -> Iterator[FixtureSite]:
    server = _Server(robots)
    if tls:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield FixtureSite(server, https=tls is not None)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
