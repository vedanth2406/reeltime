"""Anthropic response decoder.

The Messages API shape, streamed or not. Streaming splits the token counts
across two events -- ``message_start`` carries the input tokens and
``message_delta`` the output tokens -- so both have to be read out of the
recorded chunk list.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..trace import Event
from . import common, pricing

NAME = "anthropic"

HOSTS = ("api.anthropic.com",)
PATHS = ("/v1/messages", "/v1/complete")


def _looks_like_anthropic(body: Optional[Dict[str, Any]]) -> bool:
    if not body:
        return False
    if body.get("type") in ("message", "message_start", "completion"):
        return True
    return isinstance(body.get("content"), list) and "usage" in body


def matches(event: Event) -> bool:
    host, path = common.url_parts(event)
    if not (host in HOSTS or common.path_matches(path, PATHS)):
        return False
    if _looks_like_anthropic(common.response_json(event)):
        return True
    chunks = common.stream_chunks(event)
    if chunks:
        for name, data in common.sse_messages(chunks)[:1]:
            if name == "message_start":
                return True
            return _looks_like_anthropic(data if isinstance(data, dict) else None)
    return False


def _text_of(content: Any) -> Optional[str]:
    if not isinstance(content, list):
        return None
    parts = [
        block.get("text")
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return "".join(parts) if parts else None


def _from_stream(chunks: List[str]) -> Dict[str, Any]:
    text: List[str] = []
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    stop: Optional[str] = None
    model: Optional[str] = None

    for name, data in common.sse_messages(chunks):
        if not isinstance(data, dict):
            continue
        kind = name or data.get("type")
        if kind == "message_start":
            message = data.get("message") or {}
            model = model or message.get("model")
            usage = message.get("usage") or {}
            tokens_in = common.as_int(usage.get("input_tokens")) or tokens_in
            tokens_out = common.as_int(usage.get("output_tokens")) or tokens_out
        elif kind == "content_block_delta":
            delta = data.get("delta") or {}
            piece = delta.get("text")
            if isinstance(piece, str):
                text.append(piece)
        elif kind == "message_delta":
            usage = data.get("usage") or {}
            tokens_out = common.as_int(usage.get("output_tokens")) or tokens_out
            stop = (data.get("delta") or {}).get("stop_reason") or stop
    return {
        "content": "".join(text),
        "tokens": {"in": tokens_in, "out": tokens_out},
        "stop_reason": stop,
        "model": model,
    }


def decode(event: Event) -> Optional[Dict[str, Any]]:
    """Enrichment fields for an Anthropic call, or None if this is not one."""
    if not matches(event):
        return None

    request = common.request_json(event) or {}
    model = request.get("model")
    chunks = common.stream_chunks(event)

    if chunks is not None:
        assembled = _from_stream(chunks)
        tokens = assembled["tokens"]
        model = model or assembled["model"]
        content = assembled["content"]
        stop = assembled["stop_reason"]
        streamed = True
    else:
        body = common.response_json(event) or {}
        usage = body.get("usage") or {}
        tokens = {
            "in": common.as_int(usage.get("input_tokens")),
            "out": common.as_int(usage.get("output_tokens")),
        }
        model = model or body.get("model")
        content = _text_of(body.get("content"))
        stop = body.get("stop_reason")
        streamed = False

    req: Dict[str, Any] = {"provider": NAME}
    if model:
        req["model"] = model
    messages = request.get("messages")
    if isinstance(messages, list):
        req["n_messages"] = len(messages)
    if isinstance(request.get("system"), (str, list)):
        req["has_system"] = True
    for field in ("temperature", "top_p", "max_tokens"):
        if field in request:
            req[field] = request[field]
    if isinstance(request.get("tools"), list):
        req["tools"] = len(request["tools"])

    res: Dict[str, Any] = {"tokens": tokens}
    if content:
        res["preview"] = common.preview(content)
    if stop:
        res["stop_reason"] = stop
    if streamed:
        res["streamed"] = True

    meta: Dict[str, Any] = {}
    cost = pricing.cost_usd(model, tokens["in"], tokens["out"])
    if cost is not None:
        meta["cost_usd"] = cost

    return {"kind": "llm", "req": req, "res": res, "meta": meta}


def context(event: Event) -> Optional[Dict[str, Any]]:
    """The message array this call sent, normalised for display and diffing.

    The system prompt travels in its own top-level field rather than inside the
    array, so it is hoisted to position 0 -- it is part of what the model read,
    and leaving it out of the context view would hide the single field people
    most often get wrong.
    """
    if not matches(event):
        return None
    request = common.request_json(event) or {}
    messages = list(request.get("messages") or [])
    system = request.get("system")
    if system:
        messages = [{"role": "system", "content": system, "_hoisted": True}] + messages
    return {
        "provider": NAME,
        "model": request.get("model"),
        "messages": messages,
        "tools": [
            tool.get("name")
            for tool in request.get("tools") or []
            if isinstance(tool, dict)
        ],
        "params": {
            key: request[key]
            for key in ("temperature", "top_p", "max_tokens", "stop_sequences",
                        "tool_choice")
            if key in request
        },
    }
