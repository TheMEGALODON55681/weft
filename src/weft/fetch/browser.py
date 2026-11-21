"""Headless Chromium for pages that need JavaScript.

The browser makes no connection of its own:

- The page's document is the body the Fetcher already fetched. It is handed to the browser, not
  fetched again.
- Every other navigation (frames, script redirects, popups) is fetched by the same Fetcher, one
  hop at a time, so robots.txt, host spacing, the breaker and the address guard apply to it as to
  any plain fetch. A redirect is handed to the browser as a refresh to the next URL, so the next
  hop is a new navigation and comes back here too.
- Everything else (scripts, images, XHR, WebSockets, workers) goes through GuardedProxy, which
  checks the address of every connection. Chromium is told not to bypass the proxy for
  loopback, not to resolve names itself, and not to send WebRTC traffic around it. Service
  workers are blocked: they outlive the page and their requests escape request routing.

One Chromium process serves every render, with a fresh context per page so no cookie or storage
outlives a call. It starts on first use and is replaced after `browser_recycle_after` pages.
"""

import asyncio
import html
import logging
from contextlib import suppress
from dataclasses import dataclass

import httpcore
from playwright.async_api import Browser, Playwright, Request, Route, async_playwright
from playwright.async_api import Error as PlaywrightError

from weft.config import Settings
from weft.fetch.errors import FetchError
from weft.fetch.http import Fetcher, FetchResult
from weft.fetch.proxy import GuardedProxy
from weft.fetch.ssrf import GuardedBackend
from weft.obs.logging import safe_url

log = logging.getLogger("weft.fetch")

# How long to wait, after the load event, for the page's own requests to go quiet.
_SETTLE_MS = 3000
# Closing a context whose page is stuck in a script should be quick. If it is not, the browser
# is replaced rather than trusted again.
_CLOSE_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class Rendered:
    final_url: str
    html: str
    truncated: bool  # the serialised DOM hit max_body_bytes


@dataclass(eq=False, slots=True)
class _Instance:
    browser: Browser
    uses: int = 0
    active: int = 0
    broken: bool = False


def _first_line(exc: Exception) -> str:
    """Playwright errors carry a call log and install hints after the first line."""
    return next(iter(str(exc).splitlines()), "")


def _content_type(res: FetchResult) -> str:
    ctype = res.content_type or "text/html"
    return f"{ctype}; charset={res.charset}" if res.charset else ctype


