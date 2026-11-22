"""A small HTTP/2 server for tests, over TLS with ALPN, built on the `h2` library that httpx already
needs. The standard library has no HTTP/2 server, and `fixture_site.py` speaks HTTP/1.1 even over
TLS. It runs on its own thread and event loop, as that one does, and records what it saw so a test
can say which protocol the client really negotiated.

It also answers HTTP/1.1 on the same port when the client offers nothing else, so one server can
show what `http2=True` and `http2=False` each do.
"""

import asyncio
import contextlib
import gzip
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions

from fixture_site import ARTICLE_HTML

Response = tuple[int, dict[str, str], bytes]
BIG = b"<html><body>" + b"a" * 300_000 + b"</body></html>"  # far past the 65,535 byte window


def _routes() -> dict[str, Response]:
    article = ARTICLE_HTML.encode()
    return {
        "/article": (200, {"content-type": "text/html; charset=utf-8"}, article),
        "/big": (200, {"content-type": "text/html"}, BIG),
        "/gzip": (
            200,
            {"content-type": "text/html; charset=utf-8", "content-encoding": "gzip"},
            gzip.compress(article),
        ),
        "/redirect": (302, {"location": "/article"}, b""),
        "/redirect-to-private": (302, {"location": "http://10.0.0.1/admin"}, b""),
    }


class H2Site:
    def __init__(self, tls: ssl.SSLContext) -> None:
        tls.set_alpn_protocols(["h2", "http/1.1"])
        self._tls = tls
        self.routes = _routes()
        self.protocols: list[str | None] = []  # the ALPN protocol of every connection accepted
        self.requests: list[tuple[str, str]] = []  # (method, path) of every request
        self.reset_paths: set[str] = set()  # paths answered with RST_STREAM instead of a response
        self.port = 0
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def url(self, path: str, host: str = "localhost") -> str:
        return f"https://{host}:{self.port}{path}"

    def _route(self, path: str) -> Response:
        path = path.split("?", 1)[0]
        if path == "/robots.txt":
            return 404, {"content-type": "text/plain"}, b"not found"
        return self.routes.get(path, (404, {"content-type": "text/plain"}, b"not found"))

    # --- the server -------------------------------------------------------------------------

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        server = self._loop.run_until_complete(
            asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=self._tls)
        )
        self.port = int(server.sockets[0].getsockname()[1])
        self._ready.set()
        self._loop.run_forever()
        server.close()
        tasks = asyncio.all_tasks(self._loop)
        for task in tasks:
            task.cancel()
        self._loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        self._loop.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        protocol = writer.get_extra_info("ssl_object").selected_alpn_protocol()
        self.protocols.append(protocol)
        try:
            with contextlib.suppress(ConnectionError, ssl.SSLError, asyncio.IncompleteReadError):
                if protocol == "h2":
                    await self._serve_h2(reader, writer)
                else:
                    await self._serve_h1(reader, writer)
        finally:
            writer.close()

    async def _serve_h1(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        method, path, _ = head.split(b"\r\n", 1)[0].decode().split(" ", 2)
        self.requests.append((method, path))
        status, headers, body = self._route(path)
        lines = [f"HTTP/1.1 {status} X", f"content-length: {len(body)}", "connection: close"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        await writer.drain()

    async def _serve_h2(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
        )
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        unsent: dict[int, bytes] = {}  # stream id -> the part of its body the window has not let go
        while data := await reader.read(65535):
            try:
                events = conn.receive_data(data)
            except h2.exceptions.ProtocolError:
                break
            for event in events:
                if isinstance(event, h2.events.RequestReceived):
                    self._answer(conn, event, unsent)
                elif isinstance(event, h2.events.StreamReset):
                    unsent.pop(event.stream_id, None)
                elif isinstance(event, h2.events.ConnectionTerminated):
                    return
            self._send_what_fits(conn, unsent)  # a WindowUpdated event is what lets more go
            writer.write(conn.data_to_send())
            await writer.drain()

    def _answer(
        self,
        conn: h2.connection.H2Connection,
        event: h2.events.RequestReceived,
        unsent: dict[int, bytes],
    ) -> None:
        sent = dict(event.headers)
        method, path = sent[":method"], sent[":path"]
        self.requests.append((method, path))
        if path.split("?", 1)[0] in self.reset_paths:
            conn.reset_stream(event.stream_id, error_code=h2.errors.ErrorCodes.INTERNAL_ERROR)
            return
        status, headers, body = self._route(path)
        out = [(":status", str(status)), ("content-length", str(len(body))), *headers.items()]
        conn.send_headers(event.stream_id, out, end_stream=not body)
        if body:
            unsent[event.stream_id] = body

    @staticmethod
    def _send_what_fits(conn: h2.connection.H2Connection, unsent: dict[int, bytes]) -> None:
        for stream_id, body in list(unsent.items()):
            while body:
                n = min(len(body), conn.local_flow_control_window(stream_id))
                n = min(n, conn.max_outbound_frame_size)
                if n <= 0:
                    break
                conn.send_data(stream_id, body[:n])
                body = body[n:]
            if body:
                unsent[stream_id] = body
            else:
                conn.end_stream(stream_id)
                del unsent[stream_id]

    # --- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("the HTTP/2 fixture did not start")

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


@contextmanager
def run_h2_site(tls: ssl.SSLContext) -> Iterator[H2Site]:
    site = H2Site(tls)
    site.start()
    try:
        yield site
    finally:
        site.stop()
