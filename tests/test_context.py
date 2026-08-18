"""The context view: what the model actually read.

This is the headline feature, so its output is asserted rather than eyeballed.
"""

import json

import pytest

from reeltime.core import context
from reeltime.core.context import Context, Glyphs, Message, ToolCall
from reeltime.core.trace import Event

LISTING = "\n".join("line {:03d}".format(i) for i in range(40))


def llm_event(request, response=None, index=0, site="agent.py:88",
              qual="agent.py::ask"):
    return Event(
        i=index, kind="llm", site=site, qual=qual,
        req={"method": "POST", "url": "https://api.openai.com/v1/chat/completions",
             "headers": [], "body": {"json": request}},
        res={"status": 200, "headers": [],
             "body": {"json": response or {
                 "object": "chat.completion",
                 "choices": [{"message": {"content": "ok"}}],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 5}}},
             "tokens": {"in": 100, "out": 5}, "preview": "ok"},
        meta={"cost_usd": 0.0031},
    )


def anthropic_event(request, index=0):
    return Event(
        i=index, kind="llm", site="agent.py:12", qual="agent.py::ask",
        req={"method": "POST", "url": "https://api.anthropic.com/v1/messages",
             "headers": [], "body": {"json": request}},
        res={"status": 200, "headers": [], "body": {"json": {
            "type": "message", "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 9, "output_tokens": 2}}},
            "tokens": {"in": 9, "out": 2}, "preview": "ok"},
    )


# -- extraction ----------------------------------------------------------


def test_an_openai_message_array_is_extracted():
    assembled = context.from_event(llm_event({
        "model": "gpt-4o-mini",
        "temperature": 0.7,
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "hi"},
        ],
    }))
    assert assembled.provider == "openai"
    assert assembled.model == "gpt-4o-mini"
    assert [(m.role, m.text) for m in assembled.messages] == [
        ("system", "Be concise."), ("user", "hi")]
    assert assembled.params["temperature"] == 0.7
    assert assembled.tokens == {"in": 100, "out": 5}
    assert assembled.cost_usd == 0.0031


def test_anthropics_system_field_is_hoisted_into_the_array():
    # It is part of what the model read, and it is the field people most often
    # get wrong -- leaving it out of the context view would hide that.
    assembled = context.from_event(anthropic_event({
        "model": "claude-sonnet-4-5",
        "system": "Be concise.",
        "messages": [{"role": "user", "content": "hi"}],
    }))
    assert [(m.role, m.text) for m in assembled.messages] == [
        ("system", "Be concise."), ("user", "hi")]
    assert assembled.messages[0].hoisted is True
    assert "hoisted" in assembled.messages[0].shape


def test_structured_content_blocks_are_flattened():
    assembled = context.from_event(anthropic_event({
        "model": "claude-sonnet-4-5",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image", "source": {"data": "..."}},
            {"type": "text", "text": "what is it?"},
        ]}],
    }))
    message = assembled.messages[0]
    assert message.text == "look at this\nwhat is it?"
    assert message.images == 1
    assert "1 image" in message.shape


def test_openai_tool_calls_are_extracted():
    assembled = context.from_event(llm_event({
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "assistant", "content": None, "tool_calls": [
                {"type": "function", "function": {
                    "name": "delete_file",
                    "arguments": json.dumps({"path": "b.txt"})}}]},
            {"role": "tool", "name": "delete_file", "tool_call_id": "c1",
             "content": "deleted"},
        ],
        "tools": [{"type": "function", "function": {"name": "delete_file"}}],
    }))
    assistant, tool = assembled.messages
    assert assistant.tool_calls[0].name == "delete_file"
    assert assistant.tool_calls[0].args == {"path": "b.txt"}
    assert "1 tool call" in assistant.shape
    assert tool.tool_name == "delete_file"
    assert assembled.tools == ["delete_file"]


def test_anthropic_tool_use_blocks_are_extracted():
    assembled = context.from_event(anthropic_event({
        "model": "claude-sonnet-4-5",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "search", "input": {"q": "cats"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "found 3"}]},
        ],
    }))
    assert assembled.messages[0].tool_calls[0].name == "search"
    assert assembled.messages[1].text == "found 3"


