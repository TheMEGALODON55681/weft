"""A small local web server for integration tests. Nothing here touches the real internet.

Routes are registered in `ROUTES` (exact path) and `PREFIX_ROUTES`. Each handler gets the
request handler and writes the whole response itself, so odd behaviour (drips, loops,
bad encodings) stays visible in one place.
"""

import gzip
import random
import ssl
import textwrap
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

# Content exists only after the script runs. Over plain HTTP the page is an empty mount point.
JS_SHELL_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Shell</title></head>
<body><div id="root"></div>
<script>
document.getElementById("root").innerHTML = "<article><h1>Rendered report</h1><p>" +
  "This paragraph was written by a script after the page loaded. ".repeat(12) + "</p></article>";
</script></body></html>
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


def _goto(h: BaseHTTPRequestHandler) -> None:
    """/goto?u=<url>: redirect to a target the test chooses."""
    _redirect(h, parse_qs(urlsplit(h.path).query)["u"][0])


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


# --- a small site to crawl (Phase 4) -------------------------------------------------------
#
# /site/ links to a pricing section (the goal in most tests), unrelated pages, two traps, a
# near-duplicate pair, an exact duplicate, a canonical alias, a nofollow link, a path robots.txt
# disallows and an off-site link. Every page is long enough for trafilatura to keep its text.


def _page(
    title: str, body: str, links: list[tuple[str, str]] | None = None, head: str = ""
) -> bytes:
    anchors = " ".join(f'<a href="{href}">{text}</a>' for href, text in links or [])
    return (
        f"<!doctype html><html lang=en><head><meta charset=utf-8><title>{title}</title>{head}"
        f"</head><body><nav>{anchors}</nav><article><h1>{title}</h1>{body}</article>"
        "</body></html>"
    ).encode()


def _para(*sentences: str) -> str:
    return "".join(f"<p>{s}</p>" for s in sentences)


_FILLER = (
    "The garden club meets on the first Saturday of every month in the community hall. "
    "Members bring seedlings to swap, and the committee reports on the orchard and the ponds."
)
_PRICING = _para(
    "Our pricing has three plans. The starter plan is free for one person and includes the "
    "core editor, two projects and community support through the public forum.",
    "The team plan costs twelve dollars per seat per month, billed monthly or annually, and "
    "adds shared workspaces, audit logs, single sign-on and priority support.",
    "Annual billing takes twenty percent off every plan, and seats can be added or removed at "
    "any time with prorated invoices.",
)
_RELEASE = _para(
    "Release notes for version four. The importer now reads spreadsheets with merged cells, "
    "the exporter keeps column widths, and the search box remembers the last ten queries.",
    "Several fixes landed as well: dates in the Samoan time zone no longer shift by a day, the "
    "sidebar keeps its width after a restart, and printing a long table no longer cuts rows.",
)

_SHORT_TEXT = (  # 21 words and over 250 characters: kept by trafilatura, under MIN_WORDS
    "The interoperability documentation describes internationalization, authentication, "
    "authorization, containerization, orchestration, synchronization, instrumentation, "
    "observability, virtualization, administration, troubleshooting and accessibility. "
    "Notwithstanding unprecedented technological advancements."
)

