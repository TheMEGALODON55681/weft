import asyncio
import time
from collections.abc import Callable

import pytest

from fixture_site import FixtureSite
from weft.fetch.errors import FetchError
from weft.fetch.http import Fetcher
from weft.fetch.limits import CircuitBreaker, HostLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


# --- spacing -------------------------------------------------------------------------------


def test_requests_to_one_host_are_spaced_by_the_interval() -> None:
    clock = Clock()
    limiter = HostLimiter(per_host=2, overall=4, clock=clock)
    assert limiter.reserve("a", 1.0, 30) == 0
    assert limiter.reserve("a", 1.0, 30) == 1.0
    assert limiter.reserve("a", 1.0, 30) == 2.0
    clock.now += 2.5  # the third slot is at +2.0, so the next free one is at +3.0
    assert limiter.reserve("a", 1.0, 30) == pytest.approx(0.5)


def test_hosts_do_not_share_an_interval() -> None:
    limiter = HostLimiter(per_host=2, overall=4, clock=Clock())
    limiter.reserve("a", 5.0, 30)
    assert limiter.reserve("b", 5.0, 30) == 0


def test_an_idle_host_starts_fresh() -> None:
    clock = Clock()
    limiter = HostLimiter(per_host=2, overall=4, clock=clock)
    limiter.reserve("a", 1.0, 30)
    clock.now += 60
    assert limiter.reserve("a", 1.0, 30) == 0


def test_a_zero_interval_never_waits() -> None:
    limiter = HostLimiter(per_host=2, overall=4, clock=Clock())
    assert [limiter.reserve("a", 0, 30) for _ in range(3)] == [0, 0, 0]


def test_a_wait_beyond_the_budget_is_refused_and_takes_no_slot() -> None:
    limiter = HostLimiter(per_host=2, overall=4, clock=Clock())
    limiter.reserve("a", 1.0, 1.5)
    limiter.reserve("a", 1.0, 1.5)
    with pytest.raises(FetchError) as e:
        limiter.reserve("a", 1.0, 1.5)  # would wait 2.0
    assert e.value.code == "rate_limited"
    assert limiter.reserve("a", 1.0, 5) == 2.0  # the refused call left nothing behind


# --- circuit breaker -----------------------------------------------------------------------


def breaker(clock: Clock) -> CircuitBreaker:
    return CircuitBreaker(threshold=3, cooldown_s=30, clock=clock)


def fail(b: CircuitBreaker, host: str, times: int) -> None:
    for _ in range(times):
        b.check(host)
        b.record(host, ok=False)


def test_the_breaker_opens_after_consecutive_failures() -> None:
    b = breaker(Clock())
    fail(b, "a", 2)
    b.check("a")  # two failures: still closed
    b.record("a", ok=False)
    with pytest.raises(FetchError) as e:
        b.check("a")
    assert e.value.code == "circuit_open"


def test_a_success_resets_the_count() -> None:
    b = breaker(Clock())
    fail(b, "a", 2)
    b.record("a", ok=True)
    fail(b, "a", 2)
    b.check("a")


def test_one_open_host_leaves_the_others_alone() -> None:
    b = breaker(Clock())
    fail(b, "a", 3)
    b.check("b")


def test_after_the_cooldown_one_probe_goes_through() -> None:
    clock = Clock()
    b = breaker(clock)
    fail(b, "a", 3)
    clock.now += 31
    b.check("a")  # the probe
    with pytest.raises(FetchError):
        b.check("a")  # everyone else still waits while it is out


def test_a_successful_probe_closes_the_breaker() -> None:
    clock = Clock()
    b = breaker(clock)
    fail(b, "a", 3)
    clock.now += 31
    b.check("a")
    b.record("a", ok=True)
    b.check("a")
    b.check("a")


def test_a_failed_probe_reopens_for_a_full_cooldown() -> None:
    clock = Clock()
    b = breaker(clock)
    fail(b, "a", 3)
    clock.now += 31
    b.check("a")
    b.record("a", ok=False)
    clock.now += 29
    with pytest.raises(FetchError):
        b.check("a")
    clock.now += 2
    b.check("a")


def test_a_probe_that_never_reports_does_not_wedge_the_breaker() -> None:
    clock = Clock()
    b = breaker(clock)
    fail(b, "a", 3)
    clock.now += 31
    b.check("a")  # the probe is cancelled and never calls record()
    clock.now += 31
    b.check("a")  # the next cooldown admits another


def test_peek_fails_fast_while_open_and_admits_nothing() -> None:
    clock = Clock()
    b = breaker(clock)
    fail(b, "a", 3)
    with pytest.raises(FetchError) as e:
        b.peek("a")
    assert e.value.code == "circuit_open"
    clock.now += 31
    b.peek("a")  # the cooldown is over: looking does not use up the probe
    b.peek("a")
    b.check("a")
    with pytest.raises(FetchError):
        b.check("a")


def test_the_error_says_when_to_come_back() -> None:
    clock = Clock()
    b = breaker(clock)
    fail(b, "a", 3)
    clock.now += 10
    with pytest.raises(FetchError, match="20s"):
        b.check("a")


# --- through the real Fetcher --------------------------------------------------------------


