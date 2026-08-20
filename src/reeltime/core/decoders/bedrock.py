"""Amazon Bedrock response decoder.

Bedrock is one endpoint in front of many model families, and each family keeps
its answer somewhere different: Anthropic returns the Messages shape, Titan
returns ``results[].outputText`` with ``inputTextTokenCount`` beside it, Nova
and the Converse API return ``output.message`` with a ``usage`` block, and Meta
returns a flat ``generation``. The model id is not in the body at all -- it is
in the URL path -- so this decoder reads it from there.

Streaming is the interesting half. Bedrock does not stream SSE; it streams
``application/vnd.amazon.eventstream``, a binary framing with a length prelude,
two CRC32s per message, and typed headers. So the recorded chunk list is
base64, and getting token counts out of it means parsing the framing here --
which is why :func:`iter_frames` exists. It is a reader, not a validator: the
CRCs are recorded and replayed byte for byte, and re-checking them in a decoder
that must never fail a recording would buy nothing.

Bedrock puts the token counts for *every* family in the same place on a stream:
a final event carrying ``amazon-bedrock-invocationMetrics``. That is one of the
few things uniform across the families, and it is what this leans on.

**Cost is not always available, and that is deliberate.** Bedrock does not
charge the first-party price for the same model -- Claude 3.5 Sonnet is
$3.00/$15.00 direct and $6.00/$30.00 here -- so no row is inferred from an
existing one. Models whose Bedrock price could not be verified report tokens
and leave ``cost_usd`` null. See :mod:`reeltime.core.decoders.pricing`.
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import unquote

from ..trace import Event
from . import common, pricing

NAME = "bedrock"

#: ``bedrock-runtime.<region>.amazonaws.com`` and the FIPS/dualstack spellings.
HOST_MARKER = "bedrock-runtime"

#: The four operations that carry a model id in the path.
OPERATIONS = ("invoke", "invoke-with-response-stream", "converse", "converse-stream")

#: Present on the last event of any Bedrock stream, whatever the model family.
METRICS_KEY = "amazon-bedrock-invocationMetrics"

#: Length of an event-stream prelude: total length, headers length, prelude CRC.
_PRELUDE = 12


def model_from_path(path: str) -> Optional[str]:
    """``/model/anthropic.claude-3-5-sonnet%3A0/invoke`` -> the model id.

    The id is percent-encoded because it contains a colon, and cross-region
    inference profiles prefix it with a geography (``us.anthropic...``) which
    is left on: it is part of what was actually called, and pricing strips it.
    """
    parts = [segment for segment in path.split("/") if segment]
    if len(parts) < 2 or parts[0] not in ("model", "async-invoke"):
        return None
    if parts[-1] in OPERATIONS:
        parts = parts[:-1]
    return unquote("/".join(parts[1:])) or None


def matches(event: Event) -> bool:
    """Bedrock by host, or by the shape of its path.

    The host check alone is not enough, and not only for tests: a VPC endpoint,
    a gateway, LocalStack, or any ``endpoint_url=`` override puts a different
    name in front of the same API, and a Bedrock call that stops being
    recognised because of where it was sent loses its token counts for no
    reason. The path shape is specific enough to carry the recognition on its
    own -- ``/model/<id>/invoke`` with one of four known operations at the end.
    """
    host, path = common.url_parts(event)
    if model_from_path(path) is None:
        return False
    if HOST_MARKER in host:
        return True
    return any(path.endswith("/" + operation) for operation in OPERATIONS)


# -- the event-stream framing --------------------------------------------


def iter_frames(data: bytes) -> Iterator[Tuple[Dict[str, str], bytes]]:
    """Yield ``(headers, payload)`` for each message in an event stream.

    Deliberately tolerant: a truncated tail is where a killed run's trace ends,
    and a decoder that raised on one would take the whole enrichment with it.
    """
    offset = 0
    size = len(data)
    while offset + _PRELUDE <= size:
        total, header_len = struct.unpack("!II", data[offset:offset + 8])
        if total < _PRELUDE + 4 or offset + total > size:
            return
        cursor = offset + _PRELUDE
        end_headers = cursor + header_len
        headers: Dict[str, str] = {}
        while cursor < end_headers and cursor < size:
            name_len = data[cursor]
            cursor += 1
            name = data[cursor:cursor + name_len].decode("utf-8", "replace")
            cursor += name_len
            value_type = data[cursor]
            cursor += 1
            if value_type == 7:  # string, the only type Bedrock uses
                (value_len,) = struct.unpack("!H", data[cursor:cursor + 2])
                cursor += 2
                headers[name] = data[cursor:cursor + value_len].decode(
                    "utf-8", "replace")
                cursor += value_len
            else:  # pragma: no cover - Bedrock sends only string headers
                break
        yield headers, data[end_headers:offset + total - 4]
        offset += total


def frame_payloads(chunks: List[bytes]) -> List[Dict[str, Any]]:
    """The decoded JSON of every ``chunk`` event in a recorded stream.

    Bedrock wraps each model-produced object in ``{"bytes": "<base64>"}``, so
    there are two layers to come through before the model's own JSON appears.
    """
    out: List[Dict[str, Any]] = []
    for _, payload in iter_frames(b"".join(chunks)):
        try:
            envelope = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(envelope, dict):
            continue
        inner = envelope.get("bytes")
        if isinstance(inner, str):
            try:
                envelope = json.loads(base64.b64decode(inner).decode("utf-8"))
            except (ValueError, UnicodeDecodeError, binascii.Error):
                continue
        if isinstance(envelope, dict):
            out.append(envelope)
    return out


def _recorded_stream_bytes(event: Event) -> Optional[List[bytes]]:
    stream = (event.res or {}).get("stream")
    if not isinstance(stream, dict):
        return None
    from ..http import common as http_common

    return http_common.decode_chunks(stream)


# -- reading each family's answer ----------------------------------------


def _text_of_content(content: Any) -> Optional[str]:
    if not isinstance(content, list):
        return None
    parts = [block.get("text") for block in content
             if isinstance(block, dict) and isinstance(block.get("text"), str)]
    return "".join(parts) if parts else None


def read_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """Tokens and a preview from one family's non-streaming response."""
    out: Dict[str, Any] = {}

    # Anthropic on Bedrock: the Messages shape, minus the wrapper.
    usage = body.get("usage")
    if isinstance(usage, dict):
        # `input_tokens` is Anthropic's spelling; `inputTokens` is Nova's and
        # the Converse API's. Both appear under `usage`, so both are read here.
        out["tokens_in"] = common.as_int(
            usage.get("input_tokens", usage.get("inputTokens")))
        out["tokens_out"] = common.as_int(
            usage.get("output_tokens", usage.get("outputTokens")))
        out["preview"] = (_text_of_content(body.get("content"))
                          or _text_of_content(
                              ((body.get("output") or {}).get("message") or {})
                              .get("content")))
        out["stop"] = body.get("stop_reason") or body.get("stopReason")
        return out

    # Titan: token counts either side of a `results` list.
    if "inputTextTokenCount" in body:
        results = body.get("results") or []
        first = results[0] if results and isinstance(results[0], dict) else {}
        out["tokens_in"] = common.as_int(body.get("inputTextTokenCount"))
        out["tokens_out"] = common.as_int(first.get("tokenCount"))
        out["preview"] = first.get("outputText")
        out["stop"] = first.get("completionReason")
        return out

    # Meta Llama on Bedrock: flat, with its own spelling again.
    if "generation" in body:
        out["tokens_in"] = common.as_int(body.get("prompt_token_count"))
        out["tokens_out"] = common.as_int(body.get("generation_token_count"))
        out["preview"] = body.get("generation")
        out["stop"] = body.get("stop_reason")
    return out