SITE_PAGES: dict[str, bytes] = {
    "/site/": _page(
        "Acme home",
        _para(
            "Acme makes a collaborative editor for small teams. This is the home page of the "
            "test site, with links to every section and to a few things a crawler should avoid."
        ),
        [
            ("/site/about", "About us"),
            ("/site/blog", "Blog"),
            ("/site/calendar?month=1", "Events calendar"),
            ("/trap/1", "Archive"),
            ("/site/dup-a", "Release notes"),
            ("/site/dup-b", "Release notes mirror"),
            ("/site/dup-c", "Release notes copy"),
            ("/site/alias?utm_source=home", "Plans"),
            ("/site/pricing", "Pricing"),
            ("/private/site-secret", "Staff"),
            ("/site/missing", "Old page"),
            ("/pdf", "Brochure"),
            ("http://other.test/elsewhere", "Partner"),
        ],
    ),
    "/site/pricing": _page(
        "Pricing",
        _PRICING,
        [("/site/pricing/enterprise", "Enterprise pricing"), ("/site/", "Home")],
    ),
    "/site/pricing/enterprise": _page(
        "Enterprise pricing",
        _para(
            "Enterprise pricing is quoted per contract. Plans include a dedicated support "
            "engineer, a custom data processing agreement and invoicing in local currency.",
            "Contact sales for enterprise pricing on more than five hundred seats.",
        ),
    ),
    "/site/alias": _page(
        "Plans",
        _PRICING,
        head='<link rel="canonical" href="/site/pricing">',
    ),
    "/site/about": _page("About us", _para(_FILLER, "We are based in a small town by the sea.")),
    "/site/blog": _page(
        "Blog",
        _para(_FILLER) + '<p><a href="/site/nofollowed" rel="nofollow">Sponsored</a></p>',
        [(f"/site/blog/{i}", f"Garden diary {i}") for i in range(1, 4)],
    ),
    **{
        f"/site/blog/{i}": _page(
            f"Garden diary {i}",
            _para(f"Entry {i}. " + _FILLER, f"This week we planted row {i} of the beans."),
        )
        for i in range(1, 4)
    },
    "/site/nofollowed": _page("Sponsored", _para(_FILLER)),
    # dup-b differs from dup-a by one date: a near duplicate. dup-c is dup-a with other spacing.
    "/site/dup-a": _page("Release notes", _RELEASE + _para("Published on 3 March.")),
    "/site/dup-b": _page("Release notes", _RELEASE + _para("Published on 4 March.")),
    "/site/dup-c": _page(
        "Release notes", _RELEASE.replace(". ", ".   ") + _para("Published on 3 March.")
    ),
    # Reached only from /site/edge, which nothing else links to. The loop has one segment three
    # times. The two short pages have the same text, under the word count where simhash applies.
    "/site/edge": _page(
        "Edge cases",
        _para(_FILLER, "This page links to a path that loops and to two short pages."),
        [
            ("/site/edge/a/b/a/b/a/b", "Loop"),
            ("/site/edge/short-a", "Short A"),
            ("/site/edge/short-b", "Short B"),
        ],
    ),
    **{f"/site/edge/short-{x}": _page("Glossary", _para(_SHORT_TEXT)) for x in ("a", "b")},
}


# 300 KB of nested divs after a title and a paragraph: lexbor took two minutes on a bigger one.
DEEP_HTML = (
    "<html><head><title>Kept title</title></head><body><p>"
    + "word " * 40
    + "</p>"
    + "<div>" * 60_000
    + "</body></html>"
).encode()


# A script nests the DOM 8,000 levels deep, whatever the parser would have allowed.
DEEP_JS_HTML = (
    "<html><head><title>Kept title</title></head><body><p>" + "word " * 60 + "</p>"
    "<script>let n = document.body;"
    "for (let i = 0; i < 8000; i++) { const d = document.createElement('div');"
    " n.appendChild(d); n = d; }</script></body></html>"
).encode()


def _site(h: BaseHTTPRequestHandler) -> None:
    _send(h, SITE_PAGES[urlsplit(h.path).path])


_WORD_LIST = """amber basil cedar dune ember fjord grove heron iris juniper kelp lichen maple nectar
oak pebble quartz reed sage thistle umber violet willow yarrow zephyr"""
_WORDS = _WORD_LIST.split()


def _unique_text(n: int) -> str:
    """Sixty words that differ for every n, so trap pages are not near-duplicates of each other
    and dedupe cannot be what ends the trap."""
    rng = random.Random(n)  # noqa: S311 - filler text, not a secret
    return " ".join(rng.choice(_WORDS) for _ in range(60)) + "."


# Reached only from /site/anchors. Its four links share a URL shape and lead to pages with no
# words in common with the goal "refund policy", and the page that lists them says nothing
# about it either. The matching link is listed last, so a crawl that ignored anchor text would
# tie all four and take the first one listed.
SITE_PAGES["/site/anchors"] = _page(
    "Departments",
    _para(_FILLER, "Pick a department below."),
    [
        ("/site/anchors/a1", "Contact"),
        ("/site/anchors/a2", "Careers"),
        ("/site/anchors/a3", "Partners"),
        ("/site/anchors/a4", "Refund policy"),
    ],
)
for _i in range(1, 5):
    SITE_PAGES[f"/site/anchors/a{_i}"] = _page(
        f"Department {_i}", _para(f"Department {_i}. " + _unique_text(2000 + _i))
    )


