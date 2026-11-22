"""The browser pool, against a real headless Chromium.

The SSRF cases run the browser behind a proxy whose resolver is fake: `cdn.test` is a public
address and `internal.test` a private one, and the backend under the guard quietly sends both to
the fixture site. If anything got past the guard, the fixture would count the request, so a zero
count means the browser was stopped, not that the request failed for some unrelated reason. Each
case carries a positive control that must reach the fixture by the same mechanism.
"""

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpcore
import pytest

from fixture_site import JS_SHELL_HTML, FixtureSite
from skips import skip_or_fail
from weft.config import Settings
from weft.fetch.browser import BrowserPool, Rendered
from weft.fetch.errors import FetchError
from weft.fetch.http import Fetcher, FetchResult
from weft.fetch.ssrf import GuardedBackend

PUBLIC = "93.184.216.34"
PRIVATE = "10.0.0.1"
NAMES = {"cdn.test": [PUBLIC], "internal.test": [PRIVATE], "localhost": ["127.0.0.1"]}


class ToFixture(httpcore.AsyncNetworkBackend):
    """Connects every address to 127.0.0.1, and remembers what it was asked for."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self._real = httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, **kw: Any) -> httpcore.AsyncNetworkStream:
        self.asked.append(host)
        return await self._real.connect_tcp("127.0.0.1", port, **kw)

    async def connect_unix_socket(self, path: str, **_: Any) -> httpcore.AsyncNetworkStream:
        raise AssertionError("not used")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


async def fake_resolve(host: str, port: int) -> list[str]:
    return NAMES.get(host, [host])


def prefetched(site: FixtureSite, html: str, path: str = "/page") -> FetchResult:
    """What a plain HTTP fetch of `path` would have returned, without making it."""
    url = site.url(path)
    return FetchResult(url, url, 200, "text/html", "utf-8", html.encode(), False)


@pytest.fixture
async def pool_with() -> AsyncIterator[Callable[..., tuple[BrowserPool, Fetcher]]]:
    opened: list[tuple[BrowserPool, Fetcher]] = []

    def make(
        proxy_backend: httpcore.AsyncNetworkBackend | None = None, **overrides: object
    ) -> tuple[BrowserPool, Fetcher]:
        settings = Settings(**overrides)
        fetcher = Fetcher(settings)
        pool = BrowserPool(settings, fetcher, proxy_backend=proxy_backend)
        opened.append((pool, fetcher))
        return pool, fetcher

    yield make
    for pool, fetcher in opened:
        await pool.aclose()
        await fetcher.aclose()


async def render(pool: BrowserPool, res: FetchResult) -> str:
    try:
        return (await pool.render(res)).html
    except FetchError as exc:
        if exc.code == "render_unavailable":
            skip_or_fail(f"headless Chromium is not installed: {exc}")
        raise


async def test_a_javascript_shell_is_rendered(site: FixtureSite, pool_with) -> None:
    pool, _ = pool_with()
    html = await render(pool, prefetched(site, JS_SHELL_HTML))
    assert "Rendered report" in html


LEAKS = """<!doctype html><html><body><p id="out">waiting</p>
<script src="http://cdn.test:{P}/script.js"></script>
<img src="http://internal.test:{P}/leak/img">
<img src="http://127.0.0.1:{P}/leak/loopback-img">
<img src="http://localhost:{P}/leak/localhost-img">
<img src="http://[::1]:{P}/leak/v6-img">
<img src="http://cdn.test:{P}/goto?u=http://internal.test:{P}/leak/redirect">
<link rel="stylesheet" href="http://internal.test:{P}/leak/css">
<iframe src="http://127.0.0.1:{P}/leak/frame"></iframe>
<script>
const P = {P};
const ws = (url) => new Promise((done) => {{
  try {{ const s = new WebSocket(url); s.onopen = s.onerror = s.onclose = () => done(); }}
  catch (e) {{ done(); }}
}});
const worker = new Worker(URL.createObjectURL(new Blob(
  [`fetch("http://internal.test:${{P}}/leak/worker").finally(() => postMessage(1))`])));
