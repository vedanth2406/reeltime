import json
import logging

import httpx
import pytest

import reeltime as tape
from reeltime.core import decoders
from reeltime.core.decoders import anthropic, common, openai, pricing
from reeltime.core.trace import Event


def make_event(url, request=None, response=None, chunks=None):
    """A recorded http event, as the recorder would have built it."""
    res = {"status": 200, "headers": []}
    if chunks is not None:
        res["headers"] = [["content-type", "text/event-stream"]]
        res["stream"] = {"encoding": "utf-8", "chunks": chunks}
    else:
        res["body"] = {"json": response}
    return Event(
        i=0,
        kind="http",
        site="agent.py:1",
        req={"method": "POST", "url": url, "headers": [], "body": {"json": request}},
        res=res,
    )


# -- pricing -------------------------------------------------------------


def test_pricing_matches_the_longest_prefix():
    assert pricing.lookup("gpt-4o-mini-2024-07-18") == (0.15, 0.60)
    assert pricing.lookup("gpt-4o-2024-11-20") == (2.50, 10.00)
    assert pricing.lookup("claude-sonnet-4-5-20260101") == (3.00, 15.00)


def test_an_azure_style_deployment_prefix_is_stripped():
    assert pricing.lookup("my-deployment/gpt-4o-mini") == (0.15, 0.60)


def test_an_unknown_model_has_no_price():
    assert pricing.lookup("some-new-model-2030") is None
    assert pricing.cost_usd("some-new-model-2030", 100, 100) is None


def test_cost_is_per_million_tokens():
    assert pricing.cost_usd("gpt-4o-mini", 1_000_000, 0) == pytest.approx(0.15)
    assert pricing.cost_usd("gpt-4o-mini", 0, 1_000_000) == pytest.approx(0.60)


def test_missing_token_counts_produce_no_cost():
    assert pricing.cost_usd("gpt-4o-mini", None, None) is None


# -- SSE parsing ---------------------------------------------------------


def test_sse_records_are_parsed_across_chunk_boundaries():
    # One TCP read can carry half an event; the parser has to join first.
    chunks = ['data: {"a"', ': 1}\n\ndata: {"b": 2}\n', "\n"]
    assert common.sse_messages(chunks) == [(None, {"a": 1}), (None, {"b": 2})]


def test_sse_event_names_are_kept():
    chunks = ['event: message_start\ndata: {"type": "message_start"}\n\n']
    assert common.sse_messages(chunks) == [("message_start", {"type": "message_start"})]


def test_the_done_sentinel_is_not_an_event():
    assert common.sse_messages(["data: [DONE]\n\n"]) == []


# -- OpenAI --------------------------------------------------------------


