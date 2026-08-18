"""OpenAI response decoder.

Recognises the chat-completions, responses, and embeddings shapes, streamed or
not, and adds the model, token counts, and cost to the event.

Matching is by path shape *and* a response-body key check rather than by host
alone, so Azure deployments, gateways, proxies, and local test servers are all
recognised without the transport knowing anything about any of them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..trace import Event
from . import common, pricing

NAME = "openai"

HOSTS = ("api.openai.com",)
PATHS = (
    "/chat/completions",
    "/completions",
    "/responses",
    "/embeddings",
)


def _looks_like_openai(body: Optional[Dict[str, Any]]) -> bool:
    if not body:
        return False
    if "choices" in body:
        return True
    return str(body.get("object", "")).startswith(("chat.completion", "response", "list"))


def matches(event: Event) -> bool:
    host, path = common.url_parts(event)
    if not (host in HOSTS or common.path_matches(path, PATHS)):
        return False
    if _looks_like_openai(common.response_json(event)):
        return True
    chunks = common.stream_chunks(event)
    if chunks:
        for _, data in common.sse_messages(chunks)[:1]:
            return _looks_like_openai(data if isinstance(data, dict) else None)
    return False


def _usage(usage: Optional[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    """OpenAI has used two names for the same two numbers; accept both."""
    if not isinstance(usage, dict):
        return {"in": None, "out": None}
    return {
        "in": common.as_int(usage.get("prompt_tokens"))
        or common.as_int(usage.get("input_tokens")),
        "out": common.as_int(usage.get("completion_tokens"))
        or common.as_int(usage.get("output_tokens")),
    }


def _content_of(body: Dict[str, Any]) -> Optional[str]:
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            return message.get("content")
        if isinstance(choices[0], dict):
            return choices[0].get("text")
    output = body.get("output_text")
    return output if isinstance(output, str) else None


def _from_stream(chunks: List[str]) -> Dict[str, Any]:
    """Assemble the completion and usage from the recorded chunk list.

    Token counts arrive in a terminal chunk (with ``stream_options``'s
    ``include_usage``), which is exactly why the decoder reads chunks rather
    than an assembled body.
    """
    text: List[str] = []
    usage: Optional[Dict[str, Any]] = None
    finish: Optional[str] = None
    model: Optional[str] = None
    for _, data in common.sse_messages(chunks):
        if not isinstance(data, dict):
            continue
        model = model or data.get("model")
        if isinstance(data.get("usage"), dict):
            usage = data["usage"]
        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta") or {}
            piece = delta.get("content") if isinstance(delta, dict) else None
            if isinstance(piece, str):
                text.append(piece)
    return {
        "content": "".join(text),
        "usage": usage,
        "finish_reason": finish,
        "model": model,
    }


def decode(event: Event) -> Optional[Dict[str, Any]]:
    """Enrichment fields for an OpenAI call, or None if this is not one."""
    if not matches(event):
        return None

    request = common.request_json(event) or {}
    model = request.get("model")
    chunks = common.stream_chunks(event)

    if chunks is not None:
        assembled = _from_stream(chunks)
        tokens = _usage(assembled["usage"])
        model = model or assembled["model"]
        content = assembled["content"]
        finish = assembled["finish_reason"]
        streamed = True
    else:
        body = common.response_json(event) or {}
        tokens = _usage(body.get("usage"))
        model = model or body.get("model")
        content = _content_of(body)
        choices = body.get("choices")
        finish = (
            choices[0].get("finish_reason")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict)
            else None
        )
        streamed = False

    req: Dict[str, Any] = {"provider": NAME}
    if model:
        req["model"] = model
    messages = request.get("messages")
    if isinstance(messages, list):
        req["n_messages"] = len(messages)
    for field in ("temperature", "top_p", "max_tokens", "seed", "tools"):
        if field in request:
            req[field] = len(request[field]) if field == "tools" else request[field]

    res: Dict[str, Any] = {"tokens": tokens}
    if content:
        res["preview"] = common.preview(content)
    if finish:
        res["finish_reason"] = finish
    if streamed:
        res["streamed"] = True

    meta: Dict[str, Any] = {}
    cost = pricing.cost_usd(model, tokens["in"], tokens["out"])
    if cost is not None:
        meta["cost_usd"] = cost

    return {"kind": "llm", "req": req, "res": res, "meta": meta}


def context(event: Event) -> Optional[Dict[str, Any]]:
    """The message array this call sent, normalised for display and diffing.

    Returns the raw provider shapes; :mod:`reeltime.core.context` flattens
    them. Kept next to the decoder because knowing where the messages live is
    provider knowledge, and this is the module that owns it.
    """
    if not matches(event):
        return None
    request = common.request_json(event) or {}
    messages = request.get("messages")
    if not isinstance(messages, list):
        # The Responses API calls it `input`, and accepts a bare string.
        value = request.get("input")
        if isinstance(value, str):
            messages = [{"role": "user", "content": value}]
        elif isinstance(value, list):
            messages = value
        else:
            messages = []
        instructions = request.get("instructions")
        if isinstance(instructions, str):
            messages = [{"role": "system", "content": instructions}] + list(messages)
    return {
        "provider": NAME,
        "model": request.get("model"),
        "messages": messages,
        "tools": [
            (tool.get("function") or tool).get("name")
            for tool in request.get("tools") or []
            if isinstance(tool, dict)
        ],
        "params": {
            key: request[key]
            for key in ("temperature", "top_p", "max_tokens", "seed",
                        "response_format", "tool_choice")
            if key in request
        },
    }
