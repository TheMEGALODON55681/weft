# ruff: noqa: RUF001  (hostile Unicode is the point of several strings here)
"""Fuzzing. Hypothesis feeds the URL canonicaliser and every parser that reads a stranger's bytes
(HTML, robots.txt, sitemaps, response bodies) with what a hostile site or a careless agent might
send, and each test states one property that must hold for all of it: the code raises only what
its callers catch, returns something its callers can use, and finishes in time.

A failure prints a minimal example. Turn it into an `@example(...)` on the test, or into a plain
case in the module for that code, so the fix has a regression test of its own.

The default run draws the same 1000 examples every time. `WEFT_FUZZ_EXAMPLES=20000` draws
that many from a random seed (see conftest.py).
"""

import contextlib
import re
import string
import time
import zlib
from typing import Any
from urllib.parse import urlsplit

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.strategies import DrawFn

from weft.extract.content import extract_page
from weft.extract.decode import decode_body
from weft.extract.links import extract_links
from weft.extract.nesting import cap_nesting
from weft.extract.schema import DataTooLarge, Extractor
from weft.fetch.errors import FetchError
from weft.fetch.http import parse_http_url
from weft.fetch.page import wants_render
from weft.fetch.robots import Rules, _matches, normalise, parse_robots
from weft.frontier.canon import canonicalize, pattern, repeats_segments, site_key
from weft.frontier.sitemap import _gunzip, parse_sitemap

# --- URLs -----------------------------------------------------------------------------------

HOSTS = [
    "example.com", "EXAMPLE.com.", "bücher.example", "[::1]", "[::ffff:127.0.0.1]", "[::1",
    "::1]", "127.0.0.1", "0x7f.1", "2130706433", "a..b", "-.com", "xn--", "xn--a", "日本語.jp",
    "ЯНДЕКС.рф", "a" * 70 + ".com", "", " ", "a b.com", "exa%6Dple.com", "ex\x00ample.com",
    "ｅｘａｍｐｌｅ.com", "℀.com", "a.b.c.d.e.f", "…", "例え.jp.", ".", "a@b", "a:b", "a/b",
]  # fmt: skip
SCHEMES = ["http", "https", "HTTP", "HtTpS", "ftp", "mailto", "javascript", "data", "file", ""]
PORTS = ["", ":80", ":443", ":0", ":65535", ":65536", ":99999", ":abc", ":-1", ":", ":8080"]
PORTS += [": 80", ":+80", ":٨٠"]
PIECES = list("aAzZ09-._~/%:@!$&'()*+,;=?#[]\\ <>\"{}|^`")
PIECES += ["%2F", "%2f", "%41", "%zz", "%", "é", "ツ", "..", "/./", "/../", "\x00", "\n", "\t"]
PIECES += [chr(0x2028), "%E3%83%84", "%C3", "%FF"]

pieces = st.lists(st.sampled_from(PIECES), max_size=12).map("".join)
url_like = st.builds(
    lambda scheme, userinfo, host, port, path, query, frag: (
        f"{scheme}://{userinfo}{host}{port}{path}{query}{frag}"
    ),
    st.sampled_from(SCHEMES),
    st.sampled_from(["", "u@", "u:p@", "@", ":@", "u:@"]),
    st.one_of(
        st.sampled_from(HOSTS),
        st.from_regex(r"[A-Za-z0-9.-]{0,25}", fullmatch=True),
        st.text(max_size=12),
    ),
    st.sampled_from(PORTS),
    st.one_of(st.just(""), pieces.map("/".join)),
    st.one_of(st.just(""), pieces.map("?".__add__)),
    st.one_of(st.just(""), pieces.map("#".__add__)),
)
any_url = st.one_of(url_like, st.text(max_size=60), pieces)


@given(any_url)
def test_canonicalize_refuses_with_value_error_or_returns_a_stable_url(url: str) -> None:
    """The crawl catches ValueError and nothing else, so nothing else may come out. What comes back
    is the key for the frontier and the cache: it must be stable, ASCII, free of a fragment and of
    credentials, and one whose host is a name or an address."""
    try:
        canon = canonicalize(url)
    except ValueError:
        return
    parts = urlsplit(canon)
    assert parts.scheme in ("http", "https")
    assert parts.hostname
    assert parts.username is None
    assert not parts.fragment
    assert canon.isascii()
    assert parts.hostname == parts.hostname.strip(".")
    assert not any(c.isspace() or ord(c) < 32 for c in canon)
    assert canonicalize(canon) == canon
    with contextlib.suppress(FetchError):  # the fetcher may refuse it, but only in its own way
        parse_http_url(canon)
    pattern(canon)
    repeats_segments(canon)
    site_key(parts.hostname)


