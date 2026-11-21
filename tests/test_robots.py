import asyncio
import time
from collections.abc import Callable

import httpx
import pytest

from fixture_site import FixtureSite
from weft.fetch.errors import FetchError
from weft.fetch.http import Fetcher
from weft.fetch.robots import RobotsCache, parse_robots

# --- parsing and matching ------------------------------------------------------------------


def allowed(robots: str, path: str) -> bool:
    return parse_robots(robots).allows(path)


def test_empty_file_allows_everything() -> None:
    rules = parse_robots("")
    assert rules.allows("/anything")
    assert rules.crawl_delay is None


def test_disallow_is_a_prefix_match() -> None:
    robots = "User-agent: *\nDisallow: /private/"
    assert not allowed(robots, "/private/x")
    assert not allowed(robots, "/private/")
    assert allowed(robots, "/private")
    assert allowed(robots, "/public")


def test_empty_disallow_allows_everything() -> None:
    assert allowed("User-agent: *\nDisallow:", "/x")


@pytest.mark.parametrize("rule", ["Disallow: /", "Disallow: /*"])
def test_disallow_everything(rule: str) -> None:
    assert not allowed(f"User-agent: *\n{rule}", "/")
    assert not allowed(f"User-agent: *\n{rule}", "/a/b")


def test_the_longest_matching_rule_wins() -> None:
    robots = "User-agent: *\nDisallow: /private/\nAllow: /private/open"
    assert allowed(robots, "/private/open/page")
    assert not allowed(robots, "/private/closed")


def test_allow_wins_a_tie() -> None:
    assert allowed("User-agent: *\nDisallow: /a\nAllow: /a", "/a")
    assert allowed("User-agent: *\nAllow: /a\nDisallow: /a", "/a")


def test_order_does_not_matter_only_length() -> None:
    robots = "User-agent: *\nAllow: /p\nDisallow: /private/"
    assert not allowed(robots, "/private/x")


@pytest.mark.parametrize(
    ("pattern", "path", "blocked"),
    [
        ("/*.pdf$", "/files/a.pdf", True),
        ("/*.pdf$", "/files/a.pdf?download=1", False),  # $ anchors the end of path and query
        ("/*.pdf$", "/files/a.pdf.html", False),
        ("/*?", "/search?q=1", True),
        ("/*?", "/search", False),
        ("/a*b*c", "/a-b-c", True),
        ("/a*b*c", "/a-c-b", False),
        ("/exact$", "/exact", True),
        ("/exact$", "/exact/more", False),
        ("/*/draft", "/blog/draft", True),
        ("/search?q=", "/search?q=cats", True),
        ("/p", "/p", True),
        ("/*aa*aa", "/aa", False),  # the two segments cannot share characters
        ("/*aa*aa", "/aaaa", True),
    ],
)
def test_wildcards_and_end_anchor(pattern: str, path: str, blocked: bool) -> None:
    assert allowed(f"User-agent: *\nDisallow: {pattern}", path) is not blocked


def test_pathological_wildcards_stay_fast() -> None:
    # A hostile site controls this file; matching must not blow up on it.
    robots = "User-agent: *\nDisallow: /" + "a*" * 2000 + "b"
    start = time.perf_counter()
    assert allowed(robots, "/" + "a" * 8000)
    assert time.perf_counter() - start < 1


def test_a_group_naming_weft_beats_the_wildcard_group() -> None:
    # The wildcard rule is the longer match, so it would win if the groups were merged.
    robots = "User-agent: *\nDisallow: /private/\n\nUser-agent: weft\nAllow: /"
    assert allowed(robots, "/private/x")


def test_the_wildcard_group_applies_when_weft_is_not_named() -> None:
    robots = "User-agent: otherbot\nAllow: /\n\nUser-agent: *\nDisallow: /"
    assert not allowed(robots, "/x")


def test_agent_names_ignore_case() -> None:
    assert not allowed("User-agent: WEFT\nDisallow: /", "/x")


def test_one_group_can_name_several_agents() -> None:
    assert not allowed("User-agent: foo\nUser-agent: weft\nDisallow: /x", "/x")


def test_groups_for_the_same_agent_are_merged() -> None:
    robots = "User-agent: weft\nDisallow: /a\n\nUser-agent: weft\nDisallow: /b"
    assert not allowed(robots, "/a")
    assert not allowed(robots, "/b")


def test_rules_before_any_user_agent_are_ignored() -> None:
    assert allowed("Disallow: /x\nUser-agent: *\nDisallow: /y", "/x")


def test_comments_blank_lines_crlf_bom_and_odd_spacing_are_tolerated() -> None:
    robots = "﻿User-agent: *  # everyone\r\n\r\nDisallow :/x # no space\r\nDISALLOW:   /y\r\n"
    assert not allowed(robots, "/x")
    assert not allowed(robots, "/y")


