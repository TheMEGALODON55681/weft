"""Plain HTTP fetching. Redirects are followed by hand so every hop gets its own URL validation,
robots.txt check, breaker check and rate-limit slot. The SSRF check runs when each connection
opens (see ssrf.py). Every limit (time, size, hops) is enforced on the bytes actually received,
after decompression.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from http.cookiejar import DefaultCookiePolicy

import httpx

from weft.config import Settings
from weft.fetch.errors import FetchError
from weft.fetch.limits import CircuitBreaker, HostLimiter
from weft.fetch.retry import backoff_delay, parse_retry_after
from weft.fetch.robots import ROBOTS_AGENT, RobotsCache
from weft.fetch.ssrf import guarded_transport
from weft.obs.logging import safe_url

log = logging.getLogger("weft.fetch")

MAX_URL_LENGTH = 8192
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Responses that say the host is overloaded or limiting us. They count as host failures for the
# breaker. A plain 500 does not: one broken page should not close the whole site.
_BACKOFF_STATUSES = frozenset({429, 502, 503, 504})
# Transport failures worth counting against the host.
_TRANSIENT = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    final_url: str
    status: int
    content_type: str  # lowercased media type, "" when the server sent none
    charset: str | None
    body: bytes
    truncated: bool
    redirects: list[str] = field(default_factory=list)  # every URL requested before final_url
    elapsed_ms: float = 0.0


def parse_http_url(raw: str) -> httpx.URL:
    """Validate a user-supplied URL. Only http(s) with a host and no embedded credentials."""
    raw = raw.strip()
    if not raw or len(raw) > MAX_URL_LENGTH:
        raise FetchError("invalid_url", "URL is empty or too long")
    try:
        url = httpx.URL(raw)
    except httpx.InvalidURL as exc:
        raise FetchError("invalid_url", f"malformed URL: {exc}") from None
    if url.scheme not in ("http", "https"):
        raise FetchError("invalid_url", f"unsupported scheme {url.scheme!r}; use http or https")
    if not url.host:
        raise FetchError("invalid_url", "URL has no host")
    if url.userinfo:
        raise FetchError("invalid_url", "credentials in the URL are not accepted")
    return url


def _as_fetch_error(exc: Exception) -> FetchError:
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return FetchError("timeout", "request timed out")
    return FetchError("network_error", f"{type(exc).__name__}: {exc}")


class Fetcher:
    def __init__(self, settings: Settings) -> None:
        self._max_body = settings.max_body_bytes
        self._max_redirects = settings.max_redirects
        self._total_timeout = settings.total_timeout_s
        self._interval = settings.host_min_interval_s
        self._max_retries = settings.max_retries
        self._backoff = settings.retry_backoff_s
        self._robots = RobotsCache(self._fetch_robots)
        self._limiter = HostLimiter(settings.host_concurrency, settings.global_concurrency)
        self._breaker = CircuitBreaker(settings.breaker_threshold)
        self._client = httpx.AsyncClient(
            transport=guarded_transport(settings.allow_loopback, settings.http2),
            # A proxy resolves names itself, so honouring HTTP_PROXY would route around the
            # address check. This only turns off proxy variables: the transport still reads
            # SSL_CERT_FILE, which has no bearing on where a request goes.
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(settings.read_timeout_s, connect=settings.connect_timeout_s),
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            },
        )

        # Stateless on purpose: one client serves every caller, so a persistent cookie jar would
        # carry one caller's session into another's. Authenticated crawls pass cookies per job.
        self._client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, raw_url: str) -> FetchResult:
        start = time.perf_counter()
        try:
            async with asyncio.timeout(self._total_timeout):
                result = await self._fetch(raw_url, start)
        except (FetchError, TimeoutError, httpx.RequestError) as exc:
            err = exc if isinstance(exc, FetchError) else _as_fetch_error(exc)
            log.warning("fetch failed", extra={"url": safe_url(raw_url), "code": err.code})
            raise err from None
        log.info(
            "fetched",
            extra={
                "url": safe_url(result.url),
                "final_url": safe_url(result.final_url),
                "status": result.status,
                "bytes": len(result.body),
                "truncated": result.truncated,
                "redirects": len(result.redirects),
                "elapsed_ms": round(result.elapsed_ms, 1),
            },
        )
        return result

    async def _fetch_robots(self, url: str) -> tuple[int, bytes]:
        res = await self._fetch(url, time.perf_counter(), robots=False)
        return res.status, res.body

    async def _check_robots(self, url: httpx.URL) -> float:
        """Refuse a disallowed URL. Returns the site's Crawl-delay in seconds, 0 if it set none."""
        if url.raw_path == b"/robots.txt":  # always fetchable, or nobody could read the rules
            return 0.0
        rules = await self._robots.rules_for(url)
        if not rules.allows(url.raw_path.decode("ascii", "replace")):
            raise FetchError(
                "robots_disallowed",
                f"robots.txt on {url.host} disallows {ROBOTS_AGENT} from fetching {url.path}",
            )
        return rules.crawl_delay or 0.0

    async def _fetch(self, raw_url: str, start: float, *, robots: bool = True) -> FetchResult:
        url = parse_http_url(raw_url)
        seen: set[str] = set()
        chain: list[str] = []
        while True:
            seen.add(str(url.copy_with(fragment=None)))
            interval = 0.0  # robots.txt reads are not spaced, so they cost the first page nothing
            if robots:
                interval = max(self._interval, await self._check_robots(url))
            resp, body, truncated = await self._send(url, interval, start)
            location = resp.headers.get("location")
            if resp.status_code in _REDIRECT_STATUSES and location:
                if len(chain) >= self._max_redirects:
                    raise FetchError(
                        "too_many_redirects", f"more than {self._max_redirects} redirects"
                    )
                try:
                    nxt = parse_http_url(str(url.join(location)))
                except httpx.InvalidURL as exc:
                    raise FetchError("invalid_url", f"bad redirect target: {exc}") from None
                if str(nxt.copy_with(fragment=None)) in seen:
                    raise FetchError("redirect_loop", "redirects lead back to a prior URL")
                chain.append(str(url))
                url = nxt
                continue
            return FetchResult(
                url=raw_url.strip(),
                final_url=str(url),
                status=resp.status_code,
                content_type=resp.headers.get("content-type", "").partition(";")[0].strip().lower(),
                charset=resp.charset_encoding,
                body=body,
                truncated=truncated,
                redirects=chain,
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    async def _send(
        self, url: httpx.URL, interval_s: float, start: float
    ) -> tuple[httpx.Response, bytes, bool]:
        """Request `url`, retrying transport failures and 429/502/503/504 with backoff. Every
        attempt passes the host's breaker and limits, so a retry is as polite as the first try and
        stops as soon as the breaker opens. Returns the response (its headers are still readable
        after it closes), its body and whether the body hit the cap. When retries run out, a
        transport failure is raised and an overloaded response is returned as a result."""
        host = url.host
        attempt = 0
        while True:
            self._breaker.peek(host)  # fail fast instead of queuing for a host that is down
            try:
                async with self._limiter.slot(host, interval_s, self._total_timeout):
                    self._breaker.check(host)  # it may have opened while this request waited
                    resp, body, truncated = await self._attempt(url)
            except _TRANSIENT as exc:
                self._breaker.record(host, ok=False)
                if attempt >= self._max_retries:
                    raise
                delay, reason = backoff_delay(attempt, self._backoff), type(exc).__name__
            else:
                overloaded = resp.status_code in _BACKOFF_STATUSES
                self._breaker.record(host, ok=not overloaded)
                if not overloaded or attempt >= self._max_retries:
                    return resp, body, truncated
                delay = max(
                    backoff_delay(attempt, self._backoff),
                    parse_retry_after(resp.headers.get("retry-after")),
                )
                if delay >= self._total_timeout - (time.perf_counter() - start):
                    return resp, body, truncated  # the wait alone would outlast this call
                reason = str(resp.status_code)
            log.info(
                "retrying",
                extra={"url": safe_url(str(url)), "attempt": attempt + 1, "reason": reason},
            )
            await asyncio.sleep(delay)
            attempt += 1

    async def _attempt(self, url: httpx.URL) -> tuple[httpx.Response, bytes, bool]:
        async with self._client.stream("GET", url) as resp:
            if resp.status_code in _REDIRECT_STATUSES and resp.headers.get("location"):
                return resp, b"", False  # about to be followed; the body is not needed
            body, truncated = await self._read_capped(resp)
            return resp, body, truncated

    # Known limit: the cap is checked per decoded chunk, so a gzip bomb can briefly hold ~1000x
    # one network chunk (tens of MB) before it is cut. Stream-decode with a limit if that matters.
    async def _read_capped(self, resp: httpx.Response) -> tuple[bytes, bool]:
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf += chunk
            if len(buf) > self._max_body:
                return bytes(buf[: self._max_body]), True
        return bytes(buf), False