@given(any_url)
def test_parse_http_url_refuses_only_with_a_fetch_error(url: str) -> None:
    with contextlib.suppress(FetchError):
        parse_http_url(url)


WORD = st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=6)


@st.composite
def two_spellings(draw: DrawFn) -> tuple[str, str]:
    """One URL written plainly and written noisily: other case, a default port, a trailing dot,
    dot segments, escapes of letters in either hex case, shuffled parameters, a tracking
    parameter and a fragment."""

    def escaped(text: str) -> str:
        out = []
        for ch in text:
            if draw(st.booleans()):
                hexed = format(ord(ch), "02x")
                out.append("%" + (hexed.upper() if draw(st.booleans()) else hexed))
            else:
                out.append(ch)
        return "".join(out)

    scheme = draw(st.sampled_from(["http", "https"]))
    host = ".".join([*draw(st.lists(WORD, min_size=1, max_size=3)), "test"]).lower()
    segs = draw(st.lists(WORD, max_size=4))
    pairs = draw(
        st.lists(st.tuples(WORD.map("k".__add__), WORD), max_size=4, unique_by=lambda p: p[0])
    )
    slash = "/" if segs and draw(st.booleans()) else ""
    query = "&".join(f"{k}={v}" for k, v in pairs)
    plain = f"{scheme}://{host}/{'/'.join(segs)}{slash}" + (f"?{query}" if query else "")

    pieces_of_path: list[str] = []
    for i, seg in enumerate(segs):
        pieces_of_path.append(escaped(seg))
        if i < len(segs) - 1:  # dot segments between segments, so the result keeps its shape
            pieces_of_path.extend(
                draw(st.sampled_from([(), (".",), ("x", ".."), (".", "x", "..")]))
            )
    path = "/".join(pieces_of_path)
    noisy_pairs = [f"{escaped(k)}={escaped(v)}" for k, v in draw(st.permutations(pairs))]
    noisy_pairs.insert(draw(st.integers(0, len(noisy_pairs))), "utm_source=" + draw(WORD))
    port = {"http": ":80", "https": ":443"}[scheme] if draw(st.booleans()) else ""
    dot = "." if draw(st.booleans()) else ""
    noisy = (
        f"{draw(st.sampled_from([scheme, scheme.upper(), scheme.title()]))}://"
        f"{draw(st.sampled_from([host, host.upper()]))}{dot}{port}/{path}{slash}"
        f"?{'&'.join(noisy_pairs)}#{draw(WORD)}"
    )
    return plain, noisy


@given(two_spellings())
def test_two_spellings_of_one_url_have_one_canonical_form(pair: tuple[str, str]) -> None:
    plain, noisy = pair
    assert canonicalize(noisy) == canonicalize(plain)


# --- links ----------------------------------------------------------------------------------

ODD_HREFS = ["", " ", "#", "/", "//", "///", "//host", "?q", ".", "..", "../..", "http:", "http://"]
ODD_HREFS += ["javascript:alert(1)", "data:text/html,x", "mailto:a@b", "\\\\host\\share"]
ODD_HREFS += ["http:///x"]
HREFS = st.one_of(any_url, st.sampled_from(ODD_HREFS))


@st.composite
def page_with_links(draw: DrawFn) -> str:
    base = draw(st.one_of(st.none(), HREFS))
    head = f'<base href="{base}">' if base is not None else ""
    quote = draw(st.sampled_from(['"', "'", ""]))
    anchors = "".join(f"<a href={quote}{h}{quote}>x</a>" for h in draw(st.lists(HREFS, max_size=8)))
    return f"<html><head>{head}</head><body>{anchors}</body></html>"


def check_links(page_links: list[Any]) -> None:
    seen: set[str] = set()
    for link in page_links:
        parts = urlsplit(link.url)
        assert parts.scheme in ("http", "https")
        assert parts.netloc
        assert "#" not in link.url
        assert link.url not in seen
        seen.add(link.url)
        with contextlib.suppress(ValueError):  # the crawl does exactly this, and catches this
            canonicalize(link.url)


@given(page_with_links())
def test_extracted_links_are_http_urls_that_canonicalise_or_refuse_cleanly(html: str) -> None:
    check_links(extract_links(html, "http://example.test/dir/page"))


# --- HTML -----------------------------------------------------------------------------------