# A job board for extraction. The list holds records (.role) and the detail pages each have a
# title (the h1) and a salary (.salary). The list has no .salary, so a schema that requires one
# matches the detail pages and not the list. Each detail page has its own text.
_ROLES = [("Data Analyst", 80500, "/site/jobs/1"), ("Designer", 95000, "/site/jobs/2")]
SITE_PAGES["/site/jobs"] = _page(
    "Careers",
    _para("Open roles at Acme, a small company that makes a collaborative editor for teams.")
    + "".join(
        f'<div class="role"><h2>{title}</h2><span class="pay">${pay:,}</span>'
        f'<a href="{href}">Details</a></div>'
        for title, pay, href in _ROLES
    ),
    [(href, title) for title, _pay, href in _ROLES],
)
for _n, (_title, _pay, _href) in enumerate(_ROLES):
    SITE_PAGES[_href] = _page(
        _title,
        _para(f"Join the {_title.lower()} team. " + _unique_text(3000 + _n))
        + f'<p>Salary: <span class="salary">${_pay:,}</span> a year.</p>',
    )


# A docs site for passage search (Phase 8), reached only from /site/docs. Its paragraphs are
# written the way a docs generator writes them: wrapped at 72 columns in the source, with inline
# markup. trafilatura keeps the line breaks that sit next to an element, so the Markdown comes
# out hard-wrapped, which is what used to cut passages in the middle of a sentence.
#
# Six paragraphs of the section "Task.cancel" are about the query "cancel a task" and nothing else
# is, so a search ranks that one section first six times over. The guide repeats a note from the
# docs' "Timeouts" with a word changed, and the news page mentions the query once.


def _wrapped(text: str) -> str:
    return f"<p>{textwrap.fill(text, 72, break_on_hyphens=False)}</p>"


# Said in the docs under "Timeouts" and again, with one word changed, in the guide.
_TIMEOUT_NOTE = (
    "When a timeout fires, the request to cancel is delivered to the task that is waiting, and "
    "the task must let the error go on up to the timeout block. A task that catches the error "
    "and carries on makes the timeout fail, so the caller waits for as long as the task chooses "
    "to keep running."
)


_CANCEL_SECTION = [
    "To <code>cancel</code> a task, call <code>task.cancel()</code>. The task is not stopped at "
    "once: the request arrives as a <code>CancelledError</code> at the next await, so a task that "
    "is busy keeps running until it yields. Cancelling a task that has finished does nothing and "
    "returns false.",
    "A task that is asked to cancel may clean up first. Wrap the awaited work in <code>try</code> "
    "and <code>finally</code> so that files and locks are released when the task is cancelled, "
    "and let the <code>CancelledError</code> go on up to the caller of the task.",
    "Do not swallow the error. If a task catches <code>CancelledError</code> and carries on, "
    "anyone who awaits the task will believe it finished normally, and a request to cancel the "
    "task has been quietly ignored. Re-raise it after the clean-up.",
    "You can find out afterwards whether a task was cancelled. The <code>cancelled()</code> "
    "method returns true once the task has stopped because of a cancel request, and awaiting "
    "such a task raises the error again in the waiting code.",
    "To cancel many tasks at once, collect them in a list and call <code>cancel()</code> on "
    "each one, then gather the list with <code>return_exceptions</code> set, so that one "
    "cancelled task does not hide the result of the others.",
    "A cancel request can be given a message. The text passed to <code>cancel()</code> travels "
    "with the <code>CancelledError</code>, which helps when several parts of a program may "
    "cancel the same task for different reasons.",
]
SITE_PAGES["/site/docs"] = _page(
    "Cancelling tasks",
    "<h2>Task.cancel</h2>"
    + "".join(_wrapped(p) for p in _CANCEL_SECTION)
    + "<h2>Timeouts</h2>"
    + _wrapped(
        "A timeout will <code>cancel</code> the task it guards when the time is up. The block "
        "is interrupted, and the error is turned into a <code>TimeoutError</code> once the "
        "block has been left, so the caller sees only the latter."
    )
    + _wrapped(_TIMEOUT_NOTE)
    + "<h2>TaskGroup</h2>"
    + _wrapped(
        "A group cancels its other tasks when one of them fails. Each remaining task "
        "receives the request, and the group waits until every one of them has stopped "
        "before it raises, so nothing is left running behind the <code>async with</code> "
        "block."
    )
    + "<h2>Shielding</h2>"
    + _wrapped(
        "Shielding protects an inner task from a cancel request made to the task that "
        "awaits it, which is useful for work that must finish, such as writing a record "
        "to disk with <code>shield()</code>."
    ),
    [("/site/docs/guide", "Guide"), ("/site/docs/news", "News")],
)
SITE_PAGES["/site/docs/guide"] = _page(
    "Cancellation guide",
    "<h2>Handling cancellation</h2>"
    + _wrapped(_TIMEOUT_NOTE.replace("chooses", "decides"))
    + _wrapped(
        "In a long loop, check for a request to cancel the task between units of work and "
        "stop at a point where the data is consistent, then re-raise the "
        "<code>CancelledError</code> so the task is marked as cancelled."
    ),
)
SITE_PAGES["/site/docs/news"] = _page(
    "Release news",
    "<h2>This month</h2>"
    + _wrapped(
        "The dashboard now lets you cancel a task from the list of running work, and the "
        "export button keeps its place after a refresh. The mobile layout is tidier too, "
        "with <code>wider</code> buttons for touch screens."
    ),
)


