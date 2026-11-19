import time
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from fixture_site import FixtureSite
from weft.fetch.errors import FetchError
from weft.fetch.http import Fetcher
from weft.fetch.retry import backoff_delay, parse_retry_after

# --- backoff and Retry-After ---------------------------------------------------------------


@pytest.mark.parametrize("attempt", [0, 1, 2, 3])
def test_backoff_doubles_and_stays_within_its_jitter_band(attempt: int) -> None:
    ceiling = 0.5 * 2**attempt
    for _ in range(200):
        assert ceiling / 2 <= backoff_delay(attempt, 0.5) <= ceiling


def test_backoff_is_jittered() -> None:
    assert len({backoff_delay(2, 0.5) for _ in range(50)}) > 1


def test_a_zero_base_means_no_waiting() -> None:
    assert backoff_delay(3, 0) == 0


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("120", 120.0),
        ("0", 0.0),
        (" 7 ", 7.0),
        ("1.5", 1.5),
        ("-5", 0.0),
        ("soon", 0.0),
        ("", 0.0),
        (None, 0.0),
        ("nan", 0.0),
        ("inf", 0.0),
    ],
)
def test_retry_after_in_seconds(value: str | None, seconds: float) -> None:
    assert parse_retry_after(value) == seconds


def test_retry_after_as_a_date() -> None:
    now = datetime(2015, 10, 21, 7, 27, 0, tzinfo=UTC)
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now) == 60
    assert parse_retry_after("Wed, 21 Oct 2015 07:26:00 GMT", now) == 0  # already past


# --- through the real Fetcher --------------------------------------------------------------
# Backoff is 10 ms in the suite (conftest), so retries cost almost nothing.


async def test_a_503_is_retried_until_it_clears(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    r = await fetch_with().fetch(site.url("/flaky/a/2"))
    assert r.status == 200
    assert site.hits("/flaky/a/2") == 3


async def test_429_is_retried_too(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    r = await fetch_with().fetch(site.url("/flaky/b/1?s=429"))
    assert r.status == 200
    assert site.hits("/flaky/b/1") == 2


async def test_when_retries_run_out_the_last_response_is_returned(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    r = await fetch_with(max_retries=2).fetch(site.url("/flaky/c/99"))
    assert r.status == 503  # an error status is a result, as before
    assert site.hits("/flaky/c/99") == 3


async def test_zero_retries_means_one_attempt(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    r = await fetch_with(max_retries=0).fetch(site.url("/flaky/d/99"))
    assert r.status == 503
    assert site.hits("/flaky/d/99") == 1


@pytest.mark.parametrize("path", ["/status/404", "/status/500"])
async def test_other_error_statuses_are_not_retried(
    path: str, make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    await fetch_with().fetch(site.url(path))
    assert site.hits(path) == 1


async def test_dropped_connections_are_retried(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    r = await fetch_with().fetch(site.url("/reset/e/2"))
    assert r.status == 200
    assert site.hits("/reset/e/2") == 3


async def test_a_connection_that_never_recovers_ends_as_a_network_error(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    with pytest.raises(FetchError) as e:
        await fetch_with(max_retries=2).fetch(site.url("/reset/f/99"))
    assert e.value.code == "network_error"
    assert site.hits("/reset/f/99") == 3


async def test_attempts_are_separated_by_a_backoff(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    fetcher = fetch_with(retry_backoff_s=0.2)  # built first: constructing a client takes ~0.5 s
    start = time.perf_counter()
    await fetcher.fetch(site.url("/flaky/g/2"))
    assert time.perf_counter() - start >= 0.28  # at least 0.1 + 0.2, the low end of the jitter


async def test_retry_after_sets_the_wait(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    fetcher = fetch_with()
    start = time.perf_counter()
    r = await fetcher.fetch(site.url("/flaky/h/1?ra=1"))
    assert r.status == 200
    assert time.perf_counter() - start >= 0.9


async def test_a_retry_after_beyond_the_time_budget_is_not_waited_for(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    fetcher = fetch_with(total_timeout_s=3)
    start = time.perf_counter()
    r = await fetcher.fetch(site.url("/flaky/i/1?ra=999"))
    assert r.status == 503
    assert time.perf_counter() - start < 1
    assert site.hits("/flaky/i/1") == 1


async def test_retries_obey_the_host_interval(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    await fetch_with(host_min_interval_s=0.3).fetch(site.url("/flaky/j/1"))
    first, second = site.times("/flaky/j/1")
    assert second - first >= 0.24


async def test_retries_stop_when_the_breaker_opens(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    fetcher = fetch_with(max_retries=5, breaker_threshold=2)
    with pytest.raises(FetchError) as e:
        await fetcher.fetch(site.url("/flaky/k/99"))
    assert e.value.code == "circuit_open"
    assert site.hits("/flaky/k/99") == 2


async def test_robots_txt_reads_are_retried_as_well(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site(robots=503)
    with pytest.raises(FetchError) as e:
        await fetch_with(max_retries=2).fetch(site.url("/ping"))
    assert e.value.code == "robots_unavailable"
    assert site.hits("/robots.txt") == 3
    assert site.hits("/ping") == 0
