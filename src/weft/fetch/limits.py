"""Per-host politeness and failure isolation: request spacing, concurrency caps and a circuit
breaker. Hosts are keyed by name.

YAGNI note: state is kept per host forever. A crawl across millions of distinct hosts needs an
LRU here, as `RobotsCache` has.
"""

import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from weft.fetch.errors import FetchError


@dataclass(slots=True)
class _Host:
    sem: asyncio.Semaphore
    next_at: float = 0.0  # earliest start for the next request


class HostLimiter:
    """At most `per_host` requests in flight per host, `overall` in total, and starts to one host
    spaced by an interval."""

    def __init__(
        self, per_host: int, overall: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._overall = asyncio.Semaphore(overall)
        self._clock = clock
        self._hosts: defaultdict[str, _Host] = defaultdict(
            lambda: _Host(asyncio.Semaphore(per_host))
        )

    def reserve(self, host: str, interval_s: float, max_wait_s: float) -> float:
        """Claim the next start time for `host` and return how long to wait for it. A wait beyond
        `max_wait_s` is refused up front, and claims nothing."""
        state = self._hosts[host]
        now = self._clock()
        start = max(now, state.next_at)
        wait = start - now
        if wait > max_wait_s:
            raise FetchError(
                "rate_limited",
                f"{host} is already being fetched at its polite rate; try again in {wait:.0f}s",
            )
        state.next_at = start + interval_s
        return wait

    @asynccontextmanager
    async def slot(self, host: str, interval_s: float, max_wait_s: float) -> AsyncIterator[None]:
        # Host first, global last: a host that is slow or being made to wait must not sit on
        # global slots that other hosts could use.
        async with self._hosts[host].sem:
            wait = self.reserve(host, interval_s, max_wait_s)
            if wait > 0:
                await asyncio.sleep(wait)
            async with self._overall:
                yield


class CircuitBreaker:
    """Stops contacting a host after `threshold` failures in a row, for `cooldown_s`. Then one
    caller is let through as a probe: success closes the breaker, failure reopens it.

    There is no probe-in-flight flag. The caller that is let through pushes the deadline out by
    another cooldown, so a probe that dies without reporting (cancelled, timed out) costs one
    more cooldown instead of wedging the breaker.
    """

    def __init__(
        self, threshold: int, cooldown_s: float = 30.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_s
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def peek(self, host: str) -> None:
        """Raise if the breaker is open. Admits nothing, so it is safe to call before queuing."""
        if self._failures.get(host, 0) < self._threshold:
            return
        remaining = self._open_until[host] - self._clock()
        if remaining > 0:
            raise FetchError(
                "circuit_open",
                f"{host} failed {self._threshold} times in a row; "
                f"not trying again for {remaining:.0f}s",
            )

    def check(self, host: str) -> None:
        """Call right before sending, after any queuing: the breaker may have opened while the
        request waited. Once the cooldown is over, exactly one caller gets through as the probe."""
        self.peek(host)
        if self._failures.get(host, 0) >= self._threshold:
            self._open_until[host] = self._clock() + self._cooldown

    def record(self, host: str, ok: bool) -> None:
        if ok:
            self._failures.pop(host, None)
            self._open_until.pop(host, None)
            return
        count = self._failures[host] = self._failures.get(host, 0) + 1
        if count >= self._threshold:
            self._open_until[host] = self._clock() + self._cooldown