async def test_requests_to_one_host_start_one_interval_apart(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    fetcher = fetch_with(host_min_interval_s=0.3, host_concurrency=3)
    await asyncio.gather(*(fetcher.fetch(site.url("/ping")) for _ in range(3)))
    t = sorted(site.times("/ping"))
    assert t[1] - t[0] >= 0.24  # a little under 0.3: asyncio timers can fire early on Windows
    assert t[2] - t[1] >= 0.24


async def test_reading_robots_txt_does_not_cost_the_first_page_an_interval(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    await fetch_with(host_min_interval_s=1.0).fetch(site.url("/ping"))
    assert site.times("/ping")[0] - site.times("/robots.txt")[0] < 0.5


async def test_a_host_runs_at_most_host_concurrency_requests_at_once(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    fetcher = fetch_with(host_concurrency=2)
    await asyncio.gather(*(fetcher.fetch(site.url("/hold")) for _ in range(6)))
    assert site.peak == 2


async def test_the_global_cap_is_shared_by_all_hosts() -> None:
    limiter = HostLimiter(per_host=5, overall=2)
    running, peak = 0, 0

    async def work(host: str) -> None:
        nonlocal running, peak
        async with limiter.slot(host, 0, 30):
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1

    await asyncio.gather(*(work(h) for h in ["a", "b", "c"] * 3))
    assert peak == 2  # nine tasks on three hosts, each host allowed five


async def test_the_global_cap_is_applied_to_real_fetches(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    fetcher = fetch_with(host_concurrency=5, global_concurrency=2)
    await asyncio.gather(*(fetcher.fetch(site.url("/hold")) for _ in range(6)))
    assert site.peak == 2


async def test_a_wait_longer_than_the_time_budget_fails_fast(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    fetcher = fetch_with(host_min_interval_s=5, total_timeout_s=2)
    await fetcher.fetch(site.url("/ping"))
    start = time.perf_counter()
    with pytest.raises(FetchError) as e:
        await fetcher.fetch(site.url("/ping"))
    assert e.value.code == "rate_limited"
    assert time.perf_counter() - start < 1
    assert site.hits("/ping") == 1


async def test_crawl_delay_from_robots_is_honoured(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site(robots="User-agent: *\nCrawl-delay: 0.4")
    fetcher = fetch_with()
    await fetcher.fetch(site.url("/ping"))
    await fetcher.fetch(site.url("/ping"))
    first, second = site.times("/ping")
    assert second - first >= 0.34


async def test_a_crawl_delay_beyond_the_time_budget_fails_fast(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site(robots="User-agent: *\nCrawl-delay: 100")
    fetcher = fetch_with(total_timeout_s=2)
    await fetcher.fetch(site.url("/ping"))
    with pytest.raises(FetchError) as e:
        await fetcher.fetch(site.url("/ping"))
    assert e.value.code == "rate_limited"


async def test_a_failing_host_trips_the_breaker_and_stops_being_contacted(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    fetcher = fetch_with(breaker_threshold=3, max_retries=0)
    for _ in range(3):
        assert (await fetcher.fetch(site.url("/flaky/k/999"))).status == 503
    with pytest.raises(FetchError) as e:
        await fetcher.fetch(site.url("/flaky/k/999"))
    assert e.value.code == "circuit_open"
    assert site.hits("/flaky/k/999") == 3


async def test_requests_already_queued_when_the_breaker_opens_are_not_sent(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    fetcher = fetch_with(breaker_threshold=1, host_concurrency=1, max_retries=0)
    results = await asyncio.gather(
        *(fetcher.fetch(site.url("/flaky/q/99")) for _ in range(3)), return_exceptions=True
    )
    # All three passed the breaker while it was closed. The first one's 503 opened it while the
    # others were still waiting for the host's single slot.
    assert site.hits("/flaky/q/99") == 1
    errors = [r for r in results if isinstance(r, FetchError)]
    assert [e.code for e in errors] == ["circuit_open", "circuit_open"]


async def test_an_open_breaker_refuses_at_once_instead_of_queuing(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    fetcher = fetch_with(breaker_threshold=1, max_retries=0, host_min_interval_s=3)
    await fetcher.fetch(site.url("/flaky/f/99"))  # 503: opens the breaker
    start = time.perf_counter()
    with pytest.raises(FetchError) as e:
        await fetcher.fetch(site.url("/ping"))  # would otherwise wait ~3 s for its slot
    assert e.value.code == "circuit_open"
    assert time.perf_counter() - start < 1


async def test_connection_failures_count_towards_the_breaker(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    fetcher = fetch_with(breaker_threshold=2, max_retries=0)
    for _ in range(2):
        with pytest.raises(FetchError) as e:
            await fetcher.fetch(site.url("/reset/k/999"))
        assert e.value.code == "network_error"
    with pytest.raises(FetchError) as e:
        await fetcher.fetch(site.url("/reset/k/999"))
    assert e.value.code == "circuit_open"


async def test_the_breaker_is_per_host(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    fetcher = fetch_with(breaker_threshold=1, max_retries=0)
    await fetcher.fetch(site.url("/flaky/k/999"))  # 503: trips the breaker for 127.0.0.1
    with pytest.raises(FetchError):
        await fetcher.fetch(site.url("/ping"))
    assert (await fetcher.fetch(site.url("/ping").replace("127.0.0.1", "localhost"))).status == 200


async def test_an_ordinary_error_status_does_not_trip_the_breaker(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    fetcher = fetch_with(breaker_threshold=2, max_retries=0)
    for _ in range(4):
        assert (await fetcher.fetch(site.url("/status/500"))).status == 500


async def test_refused_addresses_never_trip_the_breaker(
    make_site: Callable[..., FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = make_site()
    fetcher = fetch_with(allow_loopback=False, breaker_threshold=1, max_retries=0)
    for _ in range(3):
        with pytest.raises(FetchError) as e:
            await fetcher.fetch(site.url("/ping"))
        assert e.value.code == "blocked_address"