def test_the_responses_api_shape_is_understood():
    assembled = context.from_event(llm_event({
        "model": "gpt-4.1-mini",
        "instructions": "Be terse.",
        "input": "what is 2+2?",
    }, response={"object": "response", "usage": {"input_tokens": 3, "output_tokens": 1}}))
    assert [(m.role, m.text) for m in assembled.messages] == [
        ("system", "Be terse."), ("user", "what is 2+2?")]


def test_a_non_llm_event_has_no_context():
    assert context.from_event(Event(i=0, kind="tool", site="a.py:1")) is None


def test_blob_references_are_resolved(tape_dir, server):
    import httpx

    import reeltime as tape

    big = "x" * 20_000
    url = server.route("/v1/chat/completions", json={
        "object": "chat.completion",
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01CTX") as run:
        httpx.post(url, json={"model": "gpt-4o-mini",
                              "messages": [{"role": "user", "content": big}]})

    event = tape.read_trace(run.path).events[0]
    assert event.req["body"].startswith("blob:")
    assembled = context.from_event(event, run.blobs)
    assert assembled.messages[0].text == big


# -- rendering -----------------------------------------------------------


def render(request, **kwargs):
    return context.render(context.from_event(llm_event(request)),
                          glyphs=Glyphs(True), **kwargs)


def test_the_header_states_what_the_call_was():
    out = render({"model": "gpt-4o-mini", "temperature": 0.7,
                  "messages": [{"role": "user", "content": "hi"}]})
    first = out.splitlines()[0]
    assert "event 0" in first and "gpt-4o-mini" in first
    assert "agent.py:88" in first and "(ask)" in first
    assert "1 message" in out
    assert "100 in / 5 out tokens" in out
    assert "$0.0031" in out
    assert "temperature 0.7" in out


def test_each_message_gets_a_labelled_rule():
    out = render({"messages": [{"role": "system", "content": "Be concise."},
                               {"role": "user", "content": "hi"}]})
    assert "[0] system · 11 chars" in out
    assert "[1] user · 2 chars" in out
    assert out.rstrip().endswith("ok")        # the completion is last


def test_a_long_message_collapses_with_a_visible_marker():
    out = render({"messages": [{"role": "user", "content": LISTING}]})
    assert "line 000" in out                  # the head survives
    assert "line 039" in out                  # and so does the tail
    assert "line 020" not in out              # the middle does not
    marker = [line for line in out.splitlines() if "elided" in line]
    assert len(marker) == 1
    # Both how much and which lines, so the reader knows what they are missing.
    assert "chars" in marker[0] and "lines 8-37 of 40" in marker[0]


def test_full_disables_collapsing():
    out = render({"messages": [{"role": "user", "content": LISTING}]}, collapse=False)
    assert "line 020" in out
    assert "elided" not in out


def test_short_messages_are_never_collapsed():
    out = render({"messages": [{"role": "user", "content": "one\ntwo\nthree"}]})
    assert "elided" not in out
    assert "  two" in out


def test_tool_calls_render_as_calls():
    out = render({"messages": [
        {"role": "assistant", "content": None, "tool_calls": [
            {"type": "function", "function": {
                "name": "delete_file", "arguments": '{"path": "b.txt"}'}}]}]})
    assert '→ delete_file({"path": "b.txt"})' in out


def test_ascii_glyphs_are_used_when_the_terminal_cannot_do_better():
    assembled = context.from_event(llm_event(
        {"messages": [{"role": "user", "content": LISTING}]}))
    out = context.render(assembled, glyphs=Glyphs(False))
    assert "─" not in out and "⋯" not in out
    assert "..." in out and "---" in out


def test_glyph_detection_follows_the_streams_encoding():
    class Stream:
        encoding = "utf-8"

    class Ancient:
        encoding = "cp1252"

    assert Glyphs.detect(Stream()).rule == "─"
    assert Glyphs.detect(Ancient()).rule == "-"


# -- diffing -------------------------------------------------------------


def contexts(before_messages, after_messages, model="gpt-4o-mini"):
    return (
        context.from_event(llm_event({"model": model, "messages": before_messages}, index=0)),
        context.from_event(llm_event({"model": model, "messages": after_messages}, index=1)),
    )


def test_an_injected_message_is_reported_as_injected():
    before, after = contexts(
        [{"role": "user", "content": "hi"}],
        [{"role": "user", "content": "hi"},
         {"role": "assistant", "content": "hello"}],
    )
    changes = context.diff(before, after)
    assert [c.kind for c in changes] == ["same", "added"]

    out = context.render_diff(before, after, glyphs=Glyphs(True))
    assert "INJECTED" in out
    assert "+1 message" in out
    assert "1 injected" in out


def test_a_dropped_message_is_reported_as_dropped():
    before, after = contexts(
        [{"role": "system", "content": "Be concise."},
         {"role": "user", "content": "hi"}],
        [{"role": "user", "content": "hi"}],
    )
    out = context.render_diff(before, after, glyphs=Glyphs(True))
    assert "DROPPED" in out
    assert "Be concise." in out          # you see what was lost
    assert "1 dropped" in out


def test_a_truncated_message_is_called_out_as_truncated():
    before, after = contexts(
        [{"role": "user", "content": LISTING}],
        [{"role": "user", "content": LISTING[:80]}],
    )
    change = context.diff(before, after)[0]
    assert change.kind == "changed"
    assert change.truncated
    assert change.kept_prefix                 # the start survived, the rest was cut

    out = context.render_diff(before, after, glyphs=Glyphs(True))
    assert "TRUNCATED (kept the first 80 chars)" in out


def test_a_small_edit_is_changed_but_not_truncated():
    before, after = contexts(
        [{"role": "system", "content": "You are a file assistant."}],
        [{"role": "system", "content": "You are a file assistant. Ask first."}],
    )
    change = context.diff(before, after)[0]
    assert change.kind == "changed" and not change.truncated

    out = context.render_diff(before, after, glyphs=Glyphs(True))
    assert "CHANGED" in out
    assert "+You are a file assistant. Ask first." in out


def test_an_injection_at_the_front_does_not_mark_everything_changed():
    # Index-wise comparison would report all three as changed; alignment must
    # see one insertion.
    history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
               {"role": "user", "content": "c"}]
    before, after = contexts(
        history, [{"role": "system", "content": "injected!"}] + history)
    kinds = [c.kind for c in context.diff(before, after)]
    assert kinds.count("added") == 1
    assert kinds.count("same") == 3
    assert "changed" not in kinds


def test_identical_contexts_report_no_changes():
    messages = [{"role": "user", "content": "hi"}]
    before, after = contexts(messages, messages)
    out = context.render_diff(before, after, glyphs=Glyphs(True))
    assert "no changes" in out
    assert "1 unchanged" in out


def test_the_summary_counts_unchanged_messages():
    before, after = contexts(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "a"}],
        [{"role": "system", "content": "s"}, {"role": "user", "content": "b"}],
    )
    assert "1 changed · 1 unchanged" in context.render_diff(
        before, after, glyphs=Glyphs(True))


def test_a_long_diff_shows_both_ends_of_the_cut():
    # Where a truncation starts and where it ends are both load-bearing.
    before, after = contexts(
        [{"role": "user", "content": LISTING}],
        [{"role": "user", "content": "line 000\nline 001"}],
    )
    out = context.render_diff(before, after, glyphs=Glyphs(True))
    assert "line 002" in out                # the first casualty
    assert "line 039" in out                # and the last
    assert "more diff lines" in out


def test_the_diff_header_summarises_the_size_change():
    before, after = contexts(
        [{"role": "user", "content": LISTING}],
        [{"role": "user", "content": "hi"}],
    )
    out = context.render_diff(before, after, glyphs=Glyphs(True))
    header = out.splitlines()[1]
    assert "1 messages" in header
    assert "-" in header and "chars" in header