def test_unknown_fields_are_ignored() -> None:
    robots = "Sitemap: https://example.test/s.xml\nUser-agent: *\nHost: example.test\nDisallow: /x"
    assert not allowed(robots, "/x")
    assert allowed(robots, "/y")


def test_lines_without_a_colon_are_ignored() -> None:
    assert allowed("User-agent: *\nthis is not a rule\nDisallow: /x", "/y")


def test_crawl_delay_is_read_from_the_matching_group() -> None:
    assert parse_robots("User-agent: *\nCrawl-delay: 2.5").crawl_delay == 2.5
    both = "User-agent: *\nCrawl-delay: 10\n\nUser-agent: weft\nCrawl-delay: 3"
    assert parse_robots(both).crawl_delay == 3


@pytest.mark.parametrize("value", ["abc", "-1", "nan", "inf", ""])
def test_a_bad_crawl_delay_is_ignored(value: str) -> None:
    assert parse_robots(f"User-agent: *\nCrawl-delay: {value}").crawl_delay is None


# --- the cache -----------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeFetch:
    """Stands in for the HTTP layer. Replies repeat the last one once the list runs out."""

    def __init__(self, *replies: tuple[int, bytes] | FetchError, delay: float = 0) -> None:
        self.calls: list[str] = []
        self._replies = list(replies)
        self._delay = delay

    async def __call__(self, url: str) -> tuple[int, bytes]:
        self.calls.append(url)
        if self._delay:
            await asyncio.sleep(self._delay)
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, FetchError):
            raise reply
        return reply


def cache(fetch: FakeFetch, clock: Clock | None = None, **kw: int) -> RobotsCache:
    return RobotsCache(fetch, ttl_s=3600, clock=clock or Clock(), **kw)


H = httpx.URL("http://h.test/page")


async def test_a_successful_read_is_parsed_and_asked_for_at_the_origin_root() -> None:
    fetch = FakeFetch((200, b"User-agent: *\nDisallow: /no"))
    rules = await cache(fetch).rules_for(httpx.URL("http://h.test:8080/deep/page?x=1"))
    assert not rules.allows("/no")
    assert fetch.calls == ["http://h.test:8080/robots.txt"]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
async def test_client_errors_mean_no_restrictions(status: int) -> None:
    rules = await cache(FakeFetch((status, b"Disallow: /"))).rules_for(H)
    assert rules.allows("/anything")


@pytest.mark.parametrize("status", [429, 500, 502, 503, 301])
async def test_server_errors_mean_assume_nothing_is_allowed(status: int) -> None:
    with pytest.raises(FetchError) as e:
        await cache(FakeFetch((status, b""))).rules_for(H)
    assert e.value.code == "robots_unavailable"


@pytest.mark.parametrize(
    "code", ["blocked_address", "circuit_open", "rate_limited", "timeout", "network_error"]
)
async def test_failures_of_the_read_pass_through_unchanged_and_are_not_cached(code: str) -> None:
    # A dead host is reported as a dead host, not as a robots.txt problem.
    fetch = FakeFetch(FetchError(code, "x"))
    c = cache(fetch)
    for _ in range(2):
        with pytest.raises(FetchError) as e:
            await c.rules_for(H)
        assert e.value.code == code
    assert len(fetch.calls) == 2


async def test_rules_are_cached_until_the_ttl_ends() -> None:
    clock, fetch = Clock(), FakeFetch((200, b"User-agent: *\nDisallow: /a"))
    c = cache(fetch, clock)
    await c.rules_for(H)
    clock.now += 3599
    await c.rules_for(H)
    assert len(fetch.calls) == 1
    clock.now += 2
    await c.rules_for(H)
    assert len(fetch.calls) == 2


async def test_an_unreachable_file_is_retried_after_a_minute_not_an_hour() -> None:
    clock = Clock()
    fetch = FakeFetch((503, b""), (200, b"User-agent: *\nDisallow: /a"))
    c = cache(fetch, clock)
    with pytest.raises(FetchError):
        await c.rules_for(H)
    clock.now += 30
    with pytest.raises(FetchError):
        await c.rules_for(H)
    assert len(fetch.calls) == 1  # still inside the negative cache
    clock.now += 31
    assert not (await c.rules_for(H)).allows("/a")
    assert len(fetch.calls) == 2


async def test_each_origin_has_its_own_entry() -> None:
    fetch = FakeFetch((200, b""))
    c = cache(fetch)
    for url in ["http://h.test/", "https://h.test/", "http://h.test:81/", "http://other.test/"]:
        await c.rules_for(httpx.URL(url))
    assert len(fetch.calls) == 4


async def test_concurrent_callers_share_one_fetch() -> None:
    fetch = FakeFetch((200, b""), delay=0.05)
    c = cache(fetch)
    await asyncio.gather(*(c.rules_for(H) for _ in range(5)))
    assert len(fetch.calls) == 1


