"""Turning wire bytes into trace payloads, and back.

Bodies are recorded in the most readable form that still reconstructs the
original bytes:

* JSON that parses      -> ``{"json": {...}}``, plus ``raw`` when a compact
  re-encode would not reproduce the exact bytes
* other valid UTF-8     -> ``{"text": "..."}``
* anything else         -> ``{"raw": "<base64>"}``

Readability is not a luxury here. The trace is JSONL so that ``grep`` works on
it, and a request body stored as one base64 blob would make the format useless
for the thing people actually do with a trace, which is search it.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: Content types whose chunk boundaries are semantically meaningful. Everything
#: an LLM provider streams is served as SSE, and for those the ordered chunk
#: list is recorded instead of the assembled body. Chunking of an ordinary
#: response is a transport artefact and is not worth preserving.
STREAM_CONTENT_TYPES = ("text/event-stream",)


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
            out["json"] = parsed
            # Keep the exact bytes only when we could not rebuild them, which
            # is the common case: every SDK spaces its JSON differently.
            if _compact(parsed) != data:
                out["raw"] = base64.b64encode(data).decode("ascii")
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


def encode_chunks(chunks: Sequence[bytes]) -> Dict[str, Any]:
    """Record a stream as its ordered chunk list, boundaries intact.

    Text chunks stay text so the trace remains greppable; a stream with any
    non-UTF-8 chunk falls back to base64 for the whole list rather than mixing
    representations, which would make the ordering ambiguous to read.
    """
    try:
        text_chunks = [chunk.decode("utf-8") for chunk in chunks]
    except UnicodeDecodeError:
        return {
            "encoding": "base64",
            "chunks": [base64.b64encode(chunk).decode("ascii") for chunk in chunks],
            "size": sum(len(chunk) for chunk in chunks),
        }
    return {
        "encoding": "utf-8",
        "chunks": text_chunks,
        "size": sum(len(chunk) for chunk in chunks),
    }


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
