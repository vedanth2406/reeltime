"""Matching a live call against a recorded event.

This is the hardest part of the tool and the part that decides whether it is
useful. Two naive strategies both fail in practice:

* **Index matching** breaks the moment anyone edits their code -- insert one
  import at the top of a file and every subsequent event misaligns.
* **Content-hash matching** breaks the moment a prompt changes by one
  character, which is precisely the edit a user makes when debugging. A tool
  that refuses to replay after a prompt tweak cannot be used for the thing
  people want it for.

So identity and content are separated, and a mismatch in one is survivable:

===== ================================================================
Tier  Rule
===== ================================================================
1     Same call site (``file:line``), same sequence number at that
      site, same content hash. Silent.
2     Identity established but imperfect -- the line number moved and
      the enclosing qualname still matches, or the content differs, or
      both. Matched, with a drift annotation reported at the end.
3     No positional identity at all (the code moved to another
      function), but the content hash matches an unconsumed event of
      the same kind. Matched, with a warning.
===== ================================================================

``--strict`` accepts tier 1 only, the default accepts 1-2, ``--loose`` accepts
all three. Nothing ever falls through to a live call: a debugger that quietly
does the real thing when it cannot find a recording is a debugger that lies.

Matching is scoped **within a span**, so two concurrent tool calls in different
spans can replay in either completion order. Same-span concurrent calls are
matched in recorded order, which is a documented limitation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .trace import Event

STRICT = "strict"
DEFAULT = "default"
LOOSE = "loose"
STRICTNESSES = (STRICT, DEFAULT, LOOSE)

#: Highest tier each strictness will accept.
MAX_TIER = {STRICT: 1, DEFAULT: 2, LOOSE: 3}

#: ``llm`` is a *label* a decoder puts on an http event after the fact, not a
#: different boundary. The transport asks for ``http`` on both sides, so
#: matching folds the two together -- otherwise every decoded LLM call would
#: miss its own recording.
#:
#: ``mcp`` folds in for the same reason, one milestone later. Before the MCP
#: adapter existed, an MCP call over the HTTP transport was recorded as an
#: opaque ``http`` event at the same call site; folding is what lets such a run
#: still line up against one recorded since, so the report is "this step
#: changed" rather than "these two runs share nothing".
EQUIVALENT_KINDS = {"llm": "http", "mcp": "http"}

#: What ``--only <kind>`` expands to, which is *not* the same question.
#: Folding exists so unlike events can be aligned; filtering exists so a user
#: can ask for one thing and get it. Asking for ``http`` still includes ``llm``,
#: because an llm event is an http event wearing a label -- but an ``mcp`` event
#: is a boundary of its own, and over the stdio transport there is no HTTP
#: anywhere in it.
FILTER_ALIASES = {"http": ("http", "llm")}

#: What ``tape diff`` folds together, which is a *third* question again. A
#: ``chain`` event sits at the same call site as the model calls underneath it,
#: because a suspended chain has no frame of its own, so folding it in is what
#: lets a run recorded before the LangChain adapter existed line up against one
#: recorded since.
#:
#: **It is deliberately not in EQUIVALENT_KINDS.** Alignment is advisory: a
#: wrong pairing produces a confusing report. Matching is not: a request folded
#: into the wrong bucket can be served a chain event's payload where an HTTP
#: response was expected, which is a silent wrong answer rather than a clean
#: TapeMiss. A chain node is not a boundary, so nothing recorded as ``http``
#: was ever the same crossing as one -- unlike ``llm`` and ``mcp``, which are.
ALIGN_KINDS = dict(EQUIVALENT_KINDS, chain="http")


def kind_key(kind: str) -> str:
    """The bucket a *live call* looks for its recording in."""
    return EQUIVALENT_KINDS.get(kind, kind)


def align_key(kind: str) -> str:
    """The bucket ``tape diff`` pairs two recorded events in."""
    return ALIGN_KINDS.get(kind, kind)


def filter_kinds(only: Sequence[str]) -> set:
    """The literal event kinds ``--only`` should select."""
    wanted = set()
    for kind in only:
        wanted.update(FILTER_ALIASES.get(kind, (kind,)))
    return wanted


#: Per kind, the request fields that identify the call. Deliberately narrow:
#: headers carry timestamps and content lengths, and the decoders add model and
#: token fields that only exist on the recorded side. Hashing either would make
#: every live call miss.
CONTENT_FIELDS = {
    "http": ("method", "url", "body"),
    "llm": ("method", "url", "body"),
    "tool": ("name", "args"),
    # ``op`` separates the handshake and the discovery call from a tool that
    # happens to be named ``initialize``, without giving them a kind each.
    "mcp": ("server", "op", "name", "args"),
    # Structure, and only structure. A chain node's inputs are a *consequence*
    # of the model calls above it, which the tape already holds still; hashing
    # them would report drift on every node downstream of a prompt tweak and
    # bury the one place the run actually changed. Identity is where the node
    # sits -- its path through the run tree, and which branch of a sequence or
    # a map it is.
    # ``depth`` as well as ``path``: normally one implies the other, but a very
    # deep path is capped when it is recorded, so two nodes far down a
    # recursive graph can share a capped path without sharing a position.
    "chain": ("framework", "name", "type", "path", "depth", "step"),
    "rand": ("name", "args", "kwargs", "n"),
    "time": ("name", "tz"),
    "uuid": ("name",),
}
DEFAULT_FIELDS = ("name", "args")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _body_view(body: Any) -> Any:
    """The semantic part of a recorded body.

    ``json`` when it parsed, because two encoders spacing the same object
    differently are the same request. ``size`` is dropped: it is metadata about
    the encoding, not about the call.
    """
    if not isinstance(body, dict):
        return body
    for key in ("json", "text", "raw"):
        if key in body:
            return {key: body[key]}
    return {k: v for k, v in body.items() if k != "size"}


def content_view(kind: str, req: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The subset of a request that decides whether two calls are the same."""
    req = req or {}
    fields = CONTENT_FIELDS.get(kind, DEFAULT_FIELDS)
    out: Dict[str, Any] = {}
    for name in fields:
        if name not in req:
            continue
        out[name] = _body_view(req[name]) if name == "body" else req[name]
    return out


