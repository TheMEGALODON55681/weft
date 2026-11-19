"""Plain HTTP fetching. Redirects are followed by hand so each hop is validated here and, later,
so the SSRF guard can vet every hop. Every limit (time, size, hops) is enforced on the bytes
actually received, after decompression.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from http.cookiejar import DefaultCookiePolicy

import httpx

from weft.config import Settings
from weft.obs.logging import safe_url

log = logging.getLogger("weft.fetch")

MAX_URL_LENGTH = 8192
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class FetchError(Exception):
    """A fetch that produced no usable response. `code` is stable and safe to branch on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
        self._client = httpx.AsyncClient(
            http2=settings.http2,
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

    async def _fetch(self, raw_url: str, start: float) -> FetchResult:
        url = parse_http_url(raw_url)
        seen: set[str] = set()
        chain: list[str] = []
        while True:
            seen.add(str(url.copy_with(fragment=None)))
            async with self._client.stream("GET", url) as resp:
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
                body, truncated = await self._read_capped(resp)
                return FetchResult(
                    url=raw_url.strip(),
                    final_url=str(url),
                    status=resp.status_code,
                    content_type=resp.headers.get("content-type", "")
                    .partition(";")[0]
                    .strip()
                    .lower(),
                    charset=resp.charset_encoding,
                    body=body,
                    truncated=truncated,
                    redirects=chain,
                    elapsed_ms=(time.perf_counter() - start) * 1000,
                )

    # Known limit: the cap is checked per decoded chunk, so a gzip bomb can briefly hold ~1000x
    # one network chunk (tens of MB) before it is cut. Stream-decode with a limit if that matters.
    async def _read_capped(self, resp: httpx.Response) -> tuple[bytes, bool]:
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf += chunk
            if len(buf) > self._max_body:
                return bytes(buf[: self._max_body]), True
        return bytes(buf), False