LOAD_FANOUT = 5


def _load(h: BaseHTTPRequestHandler) -> None:
    """/load/<i>: page i of a generated site of `load_pages` pages (see `run_site`). Page i links to
    pages 5i+1 to 5i+5, so the site is a tree and a crawl that reaches every page reaches each one
    once. Every page has text of its own, so none is a near duplicate of another, and takes 2 ms,
    so that concurrent requests overlap and the peak the server records means something."""
    st = _state(h)
    try:
        i = int(urlsplit(h.path).path.rsplit("/", 1)[1])
    except ValueError:
        i = -1
    if not 0 <= i < st.load_pages:
        _send(h, b"not found", status=404, ctype="text/plain")
        return
    with st.lock:
        st.load_active += 1
        st.load_peak = max(st.load_peak, st.load_active)
    try:
        time.sleep(0.002)
    finally:
        with st.lock:
            st.load_active -= 1
    children = range(LOAD_FANOUT * i + 1, LOAD_FANOUT * i + 1 + LOAD_FANOUT)
    links = [(f"/load/{c}", f"Page {c}") for c in children if c < st.load_pages]
    text = _para(*(f"Page {i}, part {k}. " + _unique_text(i + k * 10_000_000) for k in range(3)))
    _send(h, _page(f"Load page {i}", text, links))


# Reached only from here. One link goes to a host that idna cannot decode: "xn--" is made of
# letters, so it canonicalises, and it used to stop the crawl in the robots.txt lookup.
SITE_PAGES["/site/odd-hosts"] = _page(
    "Odd hosts",
    _para(_FILLER, "This page links to a host with a malformed internationalised name."),
    [("http://xn--/a", "Strange"), ("/site/about", "About us")],
)


def _trap(h: BaseHTTPRequestHandler) -> None:
    """/trap/<n>: an archive that never ends. Each page links to the next one."""
    n = int(urlsplit(h.path).path.rsplit("/", 1)[1])
    _send(
        h,
        _page(
            f"Archive {n}",
            _para(f"Archive page {n}. " + _unique_text(n)),
            [(f"/trap/{n + 1}", "Older")],
        ),
    )


def _calendar(h: BaseHTTPRequestHandler) -> None:
    """/site/calendar?month=<n>: next and previous month, forever."""
    n = int(parse_qs(urlsplit(h.path).query).get("month", ["1"])[0])
    links = [
        (f"/site/calendar?month={n + 1}", "Next month"),
        (f"/site/calendar?month={n - 1}", "Previous month"),
    ]
    _send(
        h,
        _page(
            f"Events in month {n}",
            _para(f"Nothing on in month {n}. " + _unique_text(10_000 + n)),
            links,
        ),
    )


SITEMAP_INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>/sitemaps/pages.xml</loc></sitemap>
<sitemap><loc>/sitemaps/more.xml.gz</loc></sitemap>
<sitemap><loc>/sitemaps/missing.xml</loc></sitemap>
</sitemapindex>"""

SITEMAP_PAGES = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc> {base}/site/pricing </loc><lastmod>2026-11-01</lastmod></url>
<url><loc>{base}/site/about</loc></url>
<url><loc>{base}/site/search?q=a&amp;page=2</loc></url>
<url><loc>{base}/private/listed</loc></url>
<url><loc>http://other.test/not-ours</loc></url>
</urlset>"""

