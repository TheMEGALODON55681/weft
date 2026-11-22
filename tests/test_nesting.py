"""The nesting cap. lexbor (selectolax's parser) takes time quadratic in how deep a document nests:
0.2 s at 10,000 levels, 1.3 s at 20,000, about 30 s at 100,000, and Weft parses a page three
times. The parser releases the GIL, so the event loop stays free, but the extraction thread cannot
be stopped, and there are four of them. A 500 KB page of nested `<div>` held one for two minutes.
`cap_nesting` cuts a document where it passes MAX_NESTING open elements, which no real page does
(browsers stop at 512)."""

import time
from collections.abc import Callable

import pytest

from fixture_site import ARTICLE_HTML, FixtureSite
from weft.config import Settings
from weft.extract.content import extract_page
from weft.extract.nesting import MAX_NESTING, cap_nesting
from weft.fetch.http import Fetcher
from weft.fetch.page import get_page

HEAD = "<html><head><title>Kept title</title></head><body><p>"
WORDS = "word " * 40


def deep(tag: str, n: int, close: str = "") -> str:
    return HEAD + WORDS + "</p>" + tag * n + close + "</body></html>"


@pytest.mark.parametrize(
    "html",
    [
        ARTICLE_HTML,
        "<html><body>" + "<div>x</div>" * 60_000 + "</body></html>",  # siblings, not nesting
        "<html><body>" + "<p>word " * 30_000 + "</body></html>",  # end tags are optional
        "<ul>" + "<li>item " * 30_000 + "</ul>",
        "<table>" + "<tr><td>cell " * 20_000 + "</table>",
        "<p>a<br>b" * 30_000,  # void elements never open
        "<svg>" + '<path d="M0 0"/>' * 30_000 + "</svg>",  # an SVG leaf closes itself
        "<div>" * (MAX_NESTING - 1) + "x" + "</div>" * (MAX_NESTING - 1),
        "<div>" * MAX_NESTING,  # exactly at the cap is allowed
        "</div></span></p>" * 10_000,  # end tags with nothing open change nothing
        "<DIV><Span></SPAN></div>" * 10_000,  # case does not matter
        "",
        "no tags at all",
    ],
)
def test_ordinary_documents_pass_through_unchanged(html: str) -> None:
    assert cap_nesting(html) == html


@pytest.mark.parametrize(
    ("unit", "name"),
    [
        ("<div>", "div"),
        ("<DIV>", "uppercase"),
        ("<span class=a>", "attributes"),
        ("<x-custom data-x='>'>", "custom element"),
        ("<div/>", "self-closed HTML element, which the parser nests"),
        ("<div></span>", "end tags that match nothing"),
        ("<a><b></a>", "end tags in the wrong order never get ahead of the opens"),
    ],
)
def test_a_document_past_the_cap_is_cut_at_the_first_tag_over_it(unit: str, name: str) -> None:
    html = "<html><body>" + unit * (MAX_NESTING + 500)
    out = cap_nesting(html)
    assert html.startswith(out), name
    assert abs(out.count(unit) - MAX_NESTING) <= 1, name  # cut within one unit of the cap
    assert cap_nesting(out) == out


def test_the_cut_is_exact_for_plain_divs() -> None:
    html = "<html><body>" + "<div>" * (MAX_NESTING + 10)
    # <html> and <body> are not counted; the (MAX_NESTING + 1)th <div> is where the cut falls.
    assert cap_nesting(html) == "<html><body>" + "<div>" * MAX_NESTING


def test_a_custom_limit() -> None:
    assert cap_nesting("<div>" * 5, limit=3) == "<div>" * 3
    assert cap_nesting("<div>" * 3, limit=3) == "<div>" * 3


def test_content_before_the_cut_survives_extraction() -> None:
    page = extract_page(cap_nesting(deep("<div>", MAX_NESTING * 20)), "http://a.test/")
    assert page.title == "Kept title"
    assert "word word" in page.markdown


def test_a_hostile_page_is_extracted_in_bounded_time() -> None:
    html = deep("<div>", 400_000)  # 2 MB: more than an hour of parsing without the cap
    start = time.perf_counter()
    page = extract_page(cap_nesting(html), "http://a.test/")
    assert time.perf_counter() - start < 5
    assert page.title == "Kept title"


def test_the_scan_is_linear_on_tags_that_never_end() -> None:
    # A regular expression that looks for the closing ">" of every "<a " is quadratic here.
    for html in ("<a " * 400_000, "<a" * 400_000, '<a b="' * 200_000, "<" * 1_000_000):
        start = time.perf_counter()
        cap_nesting(html)
        assert time.perf_counter() - start < 3


async def test_a_deeply_nested_page_comes_back_through_get_page(
    site: FixtureSite, fetch_with: Callable[..., Fetcher]
) -> None:
    settings = Settings(extract_timeout_s=5)
    start = time.perf_counter()
    page = await get_page(fetch_with(), settings, site.url("/deep"), render="never")
    assert time.perf_counter() - start < 5
    assert page.title == "Kept title"