def content_key(kind: str, req: Optional[Dict[str, Any]]) -> str:
    return hashlib.sha256(_canonical(content_view(kind, req)).encode("utf-8")).hexdigest()


def preview(kind: str, req: Optional[Dict[str, Any]], limit: int = 160) -> str:
    text = _canonical(content_view(kind, req))
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Match:
    """A recorded event claimed by a live call."""

    event: Event
    tier: int
    #: Why this was not a tier-1 match, for the end-of-run drift report.
    reason: str = ""

    @property
    def drifted(self) -> bool:
        return self.tier == 2

    @property
    def fuzzy(self) -> bool:
        return self.tier == 3


@dataclass
class Rejection:
    """An unconsumed event that was considered and why it was not used."""

    event: Event
    reason: str

    def line(self) -> str:
        return "#{:<4} {:<5} {:<26} {}".format(
            self.event.i, self.event.kind, self.event.site, self.reason
        )


@dataclass
class Request:
    """A live call looking for its recording."""

    kind: str
    site: str
    qual: Optional[str]
    span: str
    req: Dict[str, Any]

    @property
    def key(self) -> str:
        return content_key(self.kind, self.req)


@dataclass
class DriftRecord:
    """One tier-2 or tier-3 match, kept for the end-of-run summary."""

    index: int
    kind: str
    site: str
    tier: int
    reason: str

    def line(self) -> str:
        return "  #{:<4} {:<5} {:<26} {}".format(self.index, self.kind, self.site,
                                                 self.reason)


