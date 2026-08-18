"""Verification against the real OpenAI and Anthropic SDKs.

The transport shim knows nothing about either. These tests exist to prove
that: the same interception code records both, and the only provider-specific
logic anywhere is a pure decoder reading the recorded bytes afterwards.

Both SDKs are pointed at a local server. No network, no keys, no cost.
"""

import json

import pytest

import reeltime as tape

anthropic = pytest.importorskip("anthropic")
openai = pytest.importorskip("openai")

OPENAI_KEY = "sk-" + "A1b2C3d4E5f6G7h8I9j0" * 2
ANTHROPIC_KEY = "sk-ant-api03-" + "Zz9Yy8Xx7" * 6

CHAT_RESPONSE = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1755000000,
    "model": "gpt-4o-mini-2024-07-18",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello there"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
}

MESSAGES_RESPONSE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-5",
    "content": [{"type": "text", "text": "Hello there"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 12, "output_tokens": 3},
}

CHAT_STREAM = [
    'data: {"id":"1","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"}}]}\n\n',
    'data: {"id":"1","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":"stop"}]}\n\n',
    'data: {"id":"1","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}\n\n',
    "data: [DONE]\n\n",
]

MESSAGES_STREAM = [
    'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1",'
    '"type":"message","role":"assistant","model":"claude-sonnet-4-5","content":[],'
    '"stop_reason":null,"usage":{"input_tokens":10,"output_tokens":1}}}\n\n',
    'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"text","text":""}}\n\n',
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"Hel"}}\n\n',
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"lo"}}\n\n',
    'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
    'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    '"usage":{"output_tokens":5}}\n\n',
    'event: message_stop\ndata: {"type":"message_stop"}\n\n',
]


def only_event(run):
    events = tape.read_trace(run.path).events
    assert len(events) == 1, [e.kind for e in events]
    return events[0]


# -- OpenAI --------------------------------------------------------------


def test_openai_chat_completion(recording, server):
    server.route("/v1/chat/completions", json=CHAT_RESPONSE)
    client = openai.OpenAI(api_key=OPENAI_KEY, base_url=server.base_url + "/v1",
                           max_retries=0)
    reply = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
    )
    tape.uninstall()

    assert reply.choices[0].message.content == "Hello there"
    event = only_event(recording)
    assert event.kind == "llm"
    assert event.req["provider"] == "openai"
    assert event.req["model"] == "gpt-4o-mini"
    assert event.req["n_messages"] == 1
    assert event.req["temperature"] == 0.7
    assert event.res["tokens"] == {"in": 12, "out": 3}
    assert event.res["preview"] == "Hello there"
    assert event.res["finish_reason"] == "stop"
    # gpt-4o-mini at 0.15/0.60 per 1M: 12 in, 3 out.
    assert event.meta["cost_usd"] == pytest.approx(12 / 1e6 * 0.15 + 3 / 1e6 * 0.60)
    # The raw exchange is still there underneath the decoded view.
    assert event.req["body"]["json"]["messages"][0]["content"] == "hi"


def test_openai_streaming_reads_usage_from_the_terminal_chunk(recording, server):
    server.route("/v1/chat/completions", sse=CHAT_STREAM)
    client = openai.OpenAI(api_key=OPENAI_KEY, base_url=server.base_url + "/v1",
                           max_retries=0)
    text = ""
    for chunk in client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        stream_options={"include_usage": True},
    ):
        if chunk.choices and chunk.choices[0].delta.content:
            text += chunk.choices[0].delta.content
    tape.uninstall()

    assert text == "Hello"
    event = only_event(recording)
    assert event.kind == "llm"
    assert event.res["streamed"] is True
    assert event.res["tokens"] == {"in": 10, "out": 2}
    assert event.res["preview"] == "Hello"
    assert event.meta["cost_usd"] > 0
    assert event.res["stream"]["chunks"] == CHAT_STREAM


# -- Anthropic -----------------------------------------------------------