def test_openai_chat_response_is_decoded():
    event = make_event(
        "https://api.openai.com/v1/chat/completions",
        request={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        response={
            "object": "chat.completion",
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )
    out = openai.decode(event)
    assert out["kind"] == "llm"
    assert out["req"] == {"provider": "openai", "model": "gpt-4o-mini", "n_messages": 1}
    assert out["res"]["tokens"] == {"in": 10, "out": 5}
    assert out["res"]["preview"] == "hello"
    assert out["meta"]["cost_usd"] > 0


def test_the_responses_api_token_names_are_accepted():
    event = make_event(
        "https://api.openai.com/v1/responses",
        request={"model": "gpt-4.1-mini"},
        response={"object": "response", "usage": {"input_tokens": 7, "output_tokens": 2}},
    )
    assert openai.decode(event)["res"]["tokens"] == {"in": 7, "out": 2}


def test_openai_is_recognised_behind_a_proxy_host():
    # Matching is by path shape plus a body key check, so gateways, Azure
    # deployments, and local mocks all decode without the shim knowing.
    event = make_event(
        "http://127.0.0.1:9999/v1/chat/completions",
        request={"model": "gpt-4o-mini"},
        response={"choices": [{"message": {"content": "hi"}}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    )
    assert openai.decode(event)["req"]["provider"] == "openai"


# -- Anthropic -----------------------------------------------------------


def test_anthropic_message_response_is_decoded():
    event = make_event(
        "https://api.anthropic.com/v1/messages",
        request={"model": "claude-sonnet-4-5", "max_tokens": 100,
                 "messages": [{"role": "user", "content": "hi"}], "system": "Be brief."},
        response={
            "type": "message",
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )
    out = anthropic.decode(event)
    assert out["kind"] == "llm"
    assert out["req"]["provider"] == "anthropic"
    assert out["req"]["has_system"] is True
    assert out["res"]["tokens"] == {"in": 10, "out": 5}
    assert out["res"]["preview"] == "hello"


def test_anthropic_streaming_usage_comes_from_two_events():
    chunks = [
        'event: message_start\ndata: {"type":"message_start","message":'
        '{"model":"claude-sonnet-4-5","usage":{"input_tokens":11,"output_tokens":1}}}\n\n',
        'event: content_block_delta\ndata: {"type":"content_block_delta",'
        '"delta":{"type":"text_delta","text":"hi"}}\n\n',
        'event: message_delta\ndata: {"type":"message_delta","delta":'
        '{"stop_reason":"end_turn"},"usage":{"output_tokens":9}}\n\n',
    ]
    event = make_event("https://api.anthropic.com/v1/messages",
                       request={"model": "claude-sonnet-4-5"}, chunks=chunks)
    out = anthropic.decode(event)
    assert out["res"]["tokens"] == {"in": 11, "out": 9}
    assert out["res"]["preview"] == "hi"
    assert out["res"]["streamed"] is True


# -- registry ------------------------------------------------------------


def test_an_unrecognised_provider_returns_none():
    event = make_event("https://example.com/v1/predict",
                       request={"prompt": "hi"}, response={"result": "hello"})
    assert decoders.decode(event) is None


def test_the_two_decoders_do_not_claim_each_others_calls():
    chat = make_event("https://api.openai.com/v1/chat/completions",
                      request={"model": "gpt-4o"},
                      response={"choices": [], "usage": {}})
    messages = make_event("https://api.anthropic.com/v1/messages",
                          request={"model": "claude-sonnet-4-5"},
                          response={"type": "message", "content": [], "usage": {}})
    assert not anthropic.matches(chat)
    assert not openai.matches(messages)


def test_non_http_events_are_never_decoded():
    assert decoders.decode(Event(i=0, kind="tool", site="a.py:1")) is None


@pytest.fixture
def raising_decoder():
    bad = decoders.Decoder("explodes", lambda event: True,
                           lambda event: 1 / 0)
    decoders.register(bad, first=True)
    try:
        yield bad
    finally:
        decoders.REGISTRY.remove(bad)


def test_a_decoder_that_raises_never_fails_the_recording(
    recording, server, raising_decoder, caplog
):
    url = server.route("/api", json={"ok": True})
    with caplog.at_level(logging.DEBUG, logger="reeltime"):
        httpx.post(url, json={"q": 1})
        httpx.post(url, json={"q": 2})
    tape.uninstall()

    events = tape.read_trace(recording.path).events
    assert len(events) == 2
    assert all(e.kind == "http" for e in events)          # written unenriched
    assert events[0].res["body"]["json"] == {"ok": True}  # and complete
    # Logged once, not once per event.
    assert sum("decoder raised" in r.message for r in caplog.records) == 1


def test_decoding_can_be_disabled(tape_dir, server):
    server.route("/v1/chat/completions", json={
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    run = tape.install(tape_dir=tape_dir, collect_git=False, decode=False)
    httpx.post(server.base_url + "/v1/chat/completions", json={"model": "gpt-4o-mini"})
    tape.uninstall()

    event = tape.read_trace(run.path).events[0]
    assert event.kind == "http"
    assert "tokens" not in event.res


def test_a_written_trace_can_be_decoded_afterwards(tape_dir, server):
    # The property that makes `tape reindex` possible in M4: the same pure
    # function works over a trace read back from disk, blobs and all.
    big = "x" * 20_000
    server.route("/v1/chat/completions", json={
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    })
    run = tape.install(tape_dir=tape_dir, collect_git=False, decode=False)
    httpx.post(server.base_url + "/v1/chat/completions",
               json={"model": "gpt-4o-mini", "padding": big})
    tape.uninstall()

    event = tape.read_trace(run.path).events[0]
    assert event.req["body"].startswith("blob:")     # externalised on write
    out = decoders.decode_resolved(event, run.blobs)
    assert out["kind"] == "llm"
    assert out["res"]["tokens"] == {"in": 3, "out": 4}
