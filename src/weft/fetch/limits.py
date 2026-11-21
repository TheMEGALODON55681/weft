"""Per-host politeness and failure isolation: request spacing, concurrency caps and a circuit
breaker. Hosts are keyed by name.

Both keep state for at most `max_hosts` hosts and drop the least recently used first. A host is
only dropped when forgetting it changes nothing: no request holds or waits for its slots, it is
owed no more spacing, and its breaker is not open. So the cap can be passed for a while when
more hosts than that are busy at once.
"""

import asyncio
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from itertools import islice

from weft.fetch.errors import FetchError

DEFAULT_MAX_HOSTS = 4096


@dataclass(slots=True)
class _Host:
    sem: asyncio.Semaphore
    next_at: float = 0.0  # earliest start for the next request
    users: int = 0  # callers inside slot(), waiting or running


class HostLimiter:
    """At most `per_host` requests in flight per host, `overall` in total, and starts to one host
    spaced by an interval."""

    def __init__(
        self,
        per_host: int,
        overall: int,
        clock: Callable[[], float] = time.monotonic,
        max_hosts: int = DEFAULT_MAX_HOSTS,
    ) -> None:
        self._per_host = per_host
        self._overall = asyncio.Semaphore(overall)
        self._clock = clock
        self._max_hosts = max_hosts
        self._hosts: OrderedDict[str, _Host] = OrderedDict()

    def __len__(self) -> int:
        return len(self._hosts)

    def _host(self, host: str) -> _Host:
        state = self._hosts.get(host)
        if state is not None:
            self._hosts.move_to_end(host)
            return state
        excess = len(self._hosts) + 1 - self._max_hosts
        if excess > 0:  # make room first, so the new entry cannot be the one dropped
            now = self._clock()
            idle = (h for h, s in self._hosts.items() if s.users == 0 and s.next_at <= now)
            for h in list(islice(idle, excess)):
                del self._hosts[h]
        state = self._hosts[host] = _Host(asyncio.Semaphore(self._per_host))
        return state

    def reserve(self, host: str, interval_s: float, max_wait_s: float) -> float:
        """Claim the next start time for `host` and return how long to wait for it. A wait beyond
        `max_wait_s` is refused up front, and claims nothing."""
        state = self._host(host)
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
        state = self._host(host)
        state.users += 1  # pins the entry: eviction skips hosts with users
        try:
            # Host first, global last: a host that is slow or being made to wait must not sit on
            # global slots that other hosts could use.
            async with state.sem:
                wait = self.reserve(host, interval_s, max_wait_s)
                if wait > 0:
                    await asyncio.sleep(wait)
                async with self._overall:
                    yield
        finally:
            state.users -= 1


class CircuitBreaker:
    """Stops contacting a host after `threshold` failures in a row, for `cooldown_s`. Then one
    caller is let through as a probe: success closes the breaker, failure reopens it.

    There is no probe-in-flight flag. The caller that is let through pushes the deadline out by
    another cooldown, so a probe that dies without reporting (cancelled, timed out) costs one
    more cooldown instead of wedging the breaker.

    Only hosts with recent failures are tracked. A success forgets the host.
    """

    def __init__(
        self,
        threshold: int,
        cooldown_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        max_hosts: int = DEFAULT_MAX_HOSTS,
    ) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_s
        self._clock = clock
        self._max_hosts = max_hosts
        self._failures: OrderedDict[str, int] = OrderedDict()
        self._open_until: dict[str, float] = {}

    def __len__(self) -> int:
        return len(self._failures)

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
        excess = len(self._failures) + (host not in self._failures) - self._max_hosts
        if excess > 0:  # make room first, so this host's count cannot be the one dropped
            now = self._clock()
            closed = (
                h for h in self._failures if h != host and self._open_until.get(h, 0.0) <= now
            )
            for h in list(islice(closed, excess)):
                del self._failures[h]
                self._open_until.pop(h, None)
        count = self._failures[host] = self._failures.get(host, 0) + 1
        self._failures.move_to_end(host)
        if count >= self._threshold:
            self._open_until[host] = self._clock() + self._cooldown