def substitute_text(body: Dict[str, Any], text: str) -> Dict[str, Any]:
    """``body`` with its answer replaced by ``text``, in its own family's shape.

    The inverse of :func:`read_body`, and it exists for ``--patch
    llm.response=``. A fork substituting a completion has to hand the agent
    something the agent can *parse*: Bedrock is one endpoint in front of
    families that agree on nothing, so a generic OpenAI-shaped body -- which is
    what the httpx shim fabricates -- would make a Titan agent raise `KeyError`
    on `results` instead of showing what it does with the new answer.

    So the recorded parent response is rewritten in place rather than replaced.
    That keeps the family, the field names, and every key the decoder does not
    read, and it means a family added to `read_body` later fails visibly here
    (the text is unchanged) rather than silently returning a wrong shape.
    """
    body = json.loads(json.dumps(body))  # deep copy; the parent is not ours

    usage = body.get("usage")
    if isinstance(usage, dict):
        # Anthropic-on-Bedrock, Nova, and the Converse API. Whichever content
        # list is present is the one carrying the answer.
        for holder in (body, (body.get("output") or {}).get("message") or {}):
            content = holder.get("content")
            if isinstance(content, list):
                blocks = [b for b in content
                          if isinstance(b, dict) and isinstance(b.get("text"), str)]
                if blocks:
                    # The first block carries the whole answer; the rest go,
                    # because leaving them would append the original text.
                    holder["content"] = [dict(blocks[0], text=text)]
                    return body
        return body

    if "inputTextTokenCount" in body:
        results = body.get("results") or []
        first = results[0] if results and isinstance(results[0], dict) else {}
        body["results"] = [dict(first, outputText=text)]
        return body

    if "generation" in body:
        body["generation"] = text
        return body

    return body