const tries = [
  fetch(`http://cdn.test:${{P}}/ok/fetch`, {{mode: "no-cors"}}),
  fetch(`http://internal.test:${{P}}/leak/fetch`, {{mode: "no-cors"}}),
  fetch(`http://127.0.0.1:${{P}}/leak/fetch-loopback`, {{mode: "no-cors"}}),
  ws(`ws://cdn.test:${{P}}/ok/ws`),
  ws(`ws://internal.test:${{P}}/leak/ws`),
  ws(`ws://127.0.0.1:${{P}}/leak/ws-loopback`),
  new Promise((done) => {{ worker.onmessage = worker.onerror = done; }}),
];
navigator.sendBeacon(`http://internal.test:${{P}}/leak/beacon`, "x");
Promise.allSettled(tries).then(() => {{ document.getElementById("out").textContent = "done"; }});
</script></body></html>"""


async def test_an_allowed_page_cannot_reach_blocked_hosts(make_site, pool_with) -> None:
    """The page itself is allowed. Everything it loads goes through the guarded proxy, and every
    route to a private or loopback address is refused before a connection is made."""
    site = make_site()
    backend = ToFixture()
    pool, _ = pool_with(
        GuardedBackend(backend, allow_loopback=False, resolve=fake_resolve), allow_loopback=False
    )
    html = await render(pool, prefetched(site, LEAKS.format(P=site.port)))
    assert ">done<" in html  # every attempt had finished before the page was read
    # Positive controls: the same mechanisms reach an allowed host through the proxy.
    for path in ("/script.js", "/ok/fetch", "/ok/ws", "/goto"):
        assert site.hits(path) == 1, path
    assert site.hits_under("/leak/") == {}
    assert set(backend.asked) == {PUBLIC}  # nothing else was even dialled


async def test_udp_sends_nothing_to_a_blocked_address(site: FixtureSite, pool_with) -> None:
    """WebRTC and WebTransport speak UDP, which an HTTP proxy cannot carry. Neither may go
    around the proxy."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.bind(("127.0.0.1", 0))
        udp.setblocking(False)
        port = udp.getsockname()[1]
        pool, _ = pool_with(
            GuardedBackend(ToFixture(), allow_loopback=False, resolve=fake_resolve),
            allow_loopback=False,
        )
        # Gathering starts once the local description is set; STUN goes out straight after.
        page = f"""<p id="out">waiting</p><script>
        const out = (t) => {{ document.getElementById("out").textContent = t; }};
        try {{
          const pc = new RTCPeerConnection({{iceServers: [
            {{urls: "stun:127.0.0.1:{port}"}}, {{urls: "turn:127.0.0.1:{port}", username: "u",
              credential: "c"}}]}});
          pc.createDataChannel("x");
          pc.createOffer().then((o) => pc.setLocalDescription(o)).then(() => out("gathering"));
        }} catch (e) {{ out("no webrtc"); }}
        // WebTransport is QUIC, also UDP.
        try {{ new WebTransport("https://127.0.0.1:{port}/").ready.catch(() => {{}}); }}
        catch (e) {{}}
        </script>"""
        html = await render(pool, prefetched(site, page))
        assert ">gathering<" in html or ">no webrtc<" in html
        await asyncio.sleep(1)  # anything sent is in the socket buffer by now
        with pytest.raises(BlockingIOError):
            udp.recvfrom(2048)


async def test_service_workers_are_not_registered(make_site, pool_with) -> None:
    site = make_site()
    pool, _ = pool_with()
    # The prototype call gets past a stub on the navigator object, so try both.
    page = """<p id="out">waiting</p><script>
    const tries = [];
    try { tries.push(navigator.serviceWorker.register("/sw.js?a")); } catch (e) {}
    try { tries.push(ServiceWorkerContainer.prototype.register.call(
      navigator.serviceWorker, "/sw.js?b")); } catch (e) {}
    Promise.allSettled(tries).then(() => {
      document.getElementById("out").textContent = "done"; });
    </script>"""
    html = await render(pool, prefetched(site, page))
    assert ">done<" in html
    assert site.hits("/sw.js") == 0


async def test_navigations_obey_robots_txt_on_every_hop(make_site, pool_with) -> None:
    """Frames and script navigations are fetched by the same Fetcher as plain HTTP, so
    robots.txt applies to them, including after a redirect."""
    site = make_site()  # its robots.txt disallows /private/
    pool, _ = pool_with()
    page = """<iframe src="/private/frame"></iframe>
    <iframe src="/goto?u=/private/after-redirect"></iframe>
    <iframe src="/redirect"></iframe>
    <script>setTimeout(() => { location.href = "/private/script"; }, 50);</script>"""
    await render(pool, prefetched(site, page))
    assert site.hits_under("/private/") == {}
    assert site.hits("/goto") == 1
    assert site.hits("/article") == 1  # an allowed redirect was followed
    assert site.hits("/robots.txt") == 1


async def test_navigations_wait_their_turn_at_the_host(make_site, pool_with) -> None:
    site = make_site()
    pool, fetcher = pool_with(host_min_interval_s=60, total_timeout_s=3)
    await fetcher.fetch(site.url("/ping"))  # the page itself, which takes this host's turn
    await render(pool, prefetched(site, '<iframe src="/article"></iframe>'))
    assert site.hits("/article") == 0  # its turn was a minute away, so it was refused


async def test_navigations_respect_an_open_breaker(make_site, pool_with) -> None:
    site = make_site()
    pool, fetcher = pool_with(breaker_threshold=1, max_retries=0)
    await fetcher.fetch(site.url("/flaky/down/9?s=503"))  # opens the breaker for the host
    await render(pool, prefetched(site, '<iframe src="/ping"></iframe>'))
    assert site.hits("/ping") == 0


async def test_rendering_is_limited_to_the_configured_number_of_pages(make_site, pool_with) -> None:
    site = make_site()
    pool, _ = pool_with(browser_max_pages=1)
    res = prefetched(site, '<img src="/hold">')
    await asyncio.gather(render(pool, res), render(pool, res), render(pool, res))
    assert site.hits("/hold") == 3
    assert site.peak == 1


