"""The Recorder: everything that crosses a boundary ends up here.

One instance per run. The pipeline for every event is:

1. stamp the call site and span
2. redact  -- before anything touches disk, so a secret cannot leak into a blob
3. externalise oversized fields into the blob store
4. write

Two separate per-thread guards sit on top of that pipeline.

``is_busy`` is reentrancy: a user object whose ``__repr__`` calls ``time.time()``
would otherwise re-enter the recorder from inside step 2 while it is serialising
that very object.

``in_boundary`` is nesting: **the outermost boundary is the one recorded.** An
HTTP call made inside a ``@tape.tool`` body is not recorded separately, and
neither are random draws or clock reads made there. This is not an optimisation
-- on replay the tool's result is served from the tape and its body never runs,
so anything recorded inside it could never be matched, and unmatchable events
are exactly the silent-divergence failure the design forbids.
"""

from __future__ import annotations

import itertools
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Optional, Sequence

from . import _originals, callsite, spans
from .blobs import BlobStore
from .redact import Redactor
from .serial import to_jsonable
from .trace import Event
from .writer import TraceWriter

logger = logging.getLogger("reeltime")

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


def in_boundary() -> bool:
    """True when the current thread is inside a recorded boundary's body."""
    return getattr(_local, "depth", 0) > 0


@contextmanager
def boundary() -> Iterator[None]:
    """Mark a block as the body of a recorded boundary.

    Everything recorded inside is suppressed, because on replay this body does
    not run.
    """
    _local.depth = getattr(_local, "depth", 0) + 1
    try:
        yield
    finally:
        _local.depth -= 1


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

    #: Lets the interception layer branch without importing either class.
    replaying = False

    def __init__(
        self,
        writer: TraceWriter,
        blobs: BlobStore,
        redactor: Redactor,
        *,
        record_library_ambient: bool = False,
        enrich: Optional[Callable[[Event], Optional[Dict[str, Any]]]] = None,
    ) -> None:
        self.writer = writer
        self.blobs = blobs
        self.redactor = redactor
        self.record_library_ambient = record_library_ambient
        #: Optional pure function that recognises an event and adds fields to
        #: it -- see :mod:`reeltime.core.decoders`. Never allowed to fail a
        #: recording.
        self.enrich = enrich
        self._enrich_failed = False
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

    def _normalize(self, payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """JSON-safe and scrubbed, but not yet externalised.

        Enrichment runs between this and the blob store so a decoder reads the
        actual response body rather than a ``blob:`` reference.
        """
        if payload is None:
            return None
        jsonable = to_jsonable(payload)
        if not isinstance(jsonable, dict):  # pragma: no cover - defensive
            jsonable = {"value": jsonable}
        return self.redactor.scrub(jsonable)

    def _apply_enrichment(self, event: Event) -> None:
        """Let a decoder add fields to the event. Never fails the recording."""
        if self.enrich is None:
            return
        try:
            extra = self.enrich(event)
        except Exception:
            if not self._enrich_failed:
                self._enrich_failed = True
                logger.debug(
                    "a decoder raised on event %d; events are being written "
                    "unenriched", event.i, exc_info=True,
                )
            return
        from .decoders import apply as apply_enrichment

        apply_enrichment(event, extra)

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
        are dropped unless they originate in the user's own code: ``asyncio``
        reads ``time.monotonic()`` every loop iteration, ``logging`` timestamps
        every record, and httpx reads ``perf_counter()`` twice per request.
        None of that is the agent's nondeterminism, recording it would bury the
        trace, and the same filter applies on replay -- so those reads simply
        stay live in both directions, which is consistent and matchable. See
        :attr:`record_library_ambient`.

        Returns None when a boundary body is already being recorded above this
        call; see the module docstring.
        """
        if not self.enabled or is_busy() or in_boundary():
            return None

        with _busy():
            when = self.elapsed if t_rel is None else t_rel
            where = callsite.caller(2) if site is None else site
            if ambient and where.is_library and not self.record_library_ambient:
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
                req=self._normalize(req) or {},
                res=self._normalize(res),
                meta=dict(meta or {}),
            )
            self._apply_enrichment(event)
            event.req = self.blobs.externalize(event.req) or {}
            event.res = self.blobs.externalize(event.res)
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
        # A boundary inside another boundary is not its own event: the outer
        # one already stands for this crossing, and on replay this body will
        # not run at all.
        nested = in_boundary()
        site = callsite.caller(3)
        started = _originals.perf_counter()
        t_rel = started - self.t0
        error: Optional[BaseException] = None
        try:
            with boundary():
                yield box
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
            error = exc
            raise
        finally:
            if not nested:
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

    def resolved(self, event: Event, payload: Optional[Dict[str, Any]]) -> Any:
        """Mirror of :meth:`Player.resolved`, so callers need not branch."""
        if payload is None:
            return None
        return self.blobs.resolve(payload)

    # -- teardown --------------------------------------------------------

    def close(
        self,
        exit_code: Optional[int] = None,
        *,
        intercepted: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Write the footer and close the file. Returns the footer.

        ``intercepted`` lists the HTTP backends that were actually patched. It
        answers "why was my call not recorded?" without a second run.
        """
        footer: Dict[str, Any] = self.stats.to_dict()
        if intercepted is not None:
            footer["intercepted"] = list(intercepted)
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