FRAGMENTS = [
    "<html>", "<body>", "<div>", "</div>", "<p>", "</p>", "<a href='/x'>", '<a href="//h/y?a=1#z">',
    "</a>", "<table><tr><td>", "</table>", "<ul><li>", "<script>", "</script>", "<style>",
    "</style>", "<!--", "-->", "<![CDATA[", "]]>", "<svg><foreignObject>", "&amp;", "&#0;",
    "&#x110000;", "&#xD800;", "&bogus", "<noscript>", "</noscript>", "<title>", "</title>",
    "<base href='//x/'>", "<meta charset=bogus>", "<link rel=canonical href=//>",
    "<link rel=canonical href='/c'>", "<a href=", "<", ">", "<<", "<h1>", "</h1>", "<article>",
    "</article>", "<pre>", "<br>", "word " * 20, "\x00", chr(0xFEFF), "<form><input name=a>",
    "<li>", "</li>", "<td>", "<tr>", "<!doctype html>", "<?xml version='1.0'?>", "<html lang=",
]  # fmt: skip
HTML = settings(max_examples=max(10, settings.default.max_examples // 10))  # parsing is slow
soup = st.lists(st.one_of(st.sampled_from(FRAGMENTS), st.text(max_size=40)), max_size=60).map(
    "".join
)


@HTML
@given(soup)
def test_extract_page_never_raises_and_every_link_is_usable(html: str) -> None:
    start = time.perf_counter()
    page = extract_page(html, "http://example.test/dir/")
    assert time.perf_counter() - start < 10
    assert isinstance(page.markdown, str)
    check_links(page.links)


@given(soup, st.integers(1, 40))
def test_cap_nesting_returns_a_prefix_and_is_stable(html: str, limit: int) -> None:
    out = cap_nesting(html, limit)
    assert html.startswith(out)
    assert cap_nesting(out, limit) == out


@given(
    st.lists(
        st.builds(lambda frag, n: frag * n, st.sampled_from(FRAGMENTS), st.integers(1, 40_000)),
        min_size=1,
        max_size=3,
    )
)
def test_the_nesting_scan_is_linear_on_repeated_fragments(parts: list[str]) -> None:
    start = time.perf_counter()
    cap_nesting("".join(parts))
    assert time.perf_counter() - start < 3


@given(
    st.binary(max_size=3000),
    st.one_of(
        st.none(),
        st.sampled_from(["utf-8", "latin-1", "bogus", "utf-16", "", "x" * 100, "ascii", "cp1252"]),
        st.text(max_size=20),
    ),
    st.binary(max_size=40),
)
def test_decode_body_always_returns_text(body: bytes, header: str | None, label: bytes) -> None:
    assert isinstance(decode_body(body, header), str)
    assert isinstance(decode_body(b'<meta charset="' + label + b'">' + body, header), str)


@HTML
@given(soup)
def test_wants_render_and_the_rules_run_on_any_page(html: str) -> None:
    assert isinstance(wants_render(html, "text", 50), bool)
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "x-css": "h1"},
            "first": {"type": "string", "x-css": "a", "x-attr": "href"},
            "items": {"type": "array", "x-xpath": "//li", "items": {"type": "string"}},
        },
    }
    extractor = Extractor(schema)
    try:
        data = extractor.run(html, "http://example.test/", max_chars=2000)
    except DataTooLarge:
        return
    extractor.errors(data)


# --- robots.txt -----------------------------------------------------------------------------

FIXED_LINES = ["User-agent: *", "User-agent: weft", "User-agent:", "Disallow:", "Allow:", "#", ""]
FIXED_LINES += ["Crawl-delay: 1", "Crawl-delay: nan", "Sitemap: /s.xml", "Disallow: /*"]
FIXED_LINES += ["Allow: /$"]
ROBOTS_LINES = st.lists(
    st.one_of(
        st.sampled_from(FIXED_LINES),
        st.builds(
            "{}: {}".format,
            st.sampled_from(["Disallow", "Allow", "disallow", "ALLOW", "Sitemap", "Crawl-delay"]),
            pieces,
        ),
        st.text(max_size=30),
    ),
    max_size=30,
)
ROBOTS_PATHS = st.one_of(pieces.map("/".__add__), st.text(alphabet="/ab%*$?é.~0-9A-F", max_size=30))


@given(ROBOTS_LINES, st.sampled_from(["\n", "\r\n", "\r"]), ROBOTS_PATHS)
def test_any_robots_file_parses_and_answers_for_any_path(
    lines: list[str], eol: str, path: str
) -> None:
    rules = parse_robots(eol.join(lines))
    assert isinstance(rules.allows(path), bool)
    assert rules.crawl_delay is None or rules.crawl_delay >= 0
    assert len(rules.sitemaps) <= 50


def reference_match(pattern_text: str, path: str) -> bool:
    """The rule as a regular expression: `*` is `.*`, a trailing `$` is the end of the path."""
    body = pattern_text.removesuffix("$")
    expr = ".*".join(re.escape(part) for part in body.split("*"))
    return re.match(expr + (r"\Z" if pattern_text.endswith("$") else ""), path, re.S) is not None