async def test_the_browser_is_replaced_after_its_page_budget(site: FixtureSite, pool_with) -> None:
    pool, _ = pool_with(browser_recycle_after=2)
    res = prefetched(site, "<p>hello</p>")
    for _ in range(5):
        await render(pool, res)
    assert pool.launches == 3
    assert pool.open_browsers == 1  # the replaced ones were closed, not left running


async def test_a_page_that_exhausts_its_memory_fails_and_the_pool_recovers(
    site: FixtureSite, pool_with
) -> None:
    pool, _ = pool_with(browser_js_heap_mb=32)
    hog = "<script>const a = []; for (;;) { a.push(new Array(1e6).fill(1)); }</script>"
    with pytest.raises(FetchError) as e:
        await render(pool, prefetched(site, hog))
    assert e.value.code in ("render_failed", "timeout")
    assert "Rendered report" in await render(pool, prefetched(site, JS_SHELL_HTML))


async def test_a_render_that_overruns_its_budget_times_out(site: FixtureSite, pool_with) -> None:
    pool, _ = pool_with(render_timeout_s=0.5)
    with pytest.raises(FetchError) as e:
        await render(pool, prefetched(site, '<img src="/slow">'))
    assert e.value.code == "timeout"


async def test_a_missing_browser_is_reported_as_unavailable(
    site: FixtureSite, pool_with, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))  # an empty browser cache
    pool, _ = pool_with()
    with pytest.raises(FetchError) as e:
        await pool.render(prefetched(site, "<p>hi</p>"))
    assert e.value.code == "render_unavailable"


async def test_a_page_that_spins_forever_times_out_and_the_pool_recovers(
    site: FixtureSite, pool_with
) -> None:
    pool, _ = pool_with(render_timeout_s=3)
    with pytest.raises(FetchError) as e:
        await render(pool, prefetched(site, "<script>for (;;) {}</script>"))
    assert e.value.code == "timeout"
    assert "Rendered report" in await render(pool, prefetched(site, JS_SHELL_HTML))


async def test_a_closed_pool_starts_no_browser(site: FixtureSite, pool_with) -> None:
    """A render that arrives during shutdown must not launch a Chromium nobody will close."""
    pool, _ = pool_with()
    await render(pool, prefetched(site, "<p>hi</p>"))
    await pool.aclose()
    with pytest.raises(FetchError) as e:
        await pool.render(prefetched(site, "<p>hi</p>"))
    assert e.value.code == "render_unavailable"
    assert pool.launches == 1
    assert pool.open_browsers == 0


# --- sub-resource budget and pacing -----------------------------------------------------------


async def render_result(pool: BrowserPool, res: FetchResult) -> Rendered:
    try:
        return await pool.render(res)
    except FetchError as exc:
        if exc.code == "render_unavailable":
            skip_or_fail(f"headless Chromium is not installed: {exc}")
        raise


SEQUENTIAL_BLOBS = """<p id="out">0</p><script>
(async () => {
  let n = 0;
  for (let i = 0; i < 10; i++) {
    try {
      const r = await fetch(`/blob/40000/${i}`);  // refused ones come back as 403
      if (r.ok) { await r.arrayBuffer(); n++; }
    } catch (e) {}
    document.getElementById("out").textContent = String(n);
  }
  document.getElementById("out").textContent += " finished";
})();
</script>"""


async def test_sub_resources_stop_when_the_byte_budget_is_spent(make_site, pool_with) -> None:
    """Ten 40 kB downloads against a 100 kB budget: the third is cut short, and nothing after
    it reaches the site."""
    site = make_site()
    pool, _ = pool_with(render_max_bytes=100_000)
    shot = await render_result(pool, prefetched(site, SEQUENTIAL_BLOBS))
    assert shot.truncated  # the budget is reported as truncation
    assert " finished" in shot.html  # the page itself carried on
    assert len(site.hits_under("/blob/")) == 3
    loaded = int(shot.html.split('id="out">')[1].split(" ")[0])
    assert loaded == 2


async def test_a_generous_budget_changes_nothing(make_site, pool_with) -> None:
    site = make_site()
    pool, _ = pool_with()
    shot = await render_result(pool, prefetched(site, SEQUENTIAL_BLOBS))
    assert not shot.truncated
    assert ">10 finished<" in shot.html


PARALLEL_PINGS = """<script>
Promise.allSettled([0, 1, 2, 3].map((i) => fetch(`/ping?i=${i}`)))
  .then(() => { document.body.dataset.done = "1"; });
</script>"""


@pytest.mark.parametrize(("interval", "min_gap"), [(0.25, 0.2), (0.0, None)])
async def test_new_connections_to_a_host_are_spaced(
    make_site, pool_with, interval: float, min_gap: float | None
) -> None:
    site = make_site()
    pool, _ = pool_with(render_host_interval_s=interval)
    await render_result(pool, prefetched(site, PARALLEL_PINGS))
    times = site.times("/ping")
    assert len(times) == 4
    gaps = [b - a for a, b in pairwise(times)]
    if min_gap is None:
        assert max(gaps) < 0.2  # the control: unpaced, they go out together
    else:
        assert min(gaps) >= min_gap