class MatchIndex:
    """Recorded events, indexed for the three tiers.

    Blob references are resolved as the index is built: the recorded side
    stores large payloads by hash while the live side has them inline, so
    hashing the stored form would make every large request miss.
    """

    def __init__(self, events: Sequence[Event], blobs: Any = None) -> None:
        self.events: List[Event] = list(events)
        self._resolved: Dict[int, Dict[str, Any]] = {}
        self._keys: Dict[int, str] = {}
        self._by_site: Dict[Tuple[str, str, str], List[int]] = {}
        self._by_qual: Dict[Tuple[str, str, str], List[int]] = {}
        self._by_key: Dict[str, List[int]] = {}
        self._by_index: Dict[int, Event] = {e.i: e for e in self.events}
        self.consumed: Dict[int, bool] = {}

        for event in self.events:
            req = event.req
            if blobs is not None:
                try:
                    req = blobs.resolve(req)
                except Exception:  # pragma: no cover - missing blobs dir
                    req = event.req
            self._resolved[event.i] = req
            key = content_key(event.kind, req)
            self._keys[event.i] = key
            key_kind = kind_key(event.kind)
            self._by_site.setdefault((event.span, event.site, key_kind), []).append(event.i)
            if event.qual:
                self._by_qual.setdefault(
                    (event.span, event.qual, key_kind), []).append(event.i)
            self._by_key.setdefault(key, []).append(event.i)

    # -- lookup ----------------------------------------------------------

    def resolved_req(self, event: Event) -> Dict[str, Any]:
        return self._resolved.get(event.i, event.req)

    def key_of(self, event: Event) -> str:
        return self._keys.get(event.i, "")

    def _first_unconsumed(
        self, bucket: Optional[List[int]], qual: Optional[str] = None
    ) -> Optional[Event]:
        """The next unconsumed event in a bucket, in recorded order.

        ``qual`` guards against a coincidence: two different calls can share a
        ``file:line`` after code moves around, and matching one to the other
        because the line number happens to agree would be exactly the silent
        wrongness this matcher exists to avoid. When both sides know their
        enclosing function, they have to agree.
        """
        for index in bucket or ():
            if self.consumed.get(index):
                continue
            event = self._by_index[index]
            if qual and event.qual and event.qual != qual:
                continue
            return event
        return None

    def unconsumed(self) -> List[Event]:
        return [e for e in self.events if not self.consumed.get(e.i)]

    def take(self, request: Request) -> Optional[Match]:
        """Find the recording for ``request`` and mark it consumed."""
        key = request.key
        wanted = kind_key(request.kind)

        # Tier 1/2: same span, same call site, next unconsumed call from it.
        event = self._first_unconsumed(
            self._by_site.get((request.span, request.site, wanted)),
            request.qual,
        )
        if event is not None:
            same = self.key_of(event) == key
            return self._claim(event, 1 if same else 2,
                               "" if same else "content changed")

        # Still tier 2: the line number moved but the enclosing function did
        # not. This is what an inserted import above the call site looks like.
        if request.qual:
            event = self._first_unconsumed(
                self._by_qual.get((request.span, request.qual, wanted))
            )
            if event is not None:
                same = self.key_of(event) == key
                return self._claim(
                    event, 2,
                    "line moved (was {})".format(event.site) if same
                    else "line moved and content changed",
                )

        # Tier 3: the call site is gone entirely, but this exact content was
        # recorded somewhere. Prefer the same span.
        for candidate_index in self._by_key.get(key, ()):
            candidate = self._by_index[candidate_index]
            if self.consumed.get(candidate_index) or kind_key(candidate.kind) != wanted:
                continue
            if candidate.span == request.span:
                return self._claim(candidate, 3, "call site moved from {}".format(
                    candidate.site))
        for candidate_index in self._by_key.get(key, ()):
            candidate = self._by_index[candidate_index]
            if self.consumed.get(candidate_index) or kind_key(candidate.kind) != wanted:
                continue
            return self._claim(candidate, 3, "call site and span moved from {} in {}".format(
                candidate.site, candidate.span))
        return None

    def _claim(self, event: Event, tier: int, reason: str) -> Match:
        self.consumed[event.i] = True
        return Match(event, tier, reason)

    def release(self, event: Event) -> None:
        """Un-consume an event rejected by the strictness setting."""
        self.consumed.pop(event.i, None)

    # -- diagnostics -----------------------------------------------------

    def candidates(self, request: Request, limit: int = 5) -> List[Rejection]:
        """The nearest unconsumed events, each with why it was not used.

        This is the body of every ``TapeMiss``, so it is worth more than the
        error message itself: it is what turns "no match" into a diagnosis.
        """
        key = request.key
        scored: List[Tuple[int, Rejection]] = []

        for event in self.events:
            if self.consumed.get(event.i):
                continue
            same_kind = kind_key(event.kind) == kind_key(request.kind)
            same_site = event.site == request.site
            same_qual = bool(request.qual) and event.qual == request.qual
            same_span = event.span == request.span
            same_key = self.key_of(event) == key

            if same_kind and same_site and same_span:
                score, reason = -1, (
                    "same call site, content differs -- would match without --strict"
                    if not same_key else "same call site and content; already passed over"
                )
            elif same_site and same_span and not same_kind:
                score, reason = 0, "same call site, but recorded as {}".format(event.kind)
            elif same_kind and same_site and not same_span:
                score, reason = 1, "same call site, recorded in span {!r}".format(event.span)
            elif same_kind and same_key:
                score, reason = 2, "content matches, but at {} (needs --loose)".format(event.site)
            elif same_kind and same_qual:
                score, reason = 3, "same function, different line ({})".format(event.site)
            elif same_kind and same_span:
                score, reason = 4, "same kind and span, different call site"
            elif same_kind:
                score, reason = 5, "same kind, at {} in span {!r}".format(event.site, event.span)
            else:
                continue
            scored.append((score, Rejection(event, reason)))

        scored.sort(key=lambda pair: (pair[0], pair[1].event.i))
        rejections = [rejection for _, rejection in scored[:limit]]

        if not rejections:
            consumed_here = [
                e for e in self.events
                if self.consumed.get(e.i) and kind_key(e.kind) == kind_key(request.kind)
                and (e.site == request.site
                     or (request.qual and e.qual == request.qual))
            ]
            if consumed_here:
                rejections.append(Rejection(
                    consumed_here[-1],
                    "already replayed -- this call site ran more times than it was recorded",
                ))
        return rejections