PATTERNS = st.text(alphabet="ab/*$?", max_size=12)
PATHS = st.text(alphabet="ab/?$", max_size=14)


@given(PATTERNS, PATHS)
def test_the_matcher_agrees_with_a_regular_expression(pattern_text: str, path: str) -> None:
    assert _matches(pattern_text, path) == reference_match(pattern_text, path)


@given(st.lists(st.tuples(st.booleans(), PATTERNS), max_size=8), PATHS)
def test_the_verdict_is_the_longest_match_and_allow_wins_a_tie(
    rules: list[tuple[bool, str]], path: str
) -> None:
    hits = [(len(p), allow) for allow, p in rules if reference_match(p, path)]
    expected = max(hits)[1] if hits else True
    assert Rules(tuple(rules)).allows(path) is expected


@given(
    st.text(
        alphabet=st.characters(exclude_characters="*$#\r\n", exclude_categories=["Z", "C"]),
        min_size=1,
        max_size=20,
    )
)
def test_a_rule_always_blocks_the_path_it_was_written_as(text: str) -> None:
    """Whatever the characters, rule and path go through one normalisation, so they still meet."""
    assert not parse_robots(f"User-agent: *\nDisallow: /{text}").allows(f"/{text}")


@given(st.text(alphabet=string.ascii_letters + string.digits + "-._~", min_size=1, max_size=12))
def test_an_escaped_unreserved_character_means_the_character(text: str) -> None:
    for hex_case in (str.upper, str.lower):
        escaped = "".join("%" + hex_case(format(ord(c), "02x")) for c in text)
        assert normalise(escaped) == text


@given(st.text(max_size=40))
def test_normalise_gives_ascii_and_never_raises(text: str) -> None:
    assert normalise(text).isascii()


# --- sitemaps -------------------------------------------------------------------------------

TAGS = [b"<url>", b"</url>", b"<sitemap>", b"</sitemap>", b"<loc>", b"</loc>", b"<lastmod>"]
TAGS += [b"</lastmod>", b" ", b"\n", b"x", b"<", b"&amp;", b"<![CDATA[", b"http://a.test/"]


@given(st.binary(max_size=2000))
def test_a_sitemap_of_any_bytes_is_read_without_raising(body: bytes) -> None:
    children, entries = parse_sitemap(body)
    assert all(isinstance(c, str) for c in children)
    assert all(isinstance(u, str) and (m is None or isinstance(m, str)) for u, m in entries)


@given(
    st.lists(
        st.builds(lambda tag, n: tag * n, st.sampled_from(TAGS), st.integers(1, 30_000)),
        min_size=1,
        max_size=4,
    )
)
def test_a_sitemap_built_from_repeated_tags_is_read_in_linear_time(parts: list[bytes]) -> None:
    """A tag or a run of spaces repeated tens of thousands of times: what broke the first parser."""
    start = time.perf_counter()
    parse_sitemap(b"".join(parts))
    assert time.perf_counter() - start < 2


@given(st.binary(max_size=500), st.integers(1, 200))
def test_gunzip_never_raises_and_never_exceeds_the_cap(body: bytes, cap: int) -> None:
    assert len(_gunzip(body, cap)) <= cap
    assert len(_gunzip(zlib.compress(body), cap)) <= cap


HOSTILE_PAGES = {
    "nested divs": "<html><body>" + "<div>" * 200_000 + "x</body></html>",
    "nested inline": "<p>" + "<span>" * 50_000 + "words " * 30 + "</p>",
    "nested tables": "<table><tr><td>" * 8_000 + "cell",
    "unclosed bold": "<html><body>" + "<b>word " * 100_000 + "</body></html>",
    "comment openers": "<html><body>" + "<!--" * 50_000 + "</body></html>",
    "bare angle brackets": "<" * 200_000,
    "tags that never end": "<a " * 200_000,
    "one huge attribute": "<a href='/x' title='"
    + "a" * 1_000_000
    + "'>t</a><p>"
    + "word " * 99
    + "</p>",
    "many entities": "<p>" + "&amp;&#x41;&bogus;&#0;" * 50_000 + "</p>",
    "null bytes": "<html><body>" + "\x00" * 100_000 + "<p>after</p></body></html>",
}


@pytest.mark.parametrize("name", HOSTILE_PAGES)
def test_known_hostile_pages_are_extracted_in_bounded_time(name: str) -> None:
    html = HOSTILE_PAGES[name]
    start = time.perf_counter()
    page = extract_page(cap_nesting(html), "http://example.test/")
    assert time.perf_counter() - start < 10, name
    check_links(page.links)
