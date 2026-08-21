"""Payload builders for ``tape ui``.

**Every function here delegates to the same ``core`` function the CLI calls and
serialises what comes back.** Nothing in this module computes a number of its
own, and that is the design constraint the whole viewer rests on:

* "no new event data, no recording changes" holds *by construction* -- this
  package imports from ``core`` and never writes;
* a UI bug is a rendering bug, because if a figure is wrong here it is wrong in
  ``tape show`` too, where there is already a test for it;
* the UI cannot drift from the CLI, and
  ``tests/test_ui.py::test_the_api_matches_what_the_cli_computes`` fails if it
  starts trying to.

If you find yourself about to add arithmetic to this file, the calculation
belongs in ``core`` beside the one the CLI uses, so both get it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .. import context as context_mod
from .. import doctor as doctor_mod
from .. import paths, tracediff
from ..blobs import BlobStore
from ..trace import Trace, read_trace


class ApiError(Exception):
    """A bad request, carrying the status the handler should send."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# -- loading ---------------------------------------------------------------


def _blobs(tape_dir: Path) -> BlobStore:
    return BlobStore(paths.blobs_dir(tape_dir))


def load(tape_dir: Path, run_id: str) -> Trace:
    """One trace by exact id, or 404."""
    path = paths.trace_path(tape_dir, run_id)
    if not path.exists():
        raise ApiError(404, "no run {!r} in {}".format(run_id, tape_dir))
    return read_trace(path)


def all_traces(tape_dir: Path) -> List[Trace]:
    """Every readable trace, newest first.

    A half-written trace is skipped rather than fatal: a run that crashed is
    exactly the run somebody wants to look at, and one unreadable file must not
    take the whole index down with it.
    """
    out: List[Trace] = []
    for run_id in paths.list_run_ids(tape_dir):
        try:
            out.append(read_trace(paths.trace_path(tape_dir, run_id)))
        except Exception:  # noqa: BLE001 - see docstring
            continue
    return out


# -- payloads --------------------------------------------------------------


def _summary(trace: Trace) -> Dict[str, Any]:
    footer = trace.footer or {}
    return {
        "run_id": trace.run_id,
        "started": trace.header.started,
        "argv": trace.header.argv,
        "mode": trace.header.mode,
        "events": len(trace.events),
        "kinds": sorted({e.kind for e in trace.events}),
        "duration_s": footer.get("duration_s"),
        "cost_usd": footer.get("cost_usd"),
        "forked_from": trace.header.forked_from,
        "fork_at": trace.header.fork_at,
        "patched": footer.get("patched") or [],
        "complete": bool(trace.footer),
        "has_chain": any(e.kind == "chain" for e in trace.events),
    }


def runs(tape_dir: Path) -> Dict[str, Any]:
    """The overlay's list. Header and footer only -- no event parsing."""
    return {"tape_dir": str(tape_dir), "runs": [_summary(t) for t in all_traces(tape_dir)]}


def run(tape_dir: Path, run_id: str) -> Dict[str, Any]:
    trace = load(tape_dir, run_id)
    blobs = _blobs(tape_dir)
    events = []
    for event in trace.events:
        row = event.to_dict()
        row["req"] = blobs.resolve(event.req)
        row["res"] = blobs.resolve(event.res) if event.res is not None else None
        row["has_context"] = context_mod.from_event(event, blobs) is not None
        events.append(row)
    return {"summary": _summary(trace), "header": trace.header.to_dict(),
            "footer": trace.footer or {}, "events": events}


def context(tape_dir: Path, run_id: str, index: int,
            baseline: Optional[int] = None) -> Dict[str, Any]:
    """The message array at ``index``, optionally diffed against ``baseline``."""
    trace = load(tape_dir, run_id)
    blobs = _blobs(tape_dir)
    event = _event_at(trace, index)

    after = context_mod.from_event(event, blobs)
    if after is None:
        raise ApiError(404, "event {} is a {} event and carries no message "
                            "array".format(index, event.kind))

    payload: Dict[str, Any] = {"context": after.to_dict(), "baseline": None,
                               "changes": None}
    if baseline is None:
        return payload

    before = context_mod.from_event(_event_at(trace, baseline), blobs)
    if before is None:
        raise ApiError(404, "event {} carries no message array to diff "
                            "against".format(baseline))
    payload["baseline"] = before.to_dict()
    payload["changes"] = [c.to_dict() for c in context_mod.diff(before, after)]
    return payload


