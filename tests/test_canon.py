import pytest

from weft.frontier.canon import canonicalize, pattern, repeats_segments, site_key, url_matches


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTP://Example.COM/a", "http://example.com/a"),
        ("http://example.com", "http://example.com/"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("http://example.com./a", "http://example.com/a"),
        ("http://example.com/a#frag", "http://example.com/a"),
        ("http://example.com/a/./b/../c", "http://example.com/a/c"),
        ("http://example.com/../../a", "http://example.com/a"),
        ("http://example.com/a/b/..", "http://example.com/a/"),
        ("http://example.com/%7Euser/%2f", "http://example.com/~user/%2F"),
        ("http://example.com/a b/é", "http://example.com/a%20b/%C3%A9"),
        ("http://example.com/?b=2&a=1", "http://example.com/?a=1&b=2"),
        ("http://example.com/?utm_source=x&id=3&gclid=y&UTM_medium=z", "http://example.com/?id=3"),
        ("http://example.com/?utm_source=x", "http://example.com/"),
        ("http://example.com/?a=%41", "http://example.com/?a=A"),
        ("http://bücher.example/", "http://xn--bcher-kva.example/"),
        ("http://[::1]:8080/a", "http://[::1]:8080/a"),
    ],
)
def test_canonical_forms(raw: str, expected: str) -> None:
    assert canonicalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://example.com/A",  # path case is the server's business
        "http://example.com/a/",  # so is a trailing slash
        "http://www.example.com/",
        "http://example.com/a%2Fb",  # an encoded slash is not a slash
        "http://example.com/?a=1&a=2",  # repeated keys keep their order
    ],
)
def test_what_canonicalisation_leaves_alone(raw: str) -> None:
    assert canonicalize(raw) == raw


def test_canonicalisation_is_idempotent() -> None:
    once = canonicalize("HTTP://Ex.com:80/a/../%7eb?z=1&utm_x=2&a=%3d#f")
    assert canonicalize(once) == once


@pytest.mark.parametrize(
    "raw",
    ["ftp://example.com/", "mailto:a@b.c", "/relative", "http://", "http://u:p@example.com/"],
)
def test_uncrawlable_urls_raise(raw: str) -> None:
    with pytest.raises(ValueError):
        canonicalize(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "http://日本語..jp/",  # IDNA cannot encode an empty label
        "http://ü..com/",
        "http://" + "ü" * 70 + ".com/",  # or a label of more than 63 characters
        "http://ex\x00ample.com/",
        "http://a b.com/",
        "http://exa%6Dple.com/",  # a percent escape is not part of a name
        "http://a..b/",
        "http://.a/",
        "http://./",  # the host is empty once the trailing dot goes
        "http://.../",
        "http://[fe80::1%25eth0]/",  # a zone id
        "http://[::g]/",
    ],
)
def test_hosts_that_cannot_be_names_are_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        canonicalize(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://my_host.local/", "http://my_host.local/"),
        ("http://127.0.0.1:8080/", "http://127.0.0.1:8080/"),
        ("http://LOCALHOST./", "http://localhost/"),
        ("http://[FE80::1]/", "http://[fe80::1]/"),
        ("http://例え.jp./", "http://xn--r8jz45g.jp/"),
        ("http://xn--bcher-kva.example/", "http://xn--bcher-kva.example/"),
    ],
)
def test_ordinary_hosts_survive_the_host_check(raw: str, expected: str) -> None:
    assert canonicalize(raw) == expected


def test_site_key_ignores_www_and_case() -> None:
    assert site_key("WWW.Example.com.") == site_key("example.com") == "example.com"
    assert site_key("docs.example.com") != site_key("example.com")


def test_pattern_folds_digits_and_query_values() -> None:
    assert pattern("http://a.test/cal/2031/05?d=3") == pattern("http://a.test/cal/1999/12?d=30")
    assert pattern("http://a.test/cal/2031?d=3") != pattern("http://a.test/cal/2031?m=3")
    assert pattern("http://a.test/blog/a") != pattern("http://a.test/blog/b")


def test_repeated_segments_are_spotted() -> None:
    assert repeats_segments("http://a.test/x/y/x/y/x/y")
    assert not repeats_segments("http://a.test/x/y/x/y")
    assert not repeats_segments("http://a.test/2026/11/21")


@pytest.mark.parametrize(
    ("url", "include", "exclude", "expected"),
    [
        ("http://a.test/docs/x", [], [], True),
        ("http://a.test/docs/x", ["*/docs/*"], [], True),
        ("http://a.test/blog/x", ["*/docs/*"], [], False),
        ("http://a.test/docs/x", ["*/blog/*", "*/docs/*"], [], True),  # any one include will do
        ("http://a.test/docs/x", [], ["*/docs/*"], False),
        ("http://a.test/docs/old", ["*/docs/*"], ["*/old"], False),  # an exclude beats an include
        ("http://a.test/docs/x", [], ["*/blog/*", "*/old"], True),
        ("http://a.test/DOCS/x", ["*/docs/*"], [], False),  # patterns are case-sensitive
        ("http://a.test/docs/x?v=2", ["*/docs/x"], [], False),  # over the whole URL, query too
    ],
)
def test_include_and_exclude_globs_decide_whether_a_url_is_wanted(
    url: str, include: list[str], exclude: list[str], expected: bool
) -> None:
    assert url_matches(url, include, exclude) is expected