class BrowserPool:
    def __init__(
        self,
        settings: Settings,
        fetcher: Fetcher,
        proxy_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._settings = settings
        self._fetcher = fetcher
        self._proxy = GuardedProxy(
            proxy_backend or GuardedBackend(httpcore.AnyIOBackend(), settings.allow_loopback),
            connect_timeout_s=settings.connect_timeout_s,
            idle_timeout_s=settings.read_timeout_s,
        )
        self._pages = asyncio.Semaphore(settings.browser_max_pages)
        self._lock = asyncio.Lock()
        self._pw: Playwright | None = None
        self._proxy_port = 0
        self._current: _Instance | None = None
        self._retired: list[_Instance] = []
        self.launches = 0
        self._closed = False

    @property
    def open_browsers(self) -> int:
        live = [self._current, *self._retired]
        return sum(1 for i in live if i is not None and i.browser.is_connected())

    async def aclose(self) -> None:
        """Close every browser, stop the driver and the proxy. Safe to call more than once."""
        async with self._lock:
            self._closed = True  # a render still queued must not launch a browser after this
            for inst in [self._current, *self._retired]:
                if inst is not None:
                    with suppress(PlaywrightError):
                        await inst.browser.close()
            self._current, self._retired = None, []
            if self._pw is not None:
                # Stopping the driver also kills any browser it still owns, including one whose
                # launch was cancelled before it was handed back.
                await self._pw.stop()
                self._pw = None
            await self._proxy.aclose()

    async def render(self, res: FetchResult) -> Rendered:
        """Load `res` in the browser and return the DOM after scripts have run."""
        timeout = self._settings.render_timeout_s
        try:
            async with asyncio.timeout(timeout):
                async with self._pages:
                    inst = await self._acquire()
                    try:
                        return await self._render(inst, res)
                    finally:
                        await self._release(inst)
        except TimeoutError:
            raise FetchError("timeout", f"rendering took longer than {timeout:g}s") from None
        except PlaywrightError as exc:
            log.warning(
                "render failed",
                extra={"url": safe_url(res.final_url), "error": _first_line(exc)},
            )
            raise FetchError("render_failed", "the browser could not render the page") from None

    async def _acquire(self) -> _Instance:
        async with self._lock:
            if self._closed:
                raise FetchError("render_unavailable", "the browser pool is shutting down")
            cur = self._current
            if (
                cur is None
                or cur.broken
                or cur.uses >= self._settings.browser_recycle_after
                or not cur.browser.is_connected()
            ):
                inst = self._current = _Instance(await self._launch())
                if cur is not None:
                    self._retired.append(cur)
                    await self._close_if_idle(cur)
            else:
                inst = cur
            inst.uses += 1
            inst.active += 1
            return inst

    async def _release(self, inst: _Instance) -> None:
        inst.active -= 1
        if inst is not self._current:
            await self._close_if_idle(inst)

    async def _close_if_idle(self, inst: _Instance) -> None:
        if inst.active == 0 and inst in self._retired:
            self._retired.remove(inst)
            with suppress(PlaywrightError):
                await inst.browser.close()

    async def _launch(self) -> Browser:
        s = self._settings
        try:
            if self._pw is None:
                self._proxy_port = await self._proxy.start()
                self._pw = await async_playwright().start()
            browser = await self._pw.chromium.launch(
                headless=True,
                # "<-loopback>" removes Chromium's built-in exception for localhost, which would
                # otherwise connect to loopback addresses directly.
                proxy={"server": f"http://127.0.0.1:{self._proxy_port}", "bypass": "<-loopback>"},
                args=[
                    # Names are resolved by the proxy. If anything tried to go around it, it
                    # could not even look up a host.
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                    # WebRTC speaks UDP, which an HTTP proxy cannot carry.
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    f"--js-flags=--max-old-space-size={s.browser_js_heap_mb}",
                ],
            )
        except PlaywrightError as exc:
            log.warning("browser unavailable", extra={"error": _first_line(exc)})
            raise FetchError(
                "render_unavailable",
                "headless Chromium could not be started on this server",
            ) from None
        self.launches += 1
        log.info("browser started", extra={"launches": self.launches})
        return browser

    async def _render(self, inst: _Instance, res: FetchResult) -> Rendered:
        s = self._settings
        ctx = await inst.browser.new_context(
            service_workers="block", accept_downloads=False, user_agent=s.user_agent
        )
        try:
            document = [res]  # handed out once, to the first navigation

            async def route(route: Route, request: Request) -> None:
                with suppress(PlaywrightError):  # the context may close mid-request
                    if not request.is_navigation_request():
                        await route.continue_()  # through the guarded proxy
                    elif document:
                        doc = document.pop()
                        await route.fulfill(
                            status=doc.status, content_type=_content_type(doc), body=doc.body
                        )
                    else:
                        await self._navigate(route, request)

            await ctx.route("**/*", route)
            page = await ctx.new_page()
            page.set_default_timeout(s.render_timeout_s * 1000)
            await page.goto(res.final_url, wait_until="load")
            with suppress(PlaywrightError):
                await page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
            dom = await page.content()
            final_url = page.url
        finally:
            try:
                async with asyncio.timeout(_CLOSE_TIMEOUT_S):
                    await ctx.close()
            except (TimeoutError, PlaywrightError):
                inst.broken = True
        cap = s.max_body_bytes
        return Rendered(final_url, dom[:cap], len(dom) > cap)

    async def _navigate(self, route: Route, request: Request) -> None:
        """Fetch one hop of a navigation the way a plain fetch would, or refuse it."""
        if request.method != "GET":
            await route.abort("blockedbyclient")
            return
        try:
            hop = await self._fetcher.fetch(request.url, follow_redirects=False)
        except FetchError:  # robots, breaker, spacing, address: the Fetcher has logged it
            await route.abort("blockedbyclient")
            return
        if hop.location:
            # Chromium would follow a fulfilled 3xx without asking the route again, so the next
            # hop would skip robots.txt and the limits. A refresh makes it a new navigation.
            body = f'<meta http-equiv="refresh" content="0;url={html.escape(hop.location)}">'
            await route.fulfill(status=200, content_type="text/html", body=body)
            return
        await route.fulfill(status=hop.status, content_type=_content_type(hop), body=hop.body)
