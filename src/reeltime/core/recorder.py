"""The Recorder: everything that crosses a boundary ends up here.

One instance per run. The pipeline for every event is:

1. stamp the call site and span
2. redact  -- before anything touches disk, so a secret cannot leak into a blob
3. externalise oversized fields into the blob store
4. write

Reentrancy is guarded per-thread. A user object whose ``__repr__`` calls
``time.time()`` would otherwise re-enter the recorder from inside step 2 while
it is serialising that very object.
"""

from __future__ import annotations

import itertools
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional

from . import _originals, callsite, spans
from .blobs import BlobStore
from .redact import Redactor
from .serial import to_jsonable
from .trace import Event
from .writer import TraceWriter

_local = threading.local()


def is_busy() -> bool:
    """True when the current thread is already inside the recorder."""
    return getattr(_local, "busy", False)


@contextmanager
def _busy() -> Iterator[None]:
    _local.busy = True
    try:
        yield
    finally:
        _local.busy = False


@dataclass
class RunStats:
    """Running totals, used for the end-of-run summary line."""

    events: int = 0
    kinds: Dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    errors: int = 0

    def add(self, event: Event) -> None:
        self.events += 1
        self.kinds[event.kind] = self.kinds.get(event.kind, 0) + 1
        cost = event.meta.get("cost_usd")
        if isinstance(cost, (int, float)):
            self.cost_usd += float(cost)
        if event.meta.get("error"):
            self.errors += 1
        tokens = (event.res or {}).get("tokens")
        if isinstance(tokens, dict):
            for key, target in (("in", "tokens_in"), ("out", "tokens_out")):
                value = tokens.get(key)
                if isinstance(value, int):
                    setattr(self, target, getattr(self, target) + value)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "events": self.events,
            "kinds": dict(sorted(self.kinds.items())),
            "cost_usd": round(self.cost_usd, 6),
            "tokens": {"in": self.tokens_in, "out": self.tokens_out},
            "errors": self.errors,
        }


class Capture:
    """Mutable box handed out by :meth:`Recorder.capture`."""

    __slots__ = ("res", "meta")

    def __init__(self) -> None:
        self.res: Optional[Dict[str, Any]] = None
        self.meta: Dict[str, Any] = {}


class Recorder:
    """Writes events for one run."""

    def __init__(
        self,
        writer: TraceWriter,
        blobs: BlobStore,
        redactor: Redactor,
        *,
        record_stdlib_ambient: bool = False,
    ) -> None:
        self.writer = writer
        self.blobs = blobs
        self.redactor = redactor
        self.record_stdlib_ambient = record_stdlib_ambient
        self.stats = RunStats()
        self.t0 = _originals.perf_counter()
        self.enabled = True
        # itertools.count().__next__ is atomic under the GIL, so indices are
        # unique without a lock even when several threads record at once.
        self._counter = itertools.count()

    # -- internals -------------------------------------------------------

    @property
    def elapsed(self) -> float:
        return _originals.perf_counter() - self.t0

    def _prepare(self, payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if payload is None:
            return None
        jsonable = to_jsonable(payload)
        if not isinstance(jsonable, dict):  # pragma: no cover - defensive
            jsonable = {"value": jsonable}
        scrubbed = self.redactor.scrub(jsonable)
        return self.blobs.externalize(scrubbed)

    # -- recording -------------------------------------------------------

    def record(
        self,
        kind: str,
        req: Optional[Dict[str, Any]] = None,
        res: Optional[Dict[str, Any]] = None,
        *,
        meta: Optional[Dict[str, Any]] = None,
        site: Optional[callsite.CallSite] = None,
        span: Optional[str] = None,
        t_rel: Optional[float] = None,
        dur_ms: float = 0.0,
        ambient: bool = False,
    ) -> Optional[Event]:
        """Write one event. Returns None when the event was skipped.

        ``ambient`` marks the implicitly-patched sources (rand/time/uuid). Those
        are dropped when they originate inside the standard library, because a
        replayed ``asyncio`` event loop reading its own clock is neither
        matchable nor useful -- see :attr:`record_stdlib_ambient`.
        """
        if not self.enabled or is_busy():
            return None

        with _busy():
            when = self.elapsed if t_rel is None else t_rel
            where = callsite.caller(2) if site is None else site
            if ambient and where.is_stdlib and not self.record_stdlib_ambient:
                return None

            index = next(self._counter)
            event = Event(
                i=index,
                kind=kind,
                site=where.site,
                qual=where.qual,
                span=spans.current() if span is None else span,
                t_rel=round(when, 6),
                dur_ms=round(dur_ms, 3),
                req=self._prepare(req) or {},
                res=self._prepare(res),
                meta=dict(meta or {}),
            )
            self.stats.add(event)
            self.writer.write_event(event)
            return event

    @contextmanager
    def capture(
        self,
        kind: str,
        req: Optional[Dict[str, Any]] = None,
        *,
        meta: Optional[Dict[str, Any]] = None,
        span: Optional[str] = None,
    ) -> Iterator[Capture]:
        """Time a block and record it, including when it raises.

        ::

            with recorder.capture("tool", {"name": "read_file"}) as cap:
                cap.res = {"value": read_file(path)}

        A failing call is still a boundary crossing -- the agent saw that
        exception and acted on it -- so it is recorded with ``meta.error``.
        """
        box = Capture()
        if not self.enabled or is_busy():
            yield box
            return
        site = callsite.caller(3)
        started = _originals.perf_counter()
        t_rel = started - self.t0
        error: Optional[BaseException] = None
        try:
            yield box
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
            error = exc
            raise
        finally:
            info = dict(meta or {})
            info.update(box.meta)
            if error is not None:
                info["error"] = {
                    "type": type(error).__name__,
                    "message": str(error)[:500],
                }
            self.record(
                kind,
                req,
                box.res,
                meta=info,
                site=site,
                span=span,
                t_rel=t_rel,
                dur_ms=(_originals.perf_counter() - started) * 1000.0,
            )

    # -- teardown --------------------------------------------------------

    def close(self, exit_code: Optional[int] = None) -> Dict[str, Any]:
        """Write the footer and close the file. Returns the footer."""
        footer: Dict[str, Any] = self.stats.to_dict()
        footer["dur_s"] = round(self.elapsed, 3)
        footer["exit"] = exit_code
        redacted = self.redactor.hits
        if redacted:
            footer["redacted"] = dict(sorted(redacted.items()))
        footer["blobs"] = {
            "written": self.blobs.bytes_written,
            "deduped": self.blobs.bytes_deduped,
        }
        self.enabled = False
        self.writer.write_footer(footer)
        self.writer.close()
        return footer
