"""Finding out what is actually nondeterministic about a run.

``tape doctor`` records the same command twice and compares the traces. Where
two runs crossed the same boundary and got different answers, that boundary is
a nondeterminism source, and the trace already knows where it is in the user's
code.

This is worth having even for someone who never replays anything. "Your agent
is flaky" is a feeling; ``agent.py:88 llm — 2 of 2 completions differed
(temperature 0.7)`` is a line you can act on.

Two design choices carry most of the value:

**A source is a call site, not an event.** An agent in a loop reads the clock
forty times; reporting forty findings buries the two that matter. Findings are
grouped by ``(site, kind, name)`` and counted, so the report is as long as the
number of distinct places the run is nondeterministic.

**The path split is reported separately, and first.** Once two runs stop making
the same calls, everything after is incomparable rather than divergent -- so
the step where they split is the headline, and the sources above it are the
candidates for having caused it.

The analysis is a pure function over traces, so it is testable without running
anything twice; the CLI owns the two subprocesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .blobs import is_ref
from .tracediff import align, signature
from .trace import Event, Trace

#: How many distinct values to keep as evidence for one source.
MAX_SAMPLES = 3

#: Rendered value length before it stops being useful on one line.
VALUE_LIMIT = 48


def _short(value: Any, limit: int = VALUE_LIMIT) -> str:
    import json

    if value is None:
        return "—"
    if is_ref(value):
        return "<{}…>".format(value[5:17])
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def outcome(event: Event) -> Any:
    """What this boundary answered -- the thing two runs can disagree about.

    Deliberately the *result*, never the request. A prompt that differs between
    runs is a consequence of some earlier source, not a source itself, and
    reporting it as one sends the user to the wrong line.
    """
    res = event.res or {}
    if event.kind == "chain":
        # A LangChain node is structure, not a boundary: its outputs are what
        # the model calls underneath it produced, so reporting them as a source
        # points at the node that carried the difference rather than the one
        # that caused it. A node that *moved* is a real signal, and alignment
        # already reports that as a path split.
        return None
    if event.kind in ("rand", "time", "uuid", "tool", "mcp"):
        return res.get("value")
    if event.kind in ("llm", "http"):
        # The body is authoritative and may be a blob reference. Comparing the
        # references is enough: two runs whose bodies hash the same recorded
        # the same bytes, and neither has to be read to find that out.
        return (res.get("status"), res.get("body"), res.get("chunks"))
    return res or None


def display(event: Event) -> str:
    """A short rendering of the outcome, for the evidence lines."""
    res = event.res or {}
    if event.kind == "llm" and res.get("preview"):
        return _short(res.get("preview"))
    if event.kind in ("llm", "http"):
        body = res.get("body")
        if isinstance(body, dict) and "json" in body:
            return _short(body["json"])
        if isinstance(body, dict) and "text" in body:
            return _short(body["text"])
        return _short(body)
    return _short(res.get("value"))


def request_param(event: Event, name: str) -> Any:
    """A named parameter off a request, wherever the shape keeps it."""
    req = event.req or {}
    if name in req:
        return req[name]
    body = req.get("body")
    if isinstance(body, dict):
        inner = body.get("json")
        if isinstance(inner, dict) and name in inner:
            return inner[name]
    return None


@dataclass
class Source:
    """One place in the user's code where two runs disagreed."""

    site: str
    kind: str
    name: Optional[str] = None
    qual: Optional[str] = None
    #: How many aligned crossings at this site disagreed, out of how many.
    diverged: int = 0
    observed: int = 0
    #: Distinct rendered outcomes, as evidence.
    samples: List[str] = field(default_factory=list)
    #: Kind-specific context, e.g. the temperature an LLM call was made at.
    note: Optional[str] = None

    @property
    def label(self) -> str:
        return "{}·{}".format(self.kind, self.name) if self.name else self.kind

    #: What one crossing of this kind of boundary is called, in the report.
    UNITS = {"rand": "read", "time": "read", "uuid": "read",
             "llm": "completion", "http": "response",
             "tool": "call", "mcp": "call"}

    def detail(self) -> str:
        unit = self.UNITS.get(self.kind, "call")
        text = "{} of {} {}{} differed".format(
            self.diverged, self.observed, unit,
            "" if self.observed == 1 else "s")
        if self.note:
            text += " ({})".format(self.note)
        return text

    def suggestion(self) -> str:
        return SUGGESTIONS[self.kind](self)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "site": self.site, "kind": self.kind, "name": self.name,
            "qual": self.qual, "diverged": self.diverged,
            "observed": self.observed, "samples": list(self.samples),
            "note": self.note, "detail": self.detail(),
            "suggestion": self.suggestion(),
        }


