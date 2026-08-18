"""Shared fixtures.

Every test gets a throwaway tape directory and a guaranteed-clean process
state afterwards -- these tests patch global modules, so a leaked patch would
corrupt every test that followed it.
"""

import pytest

import reeltime as tape
from reeltime.core import callsite
from reeltime.core import tape as tape_state


@pytest.fixture(autouse=True)
def clean_state():
    yield
    tape_state._reset_for_tests()
    callsite.clear_cache()


@pytest.fixture
def tape_dir(tmp_path):
    return tmp_path / ".tape"


@pytest.fixture
def recording(tape_dir):
    """An installed tape, uninstalled at the end of the test."""
    run = tape.install(tape_dir=tape_dir, collect_git=False)
    try:
        yield run
    finally:
        if not run.closed:
            tape.uninstall()


def read(run):
    """Parse the trace a (closed) run produced."""
    return tape.read_trace(run.path)


# -- a local HTTP server the tests drive ---------------------------------
#
# Recording is tested against a real socket rather than a mocked transport on
# purpose: the shim's whole job is to sit under httpx's own machinery, and a
# fake transport would skip exactly the layer being tested.

import json as _json
import threading
import time as _time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Route:
    """One canned response."""

    def __init__(self, status=200, json=None, text=None, raw=None, headers=None,
                 sse=None, chunk_delay=0.02):
        self.status = status
        self.json = json
        self.text = text
        self.raw = raw
        self.headers = headers or {}
        self.sse = sse
        self.chunk_delay = chunk_delay


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _read_body(self):
        # Streamed uploads arrive chunked, with no content-length.
        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            body = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0] or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return body
                body += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("content-length") or 0)
        return self.rfile.read(length) if length else b""

    def _serve(self):
        body = self._read_body()
        path = self.path.split("?")[0]
        self.server.received.append(
            {"path": path, "headers": dict(self.headers), "body": body}
        )
        route = self.server.routes.get(path) or self.server.routes.get("*")
        if route is not None and route.sse is not None and route.json is not None:
            # A route can carry both shapes; pick the one the caller asked for,
            # the way a real provider does.
            wants_stream = b'"stream": true' in body or b'"stream":true' in body
            route = Route(status=route.status, headers=route.headers,
                          sse=route.sse if wants_stream else None,
                          json=None if wants_stream else route.json,
                          chunk_delay=route.chunk_delay)
        if route is None:
            self.send_response(404)
            self.send_header("content-length", "0")
            self.end_headers()
            return

        if route.sse is not None:
            self.send_response(route.status)
            self.send_header("content-type", "text/event-stream")
            for key, value in route.headers.items():
                self.send_header(key, value)
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            for chunk in route.sse:
                data = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()
                # Real streams arrive as separate reads; without a pause the
                # kernel coalesces them and the chunk-boundary test is vacuous.
                _time.sleep(route.chunk_delay)
            self.wfile.write(b"0\r\n\r\n")
            return

        if route.raw is not None:
            payload, content_type = route.raw, "application/octet-stream"
        elif route.json is not None:
            payload = _json.dumps(route.json).encode("utf-8")
            content_type = "application/json"
        else:
            payload = (route.text or "").encode("utf-8")
            content_type = "text/plain"

        self.send_response(route.status)
        self.send_header("content-type", route.headers.get("content-type", content_type))
        self.send_header("content-length", str(len(payload)))
        for key, value in route.headers.items():
            if key.lower() != "content-type":
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _serve
    do_POST = _serve

    def log_message(self, *args):
        pass


class ServerHandle:
    def __init__(self, server):
        self._server = server
        self.base_url = "http://127.0.0.1:{}".format(server.server_address[1])

    def route(self, path, **kwargs):
        self._server.routes[path] = Route(**kwargs)
        return self.base_url + path

    @property
    def received(self):
        return self._server.received


@pytest.fixture
def server():
    # Threading, not the plain HTTPServer: httpx keeps connections alive, and a
    # single-threaded server blocks the next connection behind an idle one.
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.daemon_threads = True
    httpd.routes = {}
    httpd.received = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield ServerHandle(httpd)
    finally:
        httpd.shutdown()
        httpd.server_close()
