"""The ``tape ui`` server: standard library only, loopback only, read-only.

**No web framework.** ``pyproject.toml`` declares ``dependencies = []`` and the
README says so; that property is why ``pip install reeltime`` cannot break an
environment, and it is not worth spending on a six-screen read-only viewer. So
this is ``http.server`` and one inlined HTML file: no build step, no CDN, and it
works with no network at all -- which matters, because the traces it reads came
from a machine that may be offline.

**Loopback is not a default, it is the design.** Redaction is pattern-matching
and best-effort, so the honest assumption is that a trace may still hold
something private. The bind address is therefore fixed rather than configurable,
and there is a test asserting a non-loopback connection is refused. "No auth" is
correct *because* of that bind, not instead of it.

Every route is a read. There is no write path to audit.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import api

#: Fixed. See the module docstring -- this is the security boundary, so it is
#: not a flag somebody can widen without editing the source and the test.
HOST = "127.0.0.1"
DEFAULT_PORT = 7654

_HERE = Path(__file__).resolve().parent
INDEX = _HERE / "index.html"


class Handler(BaseHTTPRequestHandler):
    """Routes. ``tape_dir`` is attached to the server, not to the request."""

    server_version = "reeltime"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, *args: Any) -> None:
        """Quiet by default; a debugger's terminal is for the debugger."""
        if os.environ.get("REELTIME_UI_LOG"):
            super().log_message(*args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The page loads nothing remote and talks to nobody. Say so, so a
        # stray third-party URL in a trace payload cannot be fetched.
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; img-src data:; "
                         "connect-src 'self'; base-uri 'none'; form-action 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status=status)

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)

        if not parts or parts[0] != "api":
            return self._page()

        try:
            payload = self._dispatch(parts[1:], query)
        except api.ApiError as exc:
            return self._error(exc.status, exc.message)
        except Exception as exc:  # noqa: BLE001 - a viewer must not 500 silently
            return self._error(500, "{}: {}".format(type(exc).__name__, exc))
        if payload is None:
            return self._error(404, "no such endpoint: {}".format(parsed.path))
        self._json(payload)

    do_HEAD = do_GET

    def _dispatch(self, rest: List[str], query: Dict[str, List[str]]
                  ) -> Optional[Dict[str, Any]]:
        tape_dir: Path = self.server.tape_dir  # type: ignore[attr-defined]

        if rest == ["runs"]:
            return api.runs(tape_dir)
        if rest == ["tree"]:
            return api.tree(tape_dir)
        if rest == ["comparable"]:
            return api.comparable(tape_dir)
        if rest == ["boot"]:
            # `explicit` is how the page decides whether to raise the runs
            # overlay: `tape ui <run>` goes straight in, bare `tape ui` opens
            # the newest run with the overlay up, which covers discovery
            # without making a file picker the landing page.
            return {"run_id": self.server.boot_run,  # type: ignore[attr-defined]
                    "explicit": self.server.boot_explicit,  # type: ignore[attr-defined]
                    "tape_dir": str(tape_dir)}
        if len(rest) == 2 and rest[0] == "run":
            return api.run(tape_dir, rest[1])
        if len(rest) == 3 and rest[0] == "run" and rest[2] == "chain":
            return api.chain(tape_dir, rest[1])
        if len(rest) == 4 and rest[0] == "run" and rest[2] == "context":
            baseline = query.get("baseline", [None])[0]
            return api.context(tape_dir, rest[1], _int(rest[3], "event index"),
                               None if baseline in (None, "") else
                               _int(baseline, "baseline"))
        if len(rest) == 3 and rest[0] == "diff":
            only = query.get("only") or None
            return api.diff(tape_dir, rest[1], rest[2], only=only)
        if rest[:1] == ["doctor"]:
            ids = [r for r in query.get("runs", []) if r]
            if len(ids) == 1 and "," in ids[0]:
                ids = ids[0].split(",")
            return api.doctor(tape_dir, ids)
        return None

    def _page(self) -> None:
        try:
            body = INDEX.read_bytes()
        except OSError:
            # Only reachable from a broken install, and the wheel gate has a
            # test for exactly that -- so say which file rather than 500.
            return self._error(500, "the UI page is missing from this install "
                                    "({})".format(INDEX))
        self._send(200, body, "text/html; charset=utf-8")


def _int(value: str, what: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise api.ApiError(400, "{} must be a number, got {!r}".format(what, value))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Any, tape_dir: Path, boot_run: Optional[str],
                 boot_explicit: bool = False) -> None:
        super().__init__(address, Handler)
        self.tape_dir = tape_dir
        self.boot_run = boot_run
        self.boot_explicit = boot_explicit


def build(tape_dir: Path, boot_run: Optional[str] = None,
          port: int = DEFAULT_PORT, boot_explicit: bool = False) -> Server:
    """A server bound to loopback. Port 0 asks the OS for a free one."""
    return Server((HOST, port), Path(tape_dir), boot_run, boot_explicit)


def serve(tape_dir: Path, boot_run: Optional[str] = None,
          port: int = DEFAULT_PORT,
          on_ready: Optional[Callable[[str], None]] = None,
          boot_explicit: bool = False) -> None:
    """Run until interrupted."""
    httpd = build(tape_dir, boot_run, port, boot_explicit)
    url = "http://{}:{}/".format(HOST, httpd.server_address[1])
    if on_ready is not None:
        on_ready(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()


def serve_in_thread(tape_dir: Path, boot_run: Optional[str] = None,
                    port: int = 0) -> Any:
    """A running server plus its URL, for tests. Caller closes it."""
    httpd = build(tape_dir, boot_run, port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, "http://{}:{}".format(HOST, httpd.server_address[1])
