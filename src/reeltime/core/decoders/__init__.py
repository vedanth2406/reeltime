"""Provider decoders: raw HTTP in, a few semantic fields out.

The transport shim is deliberately ignorant of providers -- it records bytes.
That keeps the interception layer framework-agnostic (design principle 5), but
the event schema still wants a model name, token counts, and a cost on LLM
events. A decoder closes that gap *after* the fact:

    raw http event  ──►  decode(event)  ──►  {"kind": "llm", "req": {...}, ...}

Rules, all of them load-bearing:

* A decoder is **pure**. It reads the recorded event and nothing else -- no
  network, no filesystem, and no import of the provider's SDK. That is what
  lets the same function enrich a trace recorded months ago on another machine.
* **No match is not an error.** An unrecognised provider stays a plain ``http``
  event with no token counts, which is perfectly replayable.
* **A decoder that raises never fails a recording.** It is caught, logged once
  at debug level, and the event is written unenriched.
* Enrichment adds only *small derived* fields. The messages and the response
  body are already on the event; duplicating them into ``req.messages`` would
  double the size of every trace for no gain.

Adding a provider is one module and one pricing row, with nothing patched.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, NamedTuple, Optional

from ..trace import Event
from . import anthropic, bedrock, openai


class Decoder(NamedTuple):
    """A provider recogniser. ``matches`` gates the rest; first match wins."""

    name: str
    matches: Callable[[Event], bool]
    decode: Callable[[Event], Optional[Dict[str, Any]]]
    #: Optional: extract the message array for ``tape show --context``.
    context: Optional[Callable[[Event], Optional[Dict[str, Any]]]] = None


REGISTRY: List[Decoder] = [
    Decoder(openai.NAME, openai.matches, openai.decode, openai.context),
    Decoder(anthropic.NAME, anthropic.matches, anthropic.decode, anthropic.context),
    # Bedrock last of the three: its `matches` is a host check, so it can never
    # claim a first-party call, but ordering it after the shapes it wraps keeps
    # the cheap discriminators first.
    Decoder(bedrock.NAME, bedrock.matches, bedrock.decode),
]


def register(decoder: Decoder, first: bool = False) -> None:
    """Add a decoder. ``first=True`` to take precedence over the built-ins."""
    if first:
        REGISTRY.insert(0, decoder)
    else:
        REGISTRY.append(decoder)


def decode(event: Event) -> Optional[Dict[str, Any]]:
    """Enrichment for ``event`` from the first decoder that claims it."""
    if event.kind not in ("http", "llm"):
        return None
    for decoder in REGISTRY:
        if decoder.matches(event):
            return decoder.decode(event)
    return None


def apply(event: Event, extra: Optional[Dict[str, Any]]) -> bool:
    """Merge a decoder's output onto an event. Returns True if anything changed.

    Shared by the live recording path and by ``tape reindex`` so the two cannot
    drift apart: an event enriched after the fact must come out identical to one
    enriched as it was written.
    """
    if not extra:
        return False
    changed = False
    kind = extra.get("kind")
    if isinstance(kind, str) and kind != event.kind:
        event.kind = kind
        changed = True
    for field in ("req", "res", "meta"):
        addition = extra.get(field)
        if not isinstance(addition, dict) or not addition:
            continue
        current = getattr(event, field)
        if current is None:
            setattr(event, field, dict(addition))
            changed = True
            continue
        for key, value in addition.items():
            if current.get(key) != value:
                current[key] = value
                changed = True
    return changed


def context_of(event: Event) -> Optional[Dict[str, Any]]:
    """The raw message array for an LLM event, from whichever decoder claims it."""
    if event.kind not in ("http", "llm"):
        return None
    for decoder in REGISTRY:
        if decoder.context is not None and decoder.matches(event):
            return decoder.context(event)
    return None


def decode_resolved(event: Event, blobs: Any) -> Optional[Dict[str, Any]]:
    """Decode an event read back from disk, resolving its blob references.

    Recording decodes before externalisation, so the live path never needs
    this. Reading a trace does: it is what makes a new decoder able to enrich
    old runs, and what ``tape reindex`` (M4) will be built on.
    """
    resolved = Event(
        i=event.i,
        kind=event.kind,
        site=event.site,
        span=event.span,
        t_rel=event.t_rel,
        dur_ms=event.dur_ms,
        req=blobs.resolve(event.req),
        res=blobs.resolve(event.res) if event.res is not None else None,
        qual=event.qual,
        meta=dict(event.meta),
    )
    return decode(resolved)


__all__ = ["Decoder", "REGISTRY", "apply", "context_of", "decode",
           "decode_resolved", "register"]
