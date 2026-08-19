"""Turning wire bytes into trace payloads, and back.

Bodies are recorded in the most readable form available:

* JSON that parses      -> ``{"json": {...}}``
* other valid UTF-8     -> ``{"text": "..."}``
* anything else         -> ``{"raw": "<base64>"}``

Readability is not a luxury here. The trace is JSONL so that ``grep`` works on
it, and a request body stored as one base64 blob would make the format useless
for the thing people actually do with a trace, which is search it.

**A parsed JSON body is stored only as JSON, never alongside a base64 copy of
the original bytes.** An earlier version kept both, so that a body whose
encoder spaced its JSON differently could be reproduced byte for byte. That was
a redaction hole: the scrubber rewrites the parsed view, and a secret sitting in
the base64 copy sails past every regex it owns. Since the only consumer of a
recorded body is a JSON parser, key order (preserved) matters and whitespace
does not, so the exact bytes were never worth a hole in the one guarantee that
makes traces shareable.

A binary body still becomes base64 and still cannot be scanned. That is a
narrow, documented limit rather than a silent one.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: Content types whose chunk boundaries are semantically meaningful. Most of
#: what an LLM provider streams is served as SSE, and for those the ordered
#: chunk list is recorded instead of the assembled body. Chunking of an
#: ordinary response is a transport artefact and is not worth preserving.
#:
#: Bedrock is the exception to the SSE rule: it streams
#: ``application/vnd.amazon.eventstream``, a binary framing with a length
#: prelude and two CRC32s per message. Nothing about it is line-oriented, so a
#: recorded blob that is one byte out does not degrade -- botocore's parser
#: rejects the whole stream on a CRC mismatch. That makes byte-exact capture
#: the only thing worth having here, which is precisely what the chunk list is.
STREAM_CONTENT_TYPES = ("text/event-stream", "application/vnd.amazon.eventstream")


def is_stream_content_type(content_type: Optional[str]) -> bool:
    if not content_type:
        return False
    return content_type.split(";")[0].strip().lower() in STREAM_CONTENT_TYPES


def _compact(value: Any) -> Optional[bytes]:
    try:
        return json.dumps(
            value, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def encode_body(data: Optional[bytes]) -> Dict[str, Any]:
    """Record ``data`` in the most useful reversible form."""
    if data is None:
        return {}
    out: Dict[str, Any] = {"size": len(data)}
    if not data:
        return out

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        out["raw"] = base64.b64encode(data).decode("ascii")
        return out

    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            # Deliberately no base64 copy alongside: see the module docstring.
            out["json"] = parsed
            return out

    out["text"] = text
    return out


def decode_body(payload: Optional[Mapping[str, Any]]) -> bytes:
    """Rebuild the original bytes from a recorded body. Inverse of :func:`encode_body`."""
    if not payload:
        return b""
    raw = payload.get("raw")
    if isinstance(raw, str):
        return base64.b64decode(raw)
    if "json" in payload:
        return _compact(payload["json"]) or b""
    text = payload.get("text")
    if isinstance(text, str):
        return text.encode("utf-8")
    return b""


def encode_chunks(
    chunks: Sequence[bytes], offsets: Optional[Sequence[float]] = None
) -> Dict[str, Any]:
    """Record a stream as its ordered chunk list, boundaries intact.

    Text chunks stay text so the trace remains greppable; a stream with any
    non-UTF-8 chunk falls back to base64 for the whole list rather than mixing
    representations, which would make the ordering ambiguous to read.

    ``offsets`` are milliseconds from the start of the request to the arrival
    of each chunk. They are what ``tape replay --realtime`` re-emits, which is
    how you reproduce a bug in code that cancels early or barges in.
    """
    payload: Dict[str, Any] = {"size": sum(len(chunk) for chunk in chunks)}
    try:
        payload["encoding"] = "utf-8"
        payload["chunks"] = [chunk.decode("utf-8") for chunk in chunks]
    except UnicodeDecodeError:
        payload["encoding"] = "base64"
        payload["chunks"] = [base64.b64encode(chunk).decode("ascii") for chunk in chunks]
    if offsets:
        payload["offsets"] = [round(value, 3) for value in offsets]
    return payload


def chunk_delays(payload: Optional[Mapping[str, Any]]) -> List[float]:
    """Seconds to wait before each chunk, derived from recorded offsets."""
    offsets = (payload or {}).get("offsets") or []
    delays: List[float] = []
    previous = 0.0
    for offset in offsets:
        delays.append(max(0.0, (offset - previous) / 1000.0))
        previous = offset
    return delays


def decode_chunks(payload: Optional[Mapping[str, Any]]) -> List[bytes]:
    """Rebuild the exact chunk sequence. Inverse of :func:`encode_chunks`."""
    if not payload:
        return []
    chunks = payload.get("chunks") or []
    if payload.get("encoding") == "base64":
        return [base64.b64decode(chunk) for chunk in chunks]
    return [chunk.encode("utf-8") for chunk in chunks]


def header_pairs(headers: Any) -> List[Tuple[str, str]]:
    """Normalise any header container to a list of pairs, repeats preserved."""
    if headers is None:
        return []
    items = headers.multi_items() if hasattr(headers, "multi_items") else None
    if items is None:
        items = headers.items() if hasattr(headers, "items") else headers
    out: List[Tuple[str, str]] = []
    for key, value in items:
        key = key.decode("latin-1") if isinstance(key, bytes) else str(key)
        value = value.decode("latin-1") if isinstance(value, bytes) else str(value)
        out.append((key, value))
    return out


def content_type_of(headers: Sequence[Tuple[str, str]]) -> Optional[str]:
    for key, value in headers:
        if key.lower() == "content-type":
            return value
    return None


# -- boundaries another adapter already owns -----------------------------
#
# An MCP session over the HTTP transport is recorded as `mcp` events, and the
# transport's own POST underneath is not a second boundary crossing. Nesting
# normally handles that -- the outermost boundary is the one recorded -- but
# the MCP SDK issues its requests from a task it spawned when the connection
# opened, and the boundary flag is a contextvar, so it was copied before any
# boundary existed and never reaches the request.
#
# So the owning adapter registers its endpoint instead. The shim consults this
# where it already has the URL in hand, and hands back an unwrapped transport.

_OWNED_PREFIXES: List[str] = []


def own_endpoint(url: str) -> None:
    """Claim ``url`` and everything under it for a higher-level adapter."""
    if url:
        _OWNED_PREFIXES.append(url)


def release_endpoint(url: str) -> None:
    """Give ``url`` back. Removes one claim, so nesting the same URL works."""
    try:
        _OWNED_PREFIXES.remove(url)
    except ValueError:  # pragma: no cover - defensive
        pass


def is_owned(url: Any) -> bool:
    """Whether this request is already being recorded as something else."""
    if not _OWNED_PREFIXES:
        return False
    text = str(url)
    return any(text.startswith(prefix) for prefix in _OWNED_PREFIXES)


def _reset_owned_for_tests() -> None:
    _OWNED_PREFIXES.clear()
