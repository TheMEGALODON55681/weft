import pytest

from fixture_site import ARTICLE_HTML
from weft.extract.content import extract_page
from weft.extract.links import extract_links

BASE = "https://acme.test/pricing/"


def test_links_are_absolute_deduped_and_fragment_free() -> None:
    links = extract_links(ARTICLE_HTML, BASE)
    urls = [link.url for link in links]
    assert urls == [
        "https://acme.test/",
        "https://acme.test/docs",
        "https://acme.test/pricing/enterprise",
        "https://other.test/review",
    ]
    assert links[2].text == "enterprise page"  # first occurrence keeps its text


def test_non_navigational_schemes_are_dropped() -> None:
    html = (
        '<a href="mailto:a@b.c">m</a><a href="tel:123">t</a><a href="javascript:x()">j</a>'
        '<a href="data:text/html,hi">d</a><a href="ftp://h.test/f">f</a><a href="#x">h</a>'
        '<a href="  /ok  ">ok</a><a>no href</a><a href="">empty</a>'
    )
    assert [link.url for link in extract_links(html, BASE)] == ["https://acme.test/ok"]


def test_nofollow_is_flagged() -> None:
    links = extract_links(ARTICLE_HTML, BASE)
    assert [link.nofollow for link in links] == [False, False, False, True]


def test_base_href_changes_resolution() -> None:
    html = '<base href="/other/"><a href="x">x</a>'
    assert extract_links(html, BASE)[0].url == "https://acme.test/other/x"


def test_anchor_text_is_whitespace_normalised_and_bounded() -> None:
    html = f'<a href="/a">  two \n  words </a><a href="/b">{"w" * 500}</a>'
    a, b = extract_links(html, BASE)
    assert a.text == "two words"
    assert len(b.text) == 200


@pytest.mark.parametrize("html", ["", "<<<>>>", "<a href='http://[bad'>x</a>", "\x00\x01"])
def test_links_survive_garbage(html: str) -> None:
    assert isinstance(extract_links(html, BASE), list)


def test_article_becomes_clean_markdown_with_metadata() -> None:
    page = extract_page(ARTICLE_HTML, "https://acme.test/article")
    assert page.title == "Pricing and plans"
    assert page.markdown.startswith("# Pricing and plans")
    assert "## Team plan" in page.markdown
    assert "- Audit logs" in page.markdown
    assert "Copyright Acme" not in page.markdown  # footer stripped
    assert page.metadata.description == "Plans for every team size."
    assert page.metadata.author == "Acme Docs"
    assert page.metadata.site_name == "Acme"
    assert page.metadata.language == "en"
    assert page.metadata.canonical_url == "https://acme.test/pricing"
    assert len(page.links) == 4


def test_js_shell_yields_thin_markdown_but_keeps_title_and_links() -> None:
    html = (
        "<html><head><title>App</title></head><body><nav><a href='/x'>x</a></nav>"
        '<div id=root></div><script src="/app.js"></script></body></html>'
    )
    page = extract_page(html, "https://acme.test/")
    assert len(page.markdown.split()) < 5  # thin, which is what the render policy keys on
    assert page.title == "App"
    assert [link.url for link in page.links] == ["https://acme.test/x"]


@pytest.mark.parametrize("html", ["", "   ", "plain text, no markup", "<html", "\x00" * 100])
def test_extraction_survives_garbage(html: str) -> None:
    page = extract_page(html, "https://acme.test/")
    assert isinstance(page.markdown, str)


def test_a_crashing_parser_degrades_to_empty_content(monkeypatch: pytest.MonkeyPatch) -> None:
    import trafilatura

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(trafilatura, "extract", boom)
    html = "<html><head><title>Still here</title></head><body><a href='/x'>x</a></body></html>"
    page = extract_page(html, "https://acme.test/")
    assert page.markdown == ""
    assert page.title == "Still here"
    assert len(page.links) == 1


def test_anchor_text_upgrades_when_an_image_link_came_first() -> None:
    html = '<a href="/home"><img src="logo.png"></a><a href="/home">Home</a>'
    [link] = extract_links(html, BASE)
    assert link.text == "Home"
