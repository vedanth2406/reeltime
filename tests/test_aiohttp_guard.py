"""aiohttp is unsupported, and the guard makes that impossible to miss.

The assessment behind this is in `core/http/aiohttp_guard.py`: aiohttp has no
public seam at the level reeltime records at, so it stays uncovered. What is
not acceptable is the consequence -- an un-intercepted request during a replay
reaches the real network with nothing in the trace to say so. These tests pin
down that it cannot happen quietly.
"""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import reeltime as tape
from reeltime.core.http import AiohttpGuard
from reeltime.errors import TapeError

try:
    import aiohttp
except ImportError:  # pragma: no cover - asserted against below
    aiohttp = None


def test_aiohttp_is_installed_so_this_file_is_not_a_no_op():
    """`importorskip` at module scope is how a missing dev dependency once
    turned into 25 silently skipped tests. aiohttp is a dev dependency."""
    assert aiohttp is not None, (
        "aiohttp is a dev dependency; without it the guard is untested and the "
        "silent-divergence hole it closes reopens unnoticed")


needs_aiohttp = pytest.mark.skipif(aiohttp is None, reason="aiohttp is not installed")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def http_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:{}/".format(httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()


def _fetch(url):
    async def go():
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                return await response.json()

    return asyncio.run(go())


@needs_aiohttp
def test_the_guard_installs_when_aiohttp_is_importable(recording):
    assert AiohttpGuard(recording.engine).install() is True


@needs_aiohttp
def test_aiohttp_is_not_claimed_as_an_intercepted_backend(recording):
    """The footer answers "why was my call not recorded?". Listing aiohttp
    there would answer it wrongly."""
    assert "aiohttp" not in recording.http.installed


@needs_aiohttp
def test_a_recorded_aiohttp_call_warns_once_and_records_nothing(
        tape_dir, http_server, caplog):
    with caplog.at_level("WARNING", logger="reeltime"):
        with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01REC"):
            assert _fetch(http_server) == {"ok": True}
            assert _fetch(http_server) == {"ok": True}

    warnings = [r for r in caplog.records if "aiohttp" in r.getMessage()]
    assert len(warnings) == 1, "an agent in a loop must not emit hundreds"
    assert "@tape.tool" in warnings[0].getMessage()
    assert tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events == []


@needs_aiohttp
def test_a_replay_that_reaches_aiohttp_stops_rather_than_going_live(
        tape_dir, http_server):
    """The whole reason the guard exists."""
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01REC"):
        _fetch(http_server)

    with pytest.raises(TapeError, match="never quietly do the real thing"):
        with tape.session("replay", tape_dir=tape_dir, replay="01REC",
                          collect_git=False):
            _fetch(http_server)


@needs_aiohttp
def test_the_error_names_the_request_and_the_way_out(tape_dir, http_server):
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01REC"):
        pass

    with pytest.raises(TapeError) as caught:
        with tape.session("replay", tape_dir=tape_dir, replay="01REC",
                          collect_git=False):
            _fetch(http_server)
    message = str(caught.value)
    assert "GET" in message and http_server in message
    assert "@tape.tool" in message


@needs_aiohttp
def test_an_aiohttp_call_inside_a_tool_body_passes_through(tape_dir, http_server):
    """The documented way to cover aiohttp, and the boundary rule doing its job:
    the tool's result is what replay needs, not the request inside it."""
    @tape.tool
    def fetch():
        return _fetch(http_server)

    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01REC"):
        assert fetch() == {"ok": True}

    events = tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events
    assert [e.kind for e in events] == ["tool"]

    # And the replay never reaches aiohttp at all, because the body does not run.
    with tape.session("replay", tape_dir=tape_dir, replay="01REC",
                      collect_git=False):
        assert fetch() == {"ok": True}


@needs_aiohttp
def test_uninstalling_puts_aiohttp_back(tape_dir, http_server):
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01REC"):
        pass
    # No tape at all: the patch must be gone, not merely inert.
    assert _fetch(http_server) == {"ok": True}
    assert aiohttp.ClientSession._request.__qualname__.startswith("ClientSession")
