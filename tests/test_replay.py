"""Replay: the core of the tool.

Every test here records a real agent and then re-runs it against the tape.
"""

import asyncio
import importlib
import json
import random
import sys
import textwrap
import time
import uuid

import httpx
import pytest

import reeltime as tape
from reeltime.core import spans
from reeltime.errors import TapeMiss

CHUNKS = [
    'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n',
    'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
    "data: [DONE]\n\n",
]


@pytest.fixture
def editable(tmp_path, monkeypatch):
    """A module the test can rewrite and reload between record and replay."""
    monkeypatch.syspath_prepend(str(tmp_path))
    # Bytecode caching validates on (mtime, size). Two edits of the same length
    # written in the same second look identical to it, and the reload silently
    # returns the old code -- which makes an edit-resilience test pass for the
    # wrong reason.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    path = tmp_path / "agent_under_test.py"

    def write(source):
        path.write_text(textwrap.dedent(source))
        importlib.invalidate_caches()
        existing = sys.modules.get("agent_under_test")
        if existing is not None:
            return importlib.reload(existing)
        return importlib.import_module("agent_under_test")

    try:
        yield write
    finally:
        sys.modules.pop("agent_under_test", None)


def record(tape_dir, fn, run_id="01REC"):
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id=run_id):
        return fn()


def replay(tape_dir, fn, run_id="01REC", **kwargs):
    with tape.session("replay", tape_dir=tape_dir, replay=run_id, **kwargs) as run:
        result = fn()
    return result, run.summary


# -- round-trip fidelity -------------------------------------------------


def test_a_recorded_agent_replays_identically_with_the_network_gone(
    tape_dir, server, monkeypatch
):
    url = server.route("/v1/chat", json={"reply": "hello"})

    @tape.tool
    def read_file(path):
        return "contents of " + path

    def agent():
        return {
            "notes": read_file("notes.md"),
            "reply": httpx.post(url, json={"prompt": "hi"}).json()["reply"],
            "seed": random.random(),
            "id": str(uuid.uuid4()),
            "clock": time.time(),
        }

    recorded = record(tape_dir, agent)
    server.received.clear()

    replayed, summary = replay(tape_dir, agent)
    assert replayed == recorded          # final state, value for value
    assert summary.drifts == []          # every match was tier 1
    assert server.received == []         # nothing reached the network


def test_every_intermediate_request_payload_is_byte_identical(
    tape_dir, server, monkeypatch
):
    from reeltime.core.http import httpx_shim

    url = server.route("/v1/chat", json={"reply": "ok"})
    seen = []
    original = httpx_shim._request_payload

    def probe(engine, request):
        payload = original(engine, request)
        seen.append(json.dumps(payload["body"], sort_keys=True))
        return payload

    monkeypatch.setattr(httpx_shim, "_request_payload", probe)

    def agent():
        out = []
        for i in range(3):
            out.append(httpx.post(url, json={"turn": i, "history": out}).json())
        return out

    record(tape_dir, agent)
    recorded_payloads, seen[:] = list(seen), []
    replay(tape_dir, agent)

    assert len(recorded_payloads) == 3
    assert seen == recorded_payloads     # what the agent sent, byte for byte


def test_replay_makes_no_network_calls_at_all(tape_dir, server, monkeypatch):
    url = server.route("/v1/chat", json={"reply": "ok"})

    def agent():
        return httpx.post(url, json={"prompt": "hi"}).json()

    record(tape_dir, agent)

    def explode(*args, **kwargs):
        raise AssertionError("replay attempted a real request")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", explode)
    result, summary = replay(tape_dir, agent)
    assert result == {"reply": "ok"}
    assert summary.events == 1


def test_ambient_values_are_identical_on_replay(tape_dir):
    def agent():
        return [random.random(), random.randint(1, 100), str(uuid.uuid4()),
                time.time(), random.choice("abcdef")]

    recorded = record(tape_dir, agent)
    replayed, _ = replay(tape_dir, agent)
    assert replayed == recorded


def test_a_shuffle_replays_the_same_permutation(tape_dir):
    def agent():
        deck = list(range(10))
        random.shuffle(deck)
        return deck

    recorded = record(tape_dir, agent)
    replayed, _ = replay(tape_dir, agent)
    assert replayed == recorded


def test_a_replayed_tool_body_never_runs(tape_dir):
    calls = []

    @tape.tool
    def delete_file(path):
        calls.append(path)      # stands in for the side effect
        return "deleted " + path

    def agent():
        return delete_file("b.txt")

    record(tape_dir, agent)
    assert calls == ["b.txt"]

    result, _ = replay(tape_dir, agent)
    assert result == "deleted b.txt"
    assert calls == ["b.txt"]   # not called a second time


