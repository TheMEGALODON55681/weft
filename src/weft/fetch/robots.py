"""robots.txt: parsing, matching per RFC 9309, and a per-origin cache.

The stdlib parser is not used. It matches rules in file order, not by length, and has no `*` or
`$`, so it gets real-world files wrong.
"""

import asyncio
import math
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from weft.fetch.errors import FetchError

ROBOTS_AGENT = "weft"
MAX_ROBOTS_BYTES = 512_000  # RFC 9309 asks crawlers to read at least 500 KiB
_UNREACHABLE_TTL_S = 60.0
MAX_SITEMAP_LINES = 50

Fetch = Callable[[str], Awaitable[tuple[int, bytes]]]

# RFC 9309: a line ends at CR, LF or CRLF. str.splitlines() also breaks at \x0b, \x0c, \x1c to
# \x1e, \x85 and the Unicode line and paragraph separators, which would cut a rule short.
_LINE_END = re.compile(r"\r\n|\r|\n")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
# An escape, or a character outside ASCII. A `%` not followed by two hex digits is no escape.
_ESCAPE_OR_NON_ASCII = re.compile(r"%([0-9A-Fa-f]{2})|[^\x00-\x7f]")


def _normalise_one(m: re.Match[str]) -> str:
    if m[1] is None:
        return "".join(f"%{b:02X}" for b in m[0].encode("utf-8", "surrogatepass"))
    return ch if (ch := chr(int(m[1], 16))) in _UNRESERVED else m[0].upper()


def normalise(text: str) -> str:
    """RFC 9309 section 2.2.2: one spelling for a rule and for a request path, so the two compare.
    Non-ASCII characters become UTF-8 escapes, hex digits are upper case, and an escape of an
    unreserved character is decoded. An escape of a reserved character stays one: `%2F` is not a
    slash and `%2A` is not a wildcard. Each escape is read once, so `%2541` stays `%2541`."""
    return _ESCAPE_OR_NON_ASCII.sub(_normalise_one, text)


def _matches(pattern: str, path: str) -> bool:
    """`*` matches any run of characters and a trailing `$` anchors the end. Plain string search,
    no regex: a hostile site controls the pattern, and chained `.*` can backtrack for ages."""
    anchored = pattern.endswith("$")
    parts = pattern.removesuffix("$").split("*")
    if not path.startswith(parts[0]):
        return False
    pos = len(parts[0])
    if len(parts) == 1:
        return not anchored or pos == len(path)
    for part in parts[1:-1]:
        i = path.find(part, pos)
        if i < 0:
            return False
        pos = i + len(part)
    last = parts[-1]
    if anchored:
        return len(path) - len(last) >= pos and path.endswith(last)
    return path.find(last, pos) >= 0


@dataclass(frozen=True, slots=True)
class Rules:
    rules: tuple[tuple[bool, str], ...] = ()  # (allow, pattern)
    crawl_delay: float | None = None
    sitemaps: tuple[str, ...] = ()  # Sitemap: lines, which belong to no group

    def allows(self, path: str) -> bool:
        """`path` is the request target: path plus query. The longest matching rule wins and
        Allow wins a tie. No match means allowed."""
        path = normalise(path)
        verdict, best = True, -1
        for allow, pattern in self.rules:
            n = len(pattern)
            if (n > best or (n == best and allow)) and _matches(pattern, path):
                verdict, best = allow, n
        return verdict


@dataclass(slots=True)
class _Group:
    agents: set[str] = field(default_factory=set)
    rules: list[tuple[bool, str]] = field(default_factory=list)
    delay: float | None = None


def _delay(value: str) -> float | None:
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def parse_robots(text: str) -> Rules:
    """The rules that apply to Weft: its own groups if the file names it, else the `*` groups."""
    groups: list[_Group] = []
    sitemaps: list[str] = []
    reading_agents = False
    for line in _LINE_END.split(text.lstrip("\N{BYTE ORDER MARK}")):
        name, colon, value = line.split("#", 1)[0].partition(":")
        if not colon:
            continue
        name, value = name.strip().lower(), value.strip()
        if name == "sitemap":
            if value and len(sitemaps) < MAX_SITEMAP_LINES:
                sitemaps.append(value)
        elif name == "user-agent":
            if not reading_agents:
                groups.append(_Group())
            groups[-1].agents.add(value.lower())
            reading_agents = True
        elif name in ("allow", "disallow", "crawl-delay") and groups:
            reading_agents = False
            group = groups[-1]
            if name == "crawl-delay":
                group.delay = group.delay if group.delay is not None else _delay(value)
            elif value:  # an empty Allow or Disallow says nothing
                group.rules.append((name == "allow", normalise(value)))
    named = [g for g in groups if ROBOTS_AGENT in g.agents]
    mine = named or [g for g in groups if "*" in g.agents]
    return Rules(
        tuple(rule for g in mine for rule in g.rules),
        next((g.delay for g in mine if g.delay is not None), None),
        tuple(dict.fromkeys(sitemaps)),
    )


def _origin(url: httpx.URL) -> str:
    return f"{url.scheme}://{url.netloc.decode()}"


@dataclass(slots=True)
class _Entry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    rules: Rules | None = None  # None: the file could not be read
    expires: float = -math.inf


class RobotsCache:
    """Rules per origin, read through `fetch`, which returns (status, body) for a URL.

    Status handling follows RFC 9309: 2xx is parsed, other 4xx means no restrictions, and
    anything else (5xx, 429, a redirect that goes nowhere) means assume the site is off limits,
    and look again after a minute.
    """

    def __init__(
        self,
        fetch: Fetch,
        ttl_s: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
        max_hosts: int = 512,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl_s
        self._clock = clock
        self._max_hosts = max_hosts
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def cached(self, url: httpx.URL) -> Rules | None:
        """The origin's rules if they are already known and current. Never reads the file."""
        entry = self._entries.get(_origin(url))
        return entry.rules if entry is not None and self._clock() < entry.expires else None

    async def rules_for(self, url: httpx.URL) -> Rules:
        origin = _origin(url)
        entry = self._entries.get(origin)
        if entry is None:
            entry = self._entries[origin] = _Entry()
            if len(self._entries) > self._max_hosts:
                self._entries.popitem(last=False)
        else:
            self._entries.move_to_end(origin)
        if self._clock() >= entry.expires:
            # One lock per origin: callers for the same host share a single read, and a slow
            # robots.txt on one host does not hold up the others.
            async with entry.lock:
                if self._clock() >= entry.expires:
                    await self._load(f"{origin}/robots.txt", entry)
        if entry.rules is None:
            raise FetchError(
                "robots_unavailable",
                f"robots.txt for {url.host} answered with an error, so the site is treated as "
                "off limits for now",
            )
        return entry.rules

    async def _load(self, robots_url: str, entry: _Entry) -> None:
        # An error raised by the read itself (a blocked address, an open breaker, a timeout, a
        # dead host) passes through unchanged and caches nothing: it says nothing about the
        # site's rules, and the page fetch would have failed the same way.
        status, body = await self._fetch(robots_url)
        if 200 <= status < 300:
            text = body[:MAX_ROBOTS_BYTES].decode("utf-8", "replace")
            entry.rules, ttl = parse_robots(text), self._ttl
        elif 400 <= status < 500 and status != 429:
            entry.rules, ttl = Rules(), self._ttl
        else:
            entry.rules, ttl = None, _UNREACHABLE_TTL_S
        entry.expires = self._clock() + ttl
