"""A small local web server for integration tests. Nothing here touches the real internet.

Routes are registered in `ROUTES` (exact path) and `PREFIX_ROUTES`. Each handler gets the
request handler and writes the whole response itself, so odd behaviour (drips, loops,
bad encodings) stays visible in one place.
"""

import gzip
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


ROUTES: dict[str, Callable[[BaseHTTPRequestHandler], None]] = {
    "/": lambda h: _send(h, b'<html><title>Home</title><a href="/article">a</a></html>'),
    "/article": _article,
    "/redirect": lambda h: _redirect(h, "/article"),
    "/chain/1": lambda h: _redirect(h, "/chain/2", 301),
    "/chain/2": lambda h: _redirect(h, "/article", 307),
    "/loop/a": lambda h: _redirect(h, "/loop/b"),
    "/loop/b": lambda h: _redirect(h, "/loop/a"),
    "/redirect-to-file": lambda h: _redirect(h, "file:///etc/passwd"),
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
}
PREFIX_ROUTES: dict[str, Callable[[BaseHTTPRequestHandler], None]] = {"/endless/": _endless}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        handler = ROUTES.get(path)
        if handler is None:
            handler = next((f for p, f in PREFIX_ROUTES.items() if path.startswith(p)), None)
        if handler is None:
            _send(self, b"not found", status=404, ctype="text/plain")
        else:
            handler(self)

    def log_message(self, format: str, *args: object) -> None:
        pass


class FixtureSite:
    def __init__(self, server: ThreadingHTTPServer) -> None:
        self._server = server
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def url(self, path: str) -> str:
        return self.base + path


@contextmanager
def run_site() -> Iterator[FixtureSite]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield FixtureSite(server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
