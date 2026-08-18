"""Helpers shared by the provider decoders.

Everything here is a pure read over an already-recorded event. No decoder
imports a provider SDK, opens a socket, or touches the filesystem -- a decoder
is a function from recorded bytes to a few extra fields, which is what lets the
same code enrich a trace that was written months ago.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from ..trace import Event


def url_parts(event: Event) -> Tuple[str, str]:
    """``(host, path)`` of the recorded request, lowercased host."""
    url = event.req.get("url")
    if not isinstance(url, str):
        return ("", "")
    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover - malformed URL
        return ("", "")
    return (parts.hostname or "", parts.path or "")


def request_json(event: Event) -> Optional[Dict[str, Any]]:
    body = event.req.get("body")
    if isinstance(body, dict):
        value = body.get("json")
        if isinstance(value, dict):
            return value
    return None


def response_json(event: Event) -> Optional[Dict[str, Any]]:
    body = (event.res or {}).get("body")
    if isinstance(body, dict):
        value = body.get("json")
        if isinstance(value, dict):
            return value
    return None


def stream_chunks(event: Event) -> Optional[List[str]]:
    """The recorded chunk list for a streamed response, in order."""
    stream = (event.res or {}).get("stream")
    if not isinstance(stream, dict):
        return None
    chunks = stream.get("chunks")
    if not isinstance(chunks, list):
        return None
    return [c for c in chunks if isinstance(c, str)]


def sse_messages(chunks: Sequence[str]) -> List[Tuple[Optional[str], Any]]:
    """Parse SSE chunks into ``(event_name, parsed_data)`` pairs.

    Chunk boundaries and SSE record boundaries are unrelated -- one TCP read
    can carry three events or half of one -- so the chunks are joined before
    being split on the blank-line delimiter.
    """
    out: List[Tuple[Optional[str], Any]] = []
    for block in "".join(chunks).split("\n\n"):
        if not block.strip():
            continue
        name: Optional[str] = None
        data_lines: List[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        if payload == "[DONE]":
            continue
        try:
            out.append((name, json.loads(payload)))
        except ValueError:
            out.append((name, payload))
    return out


def path_matches(path: str, suffixes: Sequence[str]) -> bool:
    return any(path.endswith(suffix) for suffix in suffixes)


def preview(text: Optional[str], limit: int = 200) -> Optional[str]:
    """A short excerpt of the completion, so ``tape ls`` can show something."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def as_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