def test_anthropic_messages(recording, server):
    server.route("/v1/messages", json=MESSAGES_RESPONSE)
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, base_url=server.base_url,
                                 max_retries=0)
    reply = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=64,
        system="Be brief.",
        messages=[{"role": "user", "content": "hi"}],
    )
    tape.uninstall()

    assert reply.content[0].text == "Hello there"
    event = only_event(recording)
    assert event.kind == "llm"
    assert event.req["provider"] == "anthropic"
    assert event.req["model"] == "claude-sonnet-4-5"
    assert event.req["has_system"] is True
    assert event.req["max_tokens"] == 64
    assert event.res["tokens"] == {"in": 12, "out": 3}
    assert event.res["preview"] == "Hello there"
    assert event.res["stop_reason"] == "end_turn"
    assert event.meta["cost_usd"] == pytest.approx(12 / 1e6 * 3.0 + 3 / 1e6 * 15.0)


def test_anthropic_streaming_splits_usage_across_two_events(recording, server):
    server.route("/v1/messages", sse=MESSAGES_STREAM)
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, base_url=server.base_url,
                                 max_retries=0)
    text = ""
    with client.messages.stream(
        model="claude-sonnet-4-5",
        max_tokens=64,
        messages=[{"role": "user", "content": "hi"}],
    ) as stream:
        for piece in stream.text_stream:
            text += piece
    tape.uninstall()

    assert text == "Hello"
    event = only_event(recording)
    assert event.kind == "llm"
    assert event.res["streamed"] is True
    # input_tokens from message_start, output_tokens from message_delta.
    assert event.res["tokens"] == {"in": 10, "out": 5}
    assert event.res["preview"] == "Hello"
    assert event.res["stop_reason"] == "end_turn"


# -- provider-agnosticism ------------------------------------------------


def test_both_sdks_record_through_the_same_code_path(recording, server):
    server.route("/v1/chat/completions", json=CHAT_RESPONSE)
    server.route("/v1/messages", json=MESSAGES_RESPONSE)
    openai.OpenAI(api_key=OPENAI_KEY, base_url=server.base_url + "/v1",
                  max_retries=0).chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
    )
    anthropic.Anthropic(api_key=ANTHROPIC_KEY, base_url=server.base_url,
                        max_retries=0).messages.create(
        model="claude-sonnet-4-5", max_tokens=8,
        messages=[{"role": "user", "content": "hi"}]
    )
    tape.uninstall()

    events = tape.read_trace(recording.path).events
    assert [e.req["provider"] for e in events] == ["openai", "anthropic"]
    # Identical transport-level structure: nothing in the shim knows a
    # provider exists.
    for event in events:
        assert set(event.req) >= {"method", "url", "headers", "body"}
        assert event.req["method"] == "POST"
        assert set(event.res) >= {"status", "headers", "body", "tokens"}


def test_an_unknown_provider_stays_a_plain_http_event(recording, server):
    server.route("/v1/chat/completions", json={"result": "who knows"})
    import httpx

    httpx.post(server.base_url + "/v1/chat/completions", json={"model": "mystery"})
    tape.uninstall()

    event = only_event(recording)
    assert event.kind == "http"          # not an error, just unenriched
    assert "tokens" not in (event.res or {})
    assert event.meta.get("cost_usd") is None


# -- redaction on the real SDK path --------------------------------------


def test_neither_sdks_key_reaches_disk(recording, server):
    server.route("/v1/chat/completions", json=CHAT_RESPONSE)
    server.route("/v1/messages", json=MESSAGES_RESPONSE)
    openai.OpenAI(api_key=OPENAI_KEY, base_url=server.base_url + "/v1",
                  max_retries=0).chat.completions.create(
        model="gpt-4o-mini",
        # A key pasted into the prompt as well as sent in the header.
        messages=[{"role": "user", "content": "my key is " + OPENAI_KEY}],
    )
    anthropic.Anthropic(api_key=ANTHROPIC_KEY, base_url=server.base_url,
                        max_retries=0).messages.create(
        model="claude-sonnet-4-5", max_tokens=8,
        messages=[{"role": "user", "content": "my key is " + ANTHROPIC_KEY}],
    )
    summary = tape.uninstall()

    on_disk = recording.path.read_text()
    blobs = "".join(
        p.read_text() for p in (recording.config.tape_dir / "blobs").glob("*")
    )
    for secret in (OPENAI_KEY, ANTHROPIC_KEY):
        assert secret not in on_disk
        assert secret not in blobs
    # The auth headers were dropped by name, and the pasted keys by pattern.
    headers = dict(tape.read_trace(recording.path).events[0].req["headers"])
    assert headers.get("authorization") == "<redacted>"
    assert "<redacted:sk>" in on_disk and "<redacted:sk-ant>" in on_disk
    assert summary.redacted["header"] >= 1
