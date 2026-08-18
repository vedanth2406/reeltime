"""Re-running the decoders over a trace that is already on disk.

A decoder is a pure function of a recorded event, which means a decoder written
today can enrich a run recorded months ago -- adding the model, the token
counts, and the cost to events written before that provider was understood.
This is what makes that property real rather than theoretical.

Rewriting a recording is not something to do casually, so the rewrite is atomic
(temp file, then rename) and ``dry_run`` reports exactly what would change
without touching anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .blobs import BlobStore
from .decoders import apply as apply_enrichment
from .decoders import decode_resolved
from .fmt import usd
from .trace import Event, Trace, dumps, read_trace


@dataclass
class ReindexResult:
    """What a reindex did, or would do."""

    run_id: str
    path: Path
    events: int = 0
    enriched: int = 0
    relabelled: List[str] = field(default_factory=list)
    cost_before: Optional[float] = None
    cost_after: Optional[float] = None
    tokens_before: int = 0
    tokens_after: int = 0
    dry_run: bool = False

    def line(self) -> str:
        verb = "would enrich" if self.dry_run else "enriched"
        return "{} {} of {} event{}".format(
            verb, self.enriched, self.events, "" if self.events == 1 else "s")

    def notes(self) -> List[str]:
        out: List[str] = []
        if self.relabelled:
            out.append("relabelled {}".format(", ".join(self.relabelled)))
        if (self.cost_before or 0) != (self.cost_after or 0):
            out.append("cost {} -> {}".format(usd(self.cost_before), usd(self.cost_after)))
        if self.tokens_before != self.tokens_after:
            out.append("tokens {:,} -> {:,}".format(self.tokens_before, self.tokens_after))
        if not self.enriched:
            out.append("nothing to add: every event was already understood")
        return out


def _totals(events: List[Event]) -> Dict[str, Any]:
    """Footer figures, recomputed from the events themselves."""
    cost = 0.0
    tokens_in = tokens_out = 0
    kinds: Dict[str, int] = {}
    for event in events:
        kinds[event.kind] = kinds.get(event.kind, 0) + 1
        value = event.meta.get("cost_usd")
        if isinstance(value, (int, float)):
            cost += float(value)
        counts = (event.res or {}).get("tokens")
        if isinstance(counts, dict):
            tokens_in += counts.get("in") or 0
            tokens_out += counts.get("out") or 0
    return {
        "cost_usd": round(cost, 6),
        "tokens": {"in": tokens_in, "out": tokens_out},
        "kinds": dict(sorted(kinds.items())),
    }


def reindex(
    path: os.PathLike, blobs: BlobStore, *, dry_run: bool = False
) -> ReindexResult:
    """Re-decode every event in ``path``, rewriting the trace unless ``dry_run``."""
    path = Path(path)
    trace = read_trace(path)
    before = _totals(trace.events)

    result = ReindexResult(
        run_id=trace.run_id,
        path=path,
        events=len(trace.events),
        cost_before=before["cost_usd"],
        tokens_before=before["tokens"]["in"] + before["tokens"]["out"],
        dry_run=dry_run,
    )

    for event in trace.events:
        was = event.kind
        try:
            extra = decode_resolved(event, blobs)
        except Exception:
            # A decoder must never damage an existing recording.
            continue
        if apply_enrichment(event, extra):
            result.enriched += 1
            if event.kind != was:
                result.relabelled.append("#{} {} -> {}".format(event.i, was, event.kind))

    after = _totals(trace.events)
    result.cost_after = after["cost_usd"]
    result.tokens_after = after["tokens"]["in"] + after["tokens"]["out"]

    if not dry_run and result.enriched:
        _rewrite(path, trace, after)
    return result


def _rewrite(path: Path, trace: Trace, totals: Dict[str, Any]) -> None:
    """Replace the trace atomically, so a crash cannot leave a torn file."""
    footer = dict(trace.footer or {})
    if footer:
        footer.update(totals)
        footer["reindexed"] = True

    lines = [dumps(trace.header.to_dict())]
    lines.extend(dumps(event.to_dict()) for event in trace.events)
    if footer:
        lines.append(dumps(dict(footer, end=True)))

    tmp = path.with_name(path.name + ".{}.tmp".format(os.getpid()))
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