def _llm_suggestion(source: "Source") -> str:
    if source.note and not source.note.endswith("0") and "temperature" in source.note:
        return ("set temperature=0 for the closest thing to a reproducible run — "
                "though most providers still do not promise identical completions, "
                "which is the reason replay exists")
    return ("the provider returned different completions with sampling already off; "
            "check the request for something that changes between runs before "
            "blaming the model")


SUGGESTIONS = {
    "rand": lambda s: ("seed the RNG at startup (random.seed(...)) if you want two "
                       "live runs to agree; reeltime records these either way, so "
                       "replay does not need it"),
    "time": lambda s: ("inject a clock instead of calling time.time() or "
                       "datetime.now() in the agent, so a test can hold it still"),
    "uuid": lambda s: ("pass ids in from the caller instead of minting them "
                       "mid-run, or accept that they differ and do not key on them"),
    "llm": _llm_suggestion,
    "http": lambda s: ("an upstream response changed between runs; stub it or pin "
                       "the version if the agent's behaviour depends on it"),
    "tool": lambda s: ("this tool returns something different for the same "
                       "arguments — make the varying part an argument, or leave it "
                       "as a boundary and let replay hold it still"),
    "mcp": lambda s: ("the MCP server answered differently for the same arguments; "
                      "if it should not have, the server is the thing to look at"),
}


@dataclass
class Split:
    """Where the runs stopped making the same calls."""

    step: int
    a: Optional[Event] = None
    b: Optional[Event] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "a": None if self.a is None else {"kind": self.a.kind, "site": self.a.site,
                                              "name": self.a.name},
            "b": None if self.b is None else {"kind": self.b.kind, "site": self.b.site,
                                              "name": self.b.name},
        }


@dataclass
class Report:
    """What two or more runs of the same command disagreed about."""

    run_ids: List[str] = field(default_factory=list)
    event_counts: List[int] = field(default_factory=list)
    sources: List[Source] = field(default_factory=list)
    split: Optional[Split] = None
    #: Paired steps whose *request* differed -- how far a source spread.
    propagated: int = 0
    compared: int = 0

    @property
    def clean(self) -> bool:
        return not self.sources and self.split is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runs": list(self.run_ids),
            "events": list(self.event_counts),
            "compared": self.compared,
            "clean": self.clean,
            "propagated": self.propagated,
            "split": None if self.split is None else self.split.to_dict(),
            "sources": [s.to_dict() for s in self.sources],
        }


def analyse(traces: Sequence[Trace]) -> Report:
    """Compare two or more traces of the same command.

    Every trace is compared against the first, so three runs report a source
    seen in any of them rather than only in the pair that happened to be
    adjacent.
    """
    if len(traces) < 2:
        raise ValueError("doctor needs at least two runs to compare")

    report = Report(
        run_ids=[t.run_id for t in traces],
        event_counts=[len(t.events) for t in traces],
    )
    found: Dict[Tuple[str, str, str], Source] = {}
    baseline = traces[0]

    for other in traces[1:]:
        steps = align(baseline.events, other.events)
        report.compared = max(report.compared, sum(1 for s in steps if s.paired))

        for index, step in enumerate(steps):
            # `align` pairs unlike events at the same position so that a diff
            # can show what replaced what. For doctor that pairing is the
            # opposite of a source: the runs called *different things* here,
            # which is a split, and reading it as "this boundary answered
            # differently" blames whichever tool happened to sort first.
            if not step.paired or signature(step.a) != signature(step.b):
                # Everything after is incomparable rather than different, so
                # record the first such position and stop attributing sources
                # from this pairing.
                if report.split is None or index < report.split.step:
                    report.split = Split(step=index, a=step.a, b=step.b)
                break

            assert step.a is not None and step.b is not None
            if step.a.req != step.b.req:
                report.propagated += 1

            key = (step.a.site, step.a.kind, step.a.name or "")
            source = found.get(key)
            if source is None:
                source = Source(site=step.a.site, kind=step.a.kind,
                                name=step.a.name, qual=step.a.qual,
                                note=_note(step.a))
                found[key] = source
            source.observed += 1
            if outcome(step.a) == outcome(step.b):
                continue
            source.diverged += 1
            for event in (step.a, step.b):
                rendered = display(event)
                if rendered not in source.samples and len(source.samples) < MAX_SAMPLES:
                    source.samples.append(rendered)

    report.sources = _rank([s for s in found.values() if s.diverged])
    return report