def test_a_recorded_tool_error_is_raised_again(tape_dir):
    @tape.tool
    def read_file(path):
        raise FileNotFoundError(path)

    def agent():
        try:
            read_file("missing.md")
            return "no error"
        except FileNotFoundError as exc:
            return "caught {}".format(exc)

    recorded = record(tape_dir, agent)
    replayed, _ = replay(tape_dir, agent)
    assert recorded == replayed == "caught missing.md"


def test_a_recorded_connection_error_is_raised_again(tape_dir):
    def agent():
        try:
            httpx.get("http://127.0.0.1:9/nope", timeout=0.3)
            return "reached it"
        except httpx.ConnectError:
            return "connect failed"

    assert record(tape_dir, agent) == "connect failed"
    replayed, _ = replay(tape_dir, agent)
    assert replayed == "connect failed"


def test_a_decoded_llm_call_replays(tape_dir, server):
    # The decoder relabels the event as `llm` on the way in, while the
    # transport asks for `http` on the way out. Folding those two is what makes
    # a real provider call replayable at all.
    url = server.route("/v1/chat/completions", json={
        "object": "chat.completion",
        "model": "gpt-4o-mini",
        "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    })

    def agent():
        return httpx.post(url, json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
        }).json()["choices"][0]["message"]["content"]

    recorded = record(tape_dir, agent)
    assert recorded == "hello"
    assert tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events[0].kind == "llm"

    server.received.clear()
    replayed, summary = replay(tape_dir, agent)
    assert replayed == "hello"
    assert summary.drifts == [] and server.received == []


def test_several_decoded_llm_turns_replay_in_order(tape_dir, server):
    url = server.route("/v1/chat/completions", json={
        "object": "chat.completion", "model": "gpt-4o-mini",
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    })

    def agent():
        history = []
        for turn in range(4):
            reply = httpx.post(url, json={
                "model": "gpt-4o-mini",
                "messages": history + [{"role": "user", "content": "turn %d" % turn}],
            }).json()
            history.append({"role": "assistant",
                            "content": reply["choices"][0]["message"]["content"]})
        return history

    recorded = record(tape_dir, agent)
    server.received.clear()
    replayed, summary = replay(tape_dir, agent)
    assert replayed == recorded
    assert summary.events == 4 and summary.drifts == []
    assert server.received == []


# -- code-edit resilience ------------------------------------------------

BASE_AGENT = """
    import reeltime as tape

    @tape.tool
    def read_file(path):
        return "contents of " + path

    def run():
        return read_file("notes.md")
"""


def test_an_inserted_line_above_the_call_site_still_replays(tape_dir, editable):
    module = editable(BASE_AGENT)
    record(tape_dir, module.run)

    # The most ordinary edit there is: one more import at the top.
    module = editable("""
        import os
        import reeltime as tape

        @tape.tool
        def read_file(path):
            return "contents of " + path

        def run():
            return read_file("notes.md")
    """)
    result, summary = replay(tape_dir, module.run)

    assert result == "contents of notes.md"
    assert len(summary.drifted) == 1
    assert "line moved" in summary.drifted[0].reason
    assert summary.fuzzy == []
    assert "1 event matched with drifted content" in " ".join(summary.notes())


def test_a_changed_argument_still_replays_with_a_drift_warning(tape_dir, editable):
    module = editable(BASE_AGENT)
    record(tape_dir, module.run)

    module = editable(BASE_AGENT.replace('"notes.md"', '"OTHER.md"'))
    result, summary = replay(tape_dir, module.run)

    # The recorded result is served: this is the point of tier 2. The user is
    # told the content drifted rather than being stopped.
    assert result == "contents of notes.md"
    assert summary.drifted[0].reason == "content changed"


def test_strict_mode_refuses_a_drifted_match(tape_dir, editable):
    module = editable(BASE_AGENT)
    record(tape_dir, module.run)

    module = editable(BASE_AGENT.replace('"notes.md"', '"OTHER.md"'))
    with pytest.raises(TapeMiss) as caught:
        replay(tape_dir, module.run, strictness="strict")
    assert "would match without --strict" in str(caught.value)


def test_a_call_moved_to_another_function_needs_loose(tape_dir, editable):
    module = editable(BASE_AGENT)
    record(tape_dir, module.run)

    moved = """
        import reeltime as tape

        @tape.tool
        def read_file(path):
            return "contents of " + path

        def helper():
            return read_file("notes.md")

        def run():
            return helper()
    """
    module = editable(moved)

    with pytest.raises(TapeMiss):
        replay(tape_dir, module.run)

    module = editable(moved)
    result, summary = replay(tape_dir, module.run, strictness="loose")
    assert result == "contents of notes.md"
    assert len(summary.fuzzy) == 1
    assert "call site" in summary.fuzzy[0].reason
    assert "matched by content hash alone" in " ".join(summary.notes())


# -- TapeMiss ------------------------------------------------------------


def test_a_missing_event_names_the_call_site_and_the_candidates(tape_dir):
    @tape.tool
    def step(n):
        return n * 2

    def agent():
        return [step(1), step(2), step(3)]

    record(tape_dir, agent)

    # Delete the middle event, as if the trace had been hand-edited.
    path = tape_dir / "runs" / "01REC.jsonl"
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:2] + lines[3:]) + "\n")

    with pytest.raises(TapeMiss) as caught:
        replay(tape_dir, agent)

    message = str(caught.value)
    assert "no recorded tool event matches this call" in message
    assert "test_replay.py" in message          # the real call site
    assert "sent" in message                    # a content preview
    assert "nearest unconsumed events" in message
    assert "#2" in message                      # the surviving neighbour
    assert "--loose" in message                 # and what to do about it