async def test_the_cache_forgets_the_least_recently_used_host() -> None:
    fetch = FakeFetch((200, b""))
    c = cache(fetch, max_hosts=2)
    for host in ["a", "b", "a", "c"]:  # "b" is now the oldest
        await c.rules_for(httpx.URL(f"http://{host}.test/"))
    await c.rules_for(httpx.URL("http://a.test/"))  # still cached
    await c.rules_for(httpx.URL("http://b.test/"))  # evicted, fetched again
    assert len(fetch.calls) == 4


async def test_only_the_first_half_megabyte_is_read() -> None:
    body = b"User-agent: *\nDisallow: /early\n" + b"# pad\n" * 100_000 + b"Disallow: /late\n"
    rules = await cache(FakeFetch((200, body))).rules_for(H)
    assert not rules.allows("/early")
    assert rules.allows("/late")


# --- through the real Fetcher --------------------------------------------------------------


async def test_a_disallowed_path_is_refused_and_never_requested(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    with pytest.raises(FetchError) as e:
        await fetch_with().fetch(site.url("/private/secret"))
    assert e.value.code == "robots_disallowed"
    assert site.hits("/private/secret") == 0


async def test_allowed_paths_and_allow_overrides_are_fetched(
    site: FixtureSite, fetch_with: Callable[..., Fetcher]
) -> None:
    fetcher = fetch_with()
    assert (await fetcher.fetch(site.url("/article"))).status == 200
    assert (await fetcher.fetch(site.url("/private/open"))).status == 200


async def test_wildcard_rules_apply_to_real_fetches(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    with pytest.raises(FetchError) as e:
        await fetch_with().fetch(site.url("/report.pdf"))
    assert e.value.code == "robots_disallowed"
    assert site.hits("/report.pdf") == 0


async def test_robots_is_read_once_per_host_even_for_concurrent_fetches(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    fetcher = fetch_with()
    await asyncio.gather(*(fetcher.fetch(site.url("/ping")) for _ in range(4)))
    await fetcher.fetch(site.url("/article"))
    assert site.hits("/robots.txt") == 1


async def test_a_redirect_into_a_disallowed_path_is_refused(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    with pytest.raises(FetchError) as e:
        await fetch_with().fetch(site.url("/redirect-to-disallowed"))
    assert e.value.code == "robots_disallowed"
    assert site.hits("/private/secret") == 0


async def test_robots_txt_itself_can_always_be_fetched(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site(robots="User-agent: *\nDisallow: /")
    assert (await fetch_with().fetch(site.url("/robots.txt"))).status == 200


async def test_a_site_closed_to_weft_by_name_is_refused(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site(robots="User-agent: weft\nDisallow: /")
    with pytest.raises(FetchError) as e:
        await fetch_with().fetch(site.url("/article"))
    assert e.value.code == "robots_disallowed"


@pytest.mark.parametrize("robots", [None, 403])
async def test_a_missing_or_forbidden_robots_file_allows_fetching(
    robots: int | None, make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site(robots=robots)
    assert (await fetch_with().fetch(site.url("/article"))).status == 200


async def test_a_broken_robots_file_blocks_fetching_and_is_not_hammered(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site(robots=500)
    fetcher = fetch_with()
    for _ in range(2):
        with pytest.raises(FetchError) as e:
            await fetcher.fetch(site.url("/article"))
        assert e.value.code == "robots_unavailable"
    assert site.hits("/article") == 0
    assert site.hits("/robots.txt") == 1


# --- Sitemap lines and the cache-only lookup ---------------------------------------------------


def test_sitemap_lines_are_collected_from_anywhere_in_the_file() -> None:
    rules = parse_robots(
        "Sitemap: https://h.test/a.xml\n"
        "User-agent: other\nDisallow: /\nSitemap: https://h.test/b.xml\n"
        "User-agent: *\nDisallow: /x\nsitemap:https://h.test/a.xml\n"
    )
    assert rules.sitemaps == ("https://h.test/a.xml", "https://h.test/b.xml")
    assert not rules.allows("/x")  # a Sitemap line does not end or start a group


def test_sitemap_lines_are_capped() -> None:
    rules = parse_robots("".join(f"Sitemap: https://h.test/{i}.xml\n" for i in range(500)))
    assert len(rules.sitemaps) == 50


async def test_cached_never_reads_the_file() -> None:
    clock = Clock()
    fetch = FakeFetch((200, b"User-agent: *\nDisallow: /no"))
    robots = cache(fetch, clock)
    assert robots.cached(H) is None
    assert fetch.calls == []
    await robots.rules_for(H)
    rules = robots.cached(H)
    assert rules is not None and not rules.allows("/no")
    clock.now += 3601
    assert robots.cached(H) is None  # expired rules are not handed out