def read_stream(payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Tokens and a preview from a decoded event stream.

    The token counts come from the metrics event Bedrock appends to every
    stream regardless of family, which is the one thing that does not need a
    per-family branch.
    """
    out: Dict[str, Any] = {}
    text: List[str] = []
    for payload in payloads:
        metrics = payload.get(METRICS_KEY)
        if isinstance(metrics, dict):
            out["tokens_in"] = common.as_int(metrics.get("inputTokenCount"))
            out["tokens_out"] = common.as_int(metrics.get("outputTokenCount"))

        # Anthropic-style deltas.
        delta = payload.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            text.append(delta["text"])
        # Nova / Converse deltas.
        if isinstance(delta, dict) and isinstance(delta.get("content"), list):
            piece = _text_of_content(delta["content"])
            if piece:
                text.append(piece)
        block = payload.get("contentBlockDelta")
        if isinstance(block, dict):
            inner = block.get("delta")
            if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                text.append(inner["text"])
        # Titan and Meta stream whole segments rather than deltas.
        for key in ("outputText", "generation"):
            if isinstance(payload.get(key), str):
                text.append(payload[key])
        if payload.get("stopReason") or payload.get("completionReason"):
            out["stop"] = payload.get("stopReason") or payload.get("completionReason")

    if text:
        out["preview"] = "".join(text)
    return out


def decode(event: Event) -> Optional[Dict[str, Any]]:
    _, path = common.url_parts(event)
    model = model_from_path(path)
    if model is None:  # pragma: no cover - matches() already checked
        return None

    chunks = _recorded_stream_bytes(event)
    if chunks is not None:
        found = read_stream(frame_payloads(chunks))
    else:
        body = common.response_json(event)
        found = read_body(body) if isinstance(body, dict) else {}

    tokens_in = found.get("tokens_in")
    tokens_out = found.get("tokens_out")
    res: Dict[str, Any] = {}
    if tokens_in is not None or tokens_out is not None:
        res["tokens"] = {"in": tokens_in, "out": tokens_out}
    excerpt = found.get("preview")
    if isinstance(excerpt, str) and excerpt:
        res["preview"] = common.preview(excerpt)
    if found.get("stop"):
        res["stop"] = found["stop"]

    meta: Dict[str, Any] = {}
    cost = pricing.cost_usd(model, tokens_in, tokens_out)
    if cost is not None:
        meta["cost_usd"] = cost

    return {
        "kind": "llm",
        "req": {"provider": NAME, "model": model,
                "streamed": chunks is not None},
        "res": res,
        "meta": meta,
    }