def test_running_off_the_end_of_the_tape_says_so(tape_dir):
    @tape.tool
    def step(n):
        return n

    def agent(count):
        return [step(n) for n in range(count)]

    record(tape_dir, lambda: agent(1))

    with pytest.raises(TapeMiss) as caught:
        replay(tape_dir, lambda: agent(2))
    assert "ran more times than it was recorded" in str(caught.value)


def test_the_error_carries_structured_fields_not_just_text(tape_dir):
    @tape.tool
    def step(n):
        return n

    def agent(count):
        return [step(n) for n in range(count)]

    record(tape_dir, lambda: agent(1))
    with pytest.raises(TapeMiss) as caught:
        replay(tape_dir, lambda: agent(2))

    error = caught.value
    assert error.kind == "tool"
    assert error.site.endswith("test_replay.py:{}".format(error.site.split(":")[-1]))
    assert error.strictness == "default"
    assert error.run_id == "01REC"


# -- concurrency ---------------------------------------------------------


def test_parallel_tools_in_separate_spans_replay_under_a_new_order(tape_dir):
    @tape.tool
    def fetch(name):
        return "value:" + name

    async def agent(delays):
        async def one(name, delay):
            with tape.span(name):
                await asyncio.sleep(delay)
                return fetch(name)

        return await asyncio.gather(
            one("a", delays[0]), one("b", delays[1]), one("c", delays[2])
        )

    recorded = record(tape_dir, lambda: asyncio.run(agent([0.03, 0.01, 0.02])))
    assert recorded == ["value:a", "value:b", "value:c"]

    # Completion order is reversed on replay. Span-scoped matching means the
    # tape does not care which task gets there first.
    replayed, summary = replay(tape_dir, lambda: asyncio.run(agent([0.01, 0.02, 0.03])))
    assert replayed == recorded
    assert summary.drifts == []


def test_same_span_concurrency_replays_in_recorded_order(tape_dir):
    @tape.tool
    def step(n):
        return n * 10

    async def agent():
        async def one(n):
            return step(n)

        return await asyncio.gather(one(0), one(1), one(2))

    recorded = record(tape_dir, lambda: asyncio.run(agent()))
    replayed, summary = replay(tape_dir, lambda: asyncio.run(agent()))
    assert replayed == recorded == [0, 10, 20]
    assert summary.drifts == []


# -- streaming -----------------------------------------------------------


def test_a_stream_replays_chunk_for_chunk(tape_dir, server):
    url = server.route("/stream", sse=CHUNKS)

    def agent():
        with httpx.stream("POST", url, json={}) as response:
            return [chunk.decode() for chunk in response.iter_bytes()]

    recorded = record(tape_dir, agent)
    assert recorded == CHUNKS

    server.received.clear()
    replayed, summary = replay(tape_dir, agent)
    assert replayed == CHUNKS            # boundaries preserved, not reassembled
    assert server.received == []


def test_an_async_stream_replays_chunk_for_chunk(tape_dir, server):
    url = server.route("/stream", sse=CHUNKS)

    async def run():
        async with httpx.AsyncClient() as client:
            async with client.stream("POST", url, json={}) as response:
                return [chunk.decode() async for chunk in response.aiter_bytes()]

    recorded = record(tape_dir, lambda: asyncio.run(run()))
    replayed, _ = replay(tape_dir, lambda: asyncio.run(run()))
    assert replayed == recorded == CHUNKS


def test_streams_replay_instantly_by_default(tape_dir, server):
    url = server.route("/stream", sse=CHUNKS, chunk_delay=0.08)

    def agent():
        with httpx.stream("POST", url, json={}) as response:
            return [c for c in response.iter_bytes()]

    record(tape_dir, agent)
    started = time.perf_counter()
    replay(tape_dir, agent)
    # The recorded stream took ~240ms of wall clock; replay does not.
    assert time.perf_counter() - started < 0.15