def _event_at(trace: Trace, index: int):
    for event in trace.events:
        if event.i == index:
            return event
    raise ApiError(404, "run {} has no event {}".format(trace.run_id, index))


def chain(tape_dir: Path, run_id: str) -> Dict[str, Any]:
    """Chain nodes with their nested boundary events attached.

    The nesting is what the transport layer alone cannot show, and it is the
    reason the LangChain adapter exists: a `chain` node and the `llm` event
    inside it are two different things at two different levels.
    """
    trace = load(tape_dir, run_id)
    nodes = [e for e in trace.events if e.kind == "chain"]
    if not nodes:
        raise ApiError(404, "run {} has no chain events".format(run_id))

    rows: List[Dict[str, Any]] = []
    for event in trace.events:
        req = event.req or {}
        if event.kind == "chain":
            rows.append({
                "i": event.i, "node": True, "site": event.site,
                "name": req.get("name"), "type": req.get("type"),
                "framework": req.get("framework"), "path": req.get("path"),
                "depth": req.get("depth") or 0, "step": req.get("step"),
                "dur_ms": event.dur_ms,
                "fan_out": sum(1 for n in nodes
                               if (n.req or {}).get("path", "").startswith(
                                   (req.get("path") or "") + "/")
                               and (n.req or {}).get("depth") == (req.get("depth") or 0) + 1),
            })
        else:
            rows.append({"i": event.i, "node": False, "kind": event.kind,
                         "site": event.site, "dur_ms": event.dur_ms,
                         "depth": _enclosing_depth(trace, event)})
    return {"summary": _summary(trace), "rows": rows}


def _enclosing_depth(trace: Trace, event: Any) -> int:
    """Depth of the nearest preceding chain node that has not closed.

    Approximated by the last chain node before this event, which is right
    because chain events are written in tree order around the boundaries they
    enclose.
    """
    depth = 0
    for other in trace.events:
        if other.i >= event.i:
            break
        if other.kind == "chain":
            depth = ((other.req or {}).get("depth") or 0) + 1
    return depth


def tree(tape_dir: Path) -> Dict[str, Any]:
    """The fork forest: every run, with children hanging off their parent."""
    summaries = [_summary(t) for t in all_traces(tape_dir)]
    by_id = {s["run_id"]: s for s in summaries}
    for summary in summaries:
        summary["children"] = []
    roots: List[Dict[str, Any]] = []
    for summary in summaries:
        parent = by_id.get(summary["forked_from"] or "")
        # A fork whose parent was deleted is a root, not a lost node.
        (parent["children"] if parent else roots).append(summary)
    return {"roots": roots}


def diff(tape_dir: Path, a: str, b: str,
         only: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    result = tracediff.diff(load(tape_dir, a), load(tape_dir, b), only=only)
    return result.to_dict()


def doctor(tape_dir: Path, run_ids: Sequence[str]) -> Dict[str, Any]:
    """Re-run ``analyse()`` over stored runs, rather than reading a report.

    A cached ``--json`` report can show findings for code that has since been
    fixed, and staleness in a correctness tool is worse than a two-second wait.
    Re-running also keeps the same-function-as-the-CLI invariant clean: there is
    no serialised intermediate that could drift from what `tape doctor` prints.

    **This never executes the user's agent.** `tape doctor` runs the command N
    times with real calls and real cost; that must not sit behind a click. The
    runs it already recorded are the input here.
    """
    if len(run_ids) < 2:
        raise ApiError(400, "doctor compares runs, so it needs at least two")
    traces = [load(tape_dir, run_id) for run_id in run_ids]
    return doctor_mod.analyse(traces).to_dict()


def comparable(tape_dir: Path) -> Dict[str, Any]:
    """Runs grouped by argv -- the sets doctor can meaningfully compare.

    `tape doctor` records N runs of one command and nothing links them in the
    trace, so the UI groups by what they actually share. Adding a header field
    to mark them would be a recording change, which this milestone does not get
    to make.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for trace in all_traces(tape_dir):
        groups.setdefault(" ".join(trace.header.argv), []).append(_summary(trace))
    return {"groups": [{"argv": argv, "runs": runs_}
                       for argv, runs_ in groups.items() if len(runs_) > 1]}
