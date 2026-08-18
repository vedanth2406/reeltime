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
from . import anthropic, openai


class Decoder(NamedTuple):
    """A provider recogniser. ``matches`` gates ``decode``; first match wins."""

    name: str
    matches: Callable[[Event], bool]
    decode: Callable[[Event], Optional[Dict[str, Any]]]


REGISTRY: List[Decoder] = [
    Decoder(openai.NAME, openai.matches, openai.decode),
    Decoder(anthropic.NAME, anthropic.matches, anthropic.decode),
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


__all__ = ["Decoder", "REGISTRY", "decode", "decode_resolved", "register"]
