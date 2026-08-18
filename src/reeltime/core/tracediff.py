"""Comparing two runs.

Not a text diff. Two traces are aligned by event *signature* -- kind, call
site, and name -- so that an event inserted near the front does not report
everything after it as changed, and then the differences are reported
structurally.

The line that matters most is the last one:

    step 15  ⋯ divergent from here (A: 6 more events, B: 9 more events)

Alignment and field-level reporting are table stakes; naming the step where two
trajectories stop being the same run is the reason to run this at all. So the
divergence point is computed first and everything else is detail hung off it.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import context as context_mod
from .fmt import usd
from .matching import kind_key
from .trace import Event, Trace

SAME = "same"
CHANGED = "changed"
ADDED = "added"
REMOVED = "removed"

#: How much of a value to show before it stops being informative.
VALUE_LIMIT = 70


def signature(event: Event) -> Tuple[str, str, str]:
    """What makes two events "the same step" for alignment purposes.

    ``llm`` folds into ``http``: the label is something a decoder applies after
    the fact, and a run recorded before that decoder existed should still line
    up against one recorded after it.
    """
    return (kind_key(event.kind), event.site, event.name or "")


@dataclass
class Change:
    """One field-level difference within a paired step."""

    label: str
    before: Any = None
    after: Any = None
    #: Pre-rendered lines, for changes that are better shown than described.
    lines: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"label": self.label}
        if self.before is not None or self.after is not None:
            out["before"], out["after"] = self.before, self.after
        if self.lines:
            out["lines"] = self.lines
        return out


@dataclass
class Step:
    """One position in the aligned comparison."""

    kind: str
    a: Optional[Event] = None
    b: Optional[Event] = None
    changes: List[Change] = field(default_factory=list)

    @property
    def event(self) -> Event:
        assert self.a is not None or self.b is not None
        return self.b if self.b is not None else self.a  # type: ignore[return-value]

    @property
    def paired(self) -> bool:
        return self.a is not None and self.b is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "a": self.a.i if self.a else None,
            "b": self.b.i if self.b else None,
            "event_kind": self.event.kind,
            "site": self.event.site,
            "name": self.event.name,
            "changes": [c.to_dict() for c in self.changes],
        }


@dataclass
class TraceDiff:
    """The comparison of two runs."""

    a: Trace
    b: Trace
    steps: List[Step] = field(default_factory=list)
    #: Index into ``steps`` where the two runs stop corresponding at all, or
    #: None when they line up end to end.
    divergence: Optional[int] = None

    @property
    def identical(self) -> bool:
        return all(step.kind == SAME for step in self.steps)

    @property
    def tail(self) -> Tuple[int, int]:
        """How many events each run has after the divergence point."""
        if self.divergence is None:
            return (0, 0)
        rest = self.steps[self.divergence:]
        return (sum(1 for s in rest if s.a is not None),
                sum(1 for s in rest if s.b is not None))

    def counts(self) -> Dict[str, int]:
        out = {SAME: 0, CHANGED: 0, ADDED: 0, REMOVED: 0}
        for step in self.steps:
            out[step.kind] += 1
        return out

    def totals(self) -> Dict[str, Dict[str, Any]]:
        return {"a": _totals(self.a), "b": _totals(self.b)}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "a": self.a.run_id,
            "b": self.b.run_id,
            "identical": self.identical,
            "divergence": self.divergence,
            "tail": {"a": self.tail[0], "b": self.tail[1]},
            "counts": self.counts(),
            "totals": self.totals(),
            "steps": [step.to_dict() for step in self.steps],
        }


def _totals(trace: Trace) -> Dict[str, Any]:
    footer = trace.footer or {}
    tokens = footer.get("tokens") or {}
    return {
        "events": len(trace.events),
        "cost_usd": footer.get("cost_usd"),
        "tokens_in": tokens.get("in", 0),
        "tokens_out": tokens.get("out", 0),
    }


# -- alignment -----------------------------------------------------------


def align(a_events: Sequence[Event], b_events: Sequence[Event]) -> List[Step]:
    """Pair up two event sequences by signature."""
    left = [signature(e) for e in a_events]
    right = [signature(e) for e in b_events]
    steps: List[Step] = []

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        a=left, b=right, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                a, b = a_events[i1 + offset], b_events[j1 + offset]
                changes = describe(a, b)
                steps.append(Step(CHANGED if changes else SAME, a, b, changes))
        elif tag == "delete":
            steps.extend(Step(REMOVED, a=e) for e in a_events[i1:i2])
        elif tag == "insert":
            steps.extend(Step(ADDED, b=e) for e in b_events[j1:j2])
        else:
            # Different signatures at the same position: the agent did
            # something else here. Pair them so the report can show what
            # replaced what, and leave any surplus as added/removed.
            old, new = a_events[i1:i2], b_events[j1:j2]
            for index in range(min(len(old), len(new))):
                a, b = old[index], new[index]
                steps.append(Step(CHANGED, a, b, describe(a, b)))
            steps.extend(Step(REMOVED, a=e) for e in old[len(new):])
            steps.extend(Step(ADDED, b=e) for e in new[len(old):])
    return steps


def find_divergence(steps: Sequence[Step]) -> Optional[int]:
    """Where the two runs stop corresponding at all.

    That is the first index of the final stretch of *unpaired* steps: up to
    there the runs are still doing the same things, however differently, and
    after it one of them simply went on alone.
    """
    divergence = None
    for index in range(len(steps) - 1, -1, -1):
        if steps[index].paired:
            break
        divergence = index
    return divergence


def diff(a: Trace, b: Trace, only: Optional[Sequence[str]] = None) -> TraceDiff:
    """Compare two traces."""
    a_events = _filter(a.events, only)
    b_events = _filter(b.events, only)
    steps = align(a_events, b_events)
    return TraceDiff(a=a, b=b, steps=steps, divergence=find_divergence(steps))


def _filter(events: Sequence[Event], only: Optional[Sequence[str]]) -> List[Event]:
    if not only:
        return list(events)
    wanted = {kind_key(k) for k in only}
    return [e for e in events if kind_key(e.kind) in wanted]


# -- describing one paired step ------------------------------------------


def _short(value: Any, limit: int = VALUE_LIMIT) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe(a: Event, b: Event) -> List[Change]:
    """What changed between two paired events."""
    changes: List[Change] = []

    if a.name != b.name:
        changes.append(Change("call", _call_of(a), _call_of(b)))
    elif kind_key(a.kind) == "tool" and (a.req.get("args") != b.req.get("args")):
        changes.append(Change("arguments", _call_of(a), _call_of(b)))

    if kind_key(a.kind) == "http":
        changes.extend(_llm_changes(a, b))

    for label, before, after in (
        ("result", (a.res or {}).get("value"), (b.res or {}).get("value")),
        ("status", (a.res or {}).get("status"), (b.res or {}).get("status")),
    ):
        if before != after and (before is not None or after is not None):
            changes.append(Change(label, _short(before), _short(after)))

    error_a = (a.meta.get("error") or {}).get("type")
    error_b = (b.meta.get("error") or {}).get("type")
    if error_a != error_b:
        changes.append(Change("error", error_a or "none", error_b or "none"))
    return changes


def _call_of(event: Event) -> str:
    name = event.name or event.kind
    args = event.req.get("args")
    if args is None:
        return name
    if isinstance(args, dict):
        inner = ", ".join("{}={}".format(k, _short(v, 24)) for k, v in args.items())
    else:
        inner = _short(args, 40)
    return "{}({})".format(name, inner)


def _llm_changes(a: Event, b: Event) -> List[Change]:
    """Differences in what the model was asked, using the context view."""
    changes: List[Change] = []
    if a.req.get("model") != b.req.get("model"):
        changes.append(Change("model", a.req.get("model"), b.req.get("model")))

    before = context_mod.from_event(a)
    after = context_mod.from_event(b)
    if before is None or after is None:
        return changes

    for change in context_mod.diff(before, after):
        if change.kind == "same":
            continue
        message = change.after or change.before
        assert message is not None
        if change.kind == "changed" and message.role == "system":
            changes.append(Change(
                "system prompt changed",
                lines=["- " + _short(change.before.text, 100),
                       "+ " + _short(change.after.text, 100)],
            ))
        elif change.kind == "changed":
            note = " (truncated)" if change.truncated else ""
            changes.append(Change(
                "message [{}] {} changed{}".format(message.index, message.role, note),
                "{:,} chars".format(change.before.chars),
                "{:,} chars".format(change.after.chars),
            ))
        elif change.kind == "added":
            changes.append(Change(
                "message [{}] {} injected".format(message.index, message.role),
                after=_short(message.text or message.shape)))
        else:
            changes.append(Change(
                "message [{}] {} dropped".format(message.index, message.role),
                before=_short(message.text or message.shape)))

    if before.tokens.get("in") != after.tokens.get("in"):
        changes.append(Change("tokens in", before.tokens.get("in"),
                              after.tokens.get("in")))
    return changes


# -- rendering -----------------------------------------------------------


def render(result: TraceDiff, glyphs: Optional[context_mod.Glyphs] = None) -> str:
    """The structural report."""
    glyphs = glyphs or context_mod.Glyphs.detect()
    out = ["diff  A {}   B {}".format(result.a.run_id, result.b.run_id), ""]

    if not result.steps:
        out.append("both runs are empty")
        return "\n".join(out) + "\n"

    index = 0
    while index < len(result.steps):
        if result.divergence is not None and index == result.divergence:
            out.append("step {:<3} {} {}".format(
                index, glyphs.ellipsis, _divergence_note(result.tail)))
            break

        step = result.steps[index]
        if step.kind == SAME:
            run_end = index
            while (run_end + 1 < len(result.steps)
                   and result.steps[run_end + 1].kind == SAME
                   and (result.divergence is None or run_end + 1 < result.divergence)):
                run_end += 1
            count = run_end - index + 1
            if count == 1:
                out.append("step {:<3} identical".format(index))
            else:
                out.append("step {}–{}  identical ({} events)".format(
                    index, run_end, count))
            index = run_end + 1
            continue

        out.append("step {:<3} {:<7} {}".format(
            index, step.event.kind, _headline(step, glyphs)))
        for change in step.changes:
            out.extend(_change_lines(change, glyphs))
        index += 1

    totals = result.totals()
    out.append("")
    out.append("cost   A {:<10} B {}".format(
        usd(totals["a"]["cost_usd"]), usd(totals["b"]["cost_usd"])))
    out.append("tokens A {:<10,} B {:,}".format(
        totals["a"]["tokens_in"] + totals["a"]["tokens_out"],
        totals["b"]["tokens_in"] + totals["b"]["tokens_out"]))
    if result.identical and result.divergence is None:
        out.append("")
        out.append("the two runs are identical")
    return "\n".join(out) + "\n"


def _divergence_note(tail: Tuple[int, int]) -> str:
    """How the two runs parted, phrased for whichever way they parted."""
    a_tail, b_tail = tail

    def events(count: int) -> str:
        return "{} more event{}".format(count, "" if count == 1 else "s")

    if a_tail and not b_tail:
        return "divergent from here (A: {}; B ended)".format(events(a_tail))
    if b_tail and not a_tail:
        return "divergent from here (A ended; B: {})".format(events(b_tail))
    return "divergent from here (A: {}, B: {})".format(events(a_tail), events(b_tail))


def _headline(step: Step, glyphs: context_mod.Glyphs) -> str:
    if step.kind == ADDED:
        return "only in B: {}".format(_call_of(step.event))
    if step.kind == REMOVED:
        return "only in A: {}".format(_call_of(step.event))
    if step.changes:
        first = step.changes[0]
        if first.label in ("call", "arguments"):
            return "{}  {}  {}".format(first.before, glyphs.arrow, first.after)
        return first.label
    return "changed"


def _change_lines(change: Change, glyphs: context_mod.Glyphs) -> List[str]:
    pad = " " * 17
    if change.lines:
        return [pad + line for line in change.lines]
    if change.label in ("call", "arguments"):
        return []          # already in the headline
    if change.before is None and change.after is not None:
        return [pad + "{}: {}".format(change.label, change.after)]
    if change.after is None and change.before is not None:
        return [pad + "{}: {}".format(change.label, change.before)]
    return [pad + "{}: {}  {}  {}".format(
        change.label, change.before, glyphs.arrow, change.after)]