SITEMAP_MORE = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>{base}/site/blog/1</loc></url></urlset>"""


def _sitemap(h: BaseHTTPRequestHandler, body: bytes, gz: bool = False) -> None:
    host = (h.headers.get("Host") or "").encode()
    body = body.replace(b"{base}", b"http://" + host).replace(
        b"<loc>/", b"<loc>http://" + host + b"/"
    )
    if gz:
        _send(h, gzip.compress(body), ctype="application/gzip")
    else:
        _send(h, body, ctype="application/xml")


def _etag(h: BaseHTTPRequestHandler) -> None:
    """/etag: the article with an ETag, and a 304 when the client already has it."""
    if h.headers.get("If-None-Match") == '"v1"':
        _send(h, status=304, ctype="", headers={"ETag": '"v1"'})
    else:
        _send(h, ARTICLE_HTML.encode(), headers={"ETag": '"v1"'})


_LAST_MODIFIED = "Tue, 03 Mar 2026 10:00:00 GMT"


def _lastmod(h: BaseHTTPRequestHandler) -> None:
    """/lastmod: as /etag, with Last-Modified and If-Modified-Since."""
    if h.headers.get("If-Modified-Since") == _LAST_MODIFIED:
        _send(h, status=304, ctype="")
    else:
        _send(h, ARTICLE_HTML.encode(), headers={"Last-Modified": _LAST_MODIFIED})


def _blob(h: BaseHTTPRequestHandler) -> None:
    """/blob/<n>/<name>: n bytes of image, for byte budgets."""
    n = int(urlsplit(h.path).path.split("/")[2])
    _send(h, b"\x89PNG" + b"\0" * max(0, n - 4), ctype="image/png")


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
    "/status/400": lambda h: _send(h, b"<html><title>Bad</title></html>", status=400),
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
    "/js-shell": lambda h: _send(h, JS_SHELL_HTML.encode()),
    "/deep": lambda h: _send(h, DEEP_HTML),
    "/deep-js": lambda h: _send(h, DEEP_JS_HTML),
    "/goto": _goto,
    "/script.js": lambda h: _send(h, b"window.loaded = true;", ctype="text/javascript"),
    "/sw.js": lambda h: _send(h, b"self.oninstall = () => {};", ctype="text/javascript"),
    "/echo-body": lambda h: _send(
        h, h.rfile.read(int(h.headers.get("Content-Length") or 0)), ctype="text/plain"
    ),
    "/host": lambda h: _send(h, (h.headers.get("Host") or "").encode(), ctype="text/plain"),
    **{path: _site for path in SITE_PAGES},
    "/site/calendar": _calendar,
    "/sitemap.xml": lambda h: _sitemap(h, SITEMAP_INDEX),
    "/sitemaps/pages.xml": lambda h: _sitemap(h, SITEMAP_PAGES),
    "/sitemaps/more.xml.gz": lambda h: _sitemap(h, SITEMAP_MORE, gz=True),
    "/etag": _etag,
    "/lastmod": _lastmod,
}
PREFIX_ROUTES: dict[str, Callable[[BaseHTTPRequestHandler], None]] = {
    "/endless/": _endless,
    "/flaky/": _flaky,
    "/reset/": _reset,
    "/private/": lambda h: _send(h, b"<html><title>private</title></html>"),
    "/trap/": _trap,
    "/load/": _load,
    "/blob/": _blob,
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

    # Count every method, so a test that expects no contact at all also sees beacons (POST) and
    # CORS preflights (OPTIONS).
    do_POST = do_PUT = do_DELETE = do_OPTIONS = do_HEAD = do_GET

    def log_message(self, format: str, *args: object) -> None:
        pass


class _Server(ThreadingHTTPServer):
    """The server plus what the handlers record. `robots` is the body of /robots.txt, an HTTP
    status to answer it with, or None for a 404."""

    def __init__(self, robots: str | int | None, load_pages: int = 0) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.daemon_threads = True
        self.robots = robots
        self.load_pages = load_pages
        self.load_active = 0
        self.load_peak = 0
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

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def total_hits(self) -> int:
        """Requests received for any path."""
        return sum(self._server.hits.values())

    def hits(self, path: str) -> int:
        """Requests received for `path` (query string ignored)."""
        return self._server.hits[path]

    def hits_under(self, prefix: str) -> dict[str, int]:
        """Paths starting with `prefix` that were requested, with their counts."""
        return {p: n for p, n in self._server.hits.items() if p.startswith(prefix)}

    def times(self, path: str) -> list[float]:
        """time.monotonic() at each request for `path`."""
        return list(self._server.times[path])

    @property
    def peak(self) -> int:
        """Most /hold requests ever in flight at once."""
        return self._server.peak

    @property
    def load_peak(self) -> int:
        """Most /load requests ever in flight at once."""
        return self._server.load_peak


@contextmanager
def run_site(
    robots: str | int | None = DEFAULT_ROBOTS,
    tls: ssl.SSLContext | None = None,
    load_pages: int = 0,
) -> Iterator[FixtureSite]:
    """`load_pages` is the size of the generated site under /load/, 0 for none."""
    server = _Server(robots, load_pages)
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
