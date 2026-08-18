"""The Player: replay consumes recorded events instead of writing them.

Mirrors :class:`~reeltime.core.recorder.Recorder`. Every interception point --
the HTTP transport shim, ``@tape.tool``, and the ambient patches -- asks the
active engine for the result of a boundary crossing. Recording performs the
crossing and writes down what happened; replaying looks it up and performs
nothing at all.

Nothing here ever falls through to a live call. If a call cannot be matched the
run stops with a :class:`~reeltime.errors.TapeMiss` naming the call site --
design principle 4, loud failure over silent divergence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import _originals, callsite, spans
from .blobs import BlobStore
from .matching import (
    DEFAULT,
    MAX_TIER,
    DriftRecord,
    MatchIndex,
    Request,
    preview,
)
from .recorder import Capture, in_boundary, is_busy, _busy
from .redact import Redactor
from .serial import to_jsonable
from .trace import Event, Trace
from ..errors import StopReplay, TapeMiss

logger = logging.getLogger("reeltime")


#: One edit shifts every line below it, so a per-event list of an eighty-event
#: run is noise. The count is the signal; a few examples are enough to act on.
DETAIL_LIMIT = 5


def _capped(records: List[DriftRecord]) -> List[str]:
    lines = [record.line() for record in records[:DETAIL_LIMIT]]
    if len(records) > DETAIL_LIMIT:
        lines.append("  … and {} more".format(len(records) - DETAIL_LIMIT))
    return lines


@dataclass
class ReplaySummary:
    """What a replay did, and everything the user needs to distrust it."""

    run_id: str
    events: int = 0
    recorded: int = 0
    dur_s: float = 0.0
    original_dur_s: Optional[float] = None
    drifts: List[DriftRecord] = field(default_factory=list)
    unconsumed: List[Event] = field(default_factory=list)
    stopped_at: Optional[int] = None
    strictness: str = DEFAULT

    @property
    def drifted(self) -> List[DriftRecord]:
        return [d for d in self.drifts if d.tier == 2]

    @property
    def fuzzy(self) -> List[DriftRecord]:
        return [d for d in self.drifts if d.tier == 3]

    @property
    def speedup(self) -> Optional[float]:
        if not self.original_dur_s or not self.dur_s:
            return None
        return self.original_dur_s / self.dur_s

    def line(self) -> str:
        text = "replayed {} event{} in {:.2f}s  ($0.00)".format(
            self.events, "" if self.events == 1 else "s", self.dur_s
        )
        speedup = self.speedup
        if speedup and speedup >= 2:
            text += "  [{:.0f}× faster than the recorded run]".format(speedup)
        return text

    def notes(self) -> List[str]:
        """Everything that was not a clean tier-1 match, stated plainly.

        Printed at the end of every replay. A drifted match that nobody
        mentions is the silent divergence this tool exists to prevent.
        """
        out: List[str] = []
        drifted, fuzzy = self.drifted, self.fuzzy
        if drifted:
            out.append("{} event{} matched with drifted content".format(
                len(drifted), "" if len(drifted) == 1 else "s"))
            out.extend(_capped(drifted))
        if fuzzy:
            out.append("{} event{} matched by content hash alone".format(
                len(fuzzy), "" if len(fuzzy) == 1 else "s"))
            out.extend(_capped(fuzzy))
        if self.stopped_at is not None:
            out.append("stopped after event {} as requested".format(self.stopped_at))
        elif self.unconsumed:
            out.append(
                "{} recorded event{} never requested -- the agent took a "
                "different path".format(
                    len(self.unconsumed), "" if len(self.unconsumed) == 1 else "s")
            )
            out.extend("  #{:<4} {:<5} {:<26} never replayed".format(
                e.i, e.kind, e.site) for e in self.unconsumed[:5])
        return out


class Player:
    """Serves recorded events to a re-running agent."""

    #: Lets the interception layer branch without importing either class.
    replaying = True

    def __init__(
        self,
        trace: Trace,
        blobs: BlobStore,
        redactor: Redactor,
        *,
        strictness: str = DEFAULT,
        stop_at: Optional[int] = None,
        realtime: bool = False,
        record_library_ambient: bool = False,
        stepper: Optional[Callable[[Event, "Player"], None]] = None,
    ) -> None:
        self.trace = trace
        self.blobs = blobs
        self.redactor = redactor
        self.strictness = strictness
        self.stop_at = stop_at
        self.realtime = realtime
        self.record_library_ambient = record_library_ambient
        self.stepper = stepper
        self.index = MatchIndex(trace.events, blobs)
        self.drifts: List[DriftRecord] = []
        self.consumed = 0
        self.enabled = True
        self.t0 = _originals.perf_counter()
        self._stopping = False
        self.stopped_at: Optional[int] = None

    @property
    def elapsed(self) -> float:
        return _originals.perf_counter() - self.t0

    def _normalize(self, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Exactly what the recorder did, so the two hashes are comparable.

        Redaction included: the recorded body says ``<redacted:sk>`` where the
        live one carries a real key, and skipping this would drift every
        request that contains a credential.
        """
        if payload is None:
            return {}
        jsonable = to_jsonable(payload)
        if not isinstance(jsonable, dict):  # pragma: no cover - defensive
            jsonable = {"value": jsonable}
        return self.redactor.scrub(jsonable)

    # -- the one method the interception layer calls ---------------------

    def consume(
        self,
        kind: str,
        req: Optional[Dict[str, Any]] = None,
        *,
        site: Optional[callsite.CallSite] = None,
        span: Optional[str] = None,
        ambient: bool = False,
    ) -> Optional[Event]:
        """The recorded event for this call, or :class:`TapeMiss`.

        Returns None only when this boundary is not ours to serve -- inside
        another boundary's body, or an ambient read made by a library. Those
        are the same cases the recorder skipped, so the two sides stay
        symmetrical and neither produces an event the other cannot match.
        """
        if not self.enabled or is_busy() or in_boundary():
            return None

        with _busy():
            where = callsite.caller(2) if site is None else site
            if ambient and where.is_library and not self.record_library_ambient:
                return None
            if self._stopping:
                raise StopReplay(self.stopped_at or 0)

            request = Request(
                kind=kind,
                site=where.site,
                qual=where.qual,
                span=spans.current() if span is None else span,
                req=self._normalize(req),
            )
            match = self.index.take(request)

            if match is not None and match.tier > MAX_TIER[self.strictness]:
                # Matched, but not well enough for this strictness. Put it back
                # so it shows up in the diagnosis as the near miss it is.
                self.index.release(match.event)
                match = None

            if match is None:
                raise TapeMiss(
                    kind,
                    request.site,
                    span=request.span,
                    qual=request.qual,
                    preview=preview(kind, request.req),
                    candidates=self.index.candidates(request),
                    strictness=self.strictness,
                    remaining=len(self.index.unconsumed()),
                    run_id=self.trace.run_id,
                )

            event = match.event
            self.consumed += 1
            if match.tier > 1:
                self.drifts.append(DriftRecord(
                    index=event.i, kind=event.kind, site=request.site,
                    tier=match.tier, reason=match.reason,
                ))
            if self.stop_at is not None and event.i >= self.stop_at:
                # Deliver this one, then refuse the next: --to N means N ran.
                self._stopping = True
                self.stopped_at = event.i

        if self.stepper is not None:
            self.stepper(event, self)
        return event

    def resolved(self, event: Event, payload: Optional[Dict[str, Any]]) -> Any:
        """A recorded payload with its blob references expanded."""
        if payload is None:
            return None
        return self.blobs.resolve(payload)

    # -- teardown --------------------------------------------------------

    def close(self, exit_code: Optional[int] = None) -> ReplaySummary:
        self.enabled = False
        footer = self.trace.footer or {}
        summary = ReplaySummary(
            run_id=self.trace.run_id,
            events=self.consumed,
            recorded=len(self.trace.events),
            dur_s=round(self.elapsed, 4),
            original_dur_s=footer.get("dur_s"),
            drifts=list(self.drifts),
            unconsumed=self.index.unconsumed(),
            stopped_at=self.stopped_at,
            strictness=self.strictness,
        )
        for note in summary.notes():
            logger.warning("reeltime: %s", note)
        return summary