def _note(event: Event) -> Optional[str]:
    """Kind-specific context worth carrying into the report."""
    if event.kind != "llm":
        return None
    temperature = request_param(event, "temperature")
    model = request_param(event, "model") or event.req.get("model")
    parts = []
    if model:
        parts.append(str(model))
    if temperature is not None:
        parts.append("temperature {}".format(temperature))
    return ", ".join(parts) or None


#: Reported in this order. An `llm` or `tool` boundary that answers differently
#: changes what the agent does next; a clock read usually does not, and putting
#: forty of them above the one that matters is how a report stops being read.
KIND_ORDER = ("llm", "tool", "mcp", "http", "rand", "uuid", "time")


def _rank(sources: Sequence[Source]) -> List[Source]:
    def key(source: Source):
        try:
            rank = KIND_ORDER.index(source.kind)
        except ValueError:  # pragma: no cover - a kind added without a rank
            rank = len(KIND_ORDER)
        return (rank, -source.diverged, source.site)

    return sorted(sources, key=key)


# -- rendering -----------------------------------------------------------


def render(report: Report, glyphs: Any = None) -> str:
    """The report, written to be acted on rather than admired."""
    from . import context as context_mod

    glyphs = glyphs or context_mod.Glyphs.detect()
    out = ["doctor  {} runs of the same command  ({})".format(
        len(report.run_ids), ", ".join(r[:14] for r in report.run_ids)), ""]

    if report.clean:
        out.append("{} no nondeterminism found across {} compared event{}".format(
            glyphs.same, report.compared, "" if report.compared == 1 else "s"))
        out.append("")
        out.append("Two runs crossed every boundary the same way. That is a")
        out.append("property of this command on this day, not a guarantee —")
        out.append("`tape doctor --runs 5` looks harder.")
        return "\n".join(out) + "\n"

    count = len(report.sources)
    if count:
        out.append("{} {} nondeterminism source{} found".format(
            glyphs.changed, count, "" if count == 1 else "s"))
    else:
        # A split with no source above it: the runs diverged somewhere reeltime
        # is not watching. Saying "0 sources found" would read as a clean bill.
        out.append("{} the runs diverged, but not at any boundary reeltime "
                   "records".format(glyphs.changed))
    out.append("")

    if report.sources:
        site_width = min(max([len(s.site) for s in report.sources] + [12]), 34)
        label_width = min(max(len(s.label) for s in report.sources), 20)
        indent = " " * (2 + site_width + 2 + label_width + 1)
        for source in report.sources:
            out.append("  {:<{sw}}  {:<{lw}} {}".format(
                _truncate_left(source.site, site_width), source.label,
                source.detail(), sw=site_width, lw=label_width))
            if source.samples:
                out.append(indent + " {} ".format(glyphs.arrow).join(source.samples))

    if report.split is not None:
        out.append("")
        out.append("  {} the runs stopped making the same calls at step {}".format(
            glyphs.ellipsis, report.split.step))
        out.append("     {}".format(_split_note(report.split)))
        out.append("     Everything after that is incomparable, not divergent.")
        if report.sources:
            out.append("     Fix the sources above and the split usually goes "
                       "with them.")
        else:
            out.append("     No recorded boundary disagreed, so the branch was "
                       "decided by something")
            out.append("     reeltime does not see — unpatched randomness, "
                       "an env var, the filesystem.")
    elif report.propagated:
        out.append("")
        out.append("  {} {} later step{} reached with a different request".format(
            glyphs.ellipsis, report.propagated,
            " was" if report.propagated == 1 else "s were"))
        out.append("     — the same calls, made with what the sources above produced.")

    if not report.sources:
        return "\n".join(out) + "\n"

    out.append("")
    out.append("suggestions:")
    seen = set()
    for source in report.sources:
        text = source.suggestion()
        if text in seen:
            continue
        seen.add(text)
        out.extend(_wrap("  {}: ".format(source.label), text))
    return "\n".join(out) + "\n"


def _split_note(split: Split) -> str:
    def describe(event: Optional[Event], run: str) -> str:
        if event is None:
            return "{} had nothing here".format(run)
        what = "{}·{}".format(event.kind, event.name) if event.name else event.kind
        return "{} called {} at {}".format(run, what, event.site)

    return "{}; {}".format(describe(split.a, "run 1"), describe(split.b, "run 2"))


def _truncate_left(text: str, width: int) -> str:
    """Keep the end of a path: the file and line are what you click."""
    return text if len(text) <= width else "…" + text[-(width - 1):]


def _wrap(prefix: str, text: str, width: int = 78) -> List[str]:
    import textwrap

    return textwrap.wrap(prefix + text, width=width,
                         subsequent_indent=" " * len(prefix)) or [prefix]