def test_realtime_reinstates_the_recorded_gaps(tape_dir, server):
    url = server.route("/stream", sse=CHUNKS, chunk_delay=0.08)

    def agent():
        with httpx.stream("POST", url, json={}) as response:
            return [c for c in response.iter_bytes()]

    record(tape_dir, agent)
    started = time.perf_counter()
    replay(tape_dir, agent, realtime=True)
    # Three chunks at ~80ms apart: the timing is reproduced, which is what
    # makes a barge-in or early-cancel bug reproducible.
    assert time.perf_counter() - started > 0.15


def test_chunk_offsets_are_recorded(tape_dir, server):
    url = server.route("/stream", sse=CHUNKS, chunk_delay=0.05)

    def agent():
        with httpx.stream("POST", url, json={}) as response:
            return [c for c in response.iter_bytes()]

    record(tape_dir, agent)
    event = tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events[0]
    offsets = event.res["stream"]["offsets"]
    assert len(offsets) == 3
    assert offsets == sorted(offsets)
    assert offsets[-1] > 50      # ms, and increasing


# -- stopping ------------------------------------------------------------


def test_to_stops_after_the_named_event(tape_dir):
    seen = []

    @tape.tool
    def step(n):
        seen.append(n)
        return n

    def agent():
        for n in range(5):
            step(n)

    record(tape_dir, agent)  # noqa: the bodies run here
    seen.clear()

    from reeltime.errors import StopReplay

    with pytest.raises(StopReplay) as caught:
        replay(tape_dir, agent, stop_at=2)
    assert caught.value.stopped_at == 2
    assert seen == []            # bodies never ran


def test_a_stopped_replay_reports_where_it_stopped(tape_dir):
    @tape.tool
    def step(n):
        return n

    def agent():
        return [step(n) for n in range(4)]

    record(tape_dir, agent)

    tape.install("replay", tape_dir=tape_dir, replay="01REC", stop_at=1)
    try:
        with pytest.raises(BaseException):
            agent()
    finally:
        summary = tape.uninstall()
    assert summary.stopped_at == 1
    assert "stopped after event 1" in " ".join(summary.notes())


# -- summary -------------------------------------------------------------


def test_unconsumed_events_are_reported(tape_dir):
    @tape.tool
    def step(n):
        return n

    def agent(count):
        return [step(n) for n in range(count)]

    record(tape_dir, lambda: agent(4))
    _, summary = replay(tape_dir, lambda: agent(2))

    assert summary.events == 2
    assert len(summary.unconsumed) == 2
    assert "never requested" in " ".join(summary.notes())


def test_a_clean_replay_has_nothing_to_report(tape_dir):
    @tape.tool
    def step(n):
        return n

    def agent():
        return [step(n) for n in range(3)]

    record(tape_dir, agent)
    _, summary = replay(tape_dir, agent)
    assert summary.notes() == []
    assert "replayed 3 events" in summary.line()


def test_replay_reports_its_speedup(tape_dir, server):
    url = server.route("/slow", json={"ok": True})

    def agent():
        return [httpx.post(url, json={"n": n}).json() for n in range(3)]

    record(tape_dir, agent)
    _, summary = replay(tape_dir, agent)
    assert summary.speedup is not None and summary.speedup > 1


def test_recording_is_refused_while_replaying(tape_dir):
    @tape.tool
    def step(n):
        return n

    record(tape_dir, lambda: step(1))
    tape.install("replay", tape_dir=tape_dir, replay="01REC")
    try:
        with pytest.raises(Exception, match="cannot record while replaying"):
            tape.record_event("tool", {"name": "sneaky"})
    finally:
        tape.uninstall()


# -- requests ------------------------------------------------------------


def test_a_requests_call_replays(tape_dir, server):
    import requests

    url = server.route("/api", json={"reply": "ok"})

    def agent():
        response = requests.post(url, json={"prompt": "hi"})
        return response.status_code, response.json(), response.headers["content-type"]

    recorded = record(tape_dir, agent)
    server.received.clear()

    replayed, summary = replay(tape_dir, agent)
    assert replayed == recorded
    assert server.received == []
    assert summary.drifts == []


def test_a_recorded_requests_failure_is_raised_again(tape_dir):
    import requests

    def agent():
        try:
            requests.get("http://127.0.0.1:9/nope", timeout=0.3)
            return "reached it"
        except requests.exceptions.ConnectionError:
            return "connect failed"

    assert record(tape_dir, agent) == "connect failed"
    replayed, _ = replay(tape_dir, agent)
    assert replayed == "connect failed"
