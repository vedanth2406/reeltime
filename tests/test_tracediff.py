"""Comparing two runs.

The divergence line is the point of the whole command, so it is asserted from
several directions: where it lands, what it counts, and that it stays absent
when the two runs really do line up end to end.
"""

import json

import pytest

from reeltime.core import tracediff
from reeltime.core.context import Glyphs
from reeltime.core.tracediff import ADDED, CHANGED, REMOVED, SAME
from reeltime.core.trace import Event, Header, Trace


def tool(index, name, site=None, value="ok", **args):
    return Event(
        i=index, kind="tool", site=site or "agent.py:{}".format(10 + index),
        qual="agent.py::main", req={"name": name, "args": args or {"n": index}},
        res={"value": value},
    )


def llm(index, messages, model="gpt-4o-mini", site="agent.py:88", tokens_in=100):
    return Event(
        i=index, kind="llm", site=site, qual="agent.py::ask",
        req={"method": "POST", "url": "https://api.openai.com/v1/chat/completions",
             "headers": [], "model": model,
             "body": {"json": {"model": model, "messages": messages}}},
        res={"status": 200, "headers": [], "tokens": {"in": tokens_in, "out": 5},
             "preview": "ok",
             "body": {"json": {"choices": [{"message": {"content": "ok"}}],
                               "usage": {"prompt_tokens": tokens_in,
                                         "completion_tokens": 5}}}},
        meta={"cost_usd": 0.001},
    )


def trace(events, run_id="01A", cost=0.01, tokens=(100, 20)):
    return Trace(
        header=Header(run_id=run_id, started="", argv=["agent.py"], cwd="", python=""),
        events=list(events),
        footer={"events": len(events), "cost_usd": cost,
                "tokens": {"in": tokens[0], "out": tokens[1]}},
    )


# -- the divergence point ------------------------------------------------


def test_two_runs_that_walk_off_in_different_directions():
    """Paired differences are shown; the divergence line covers the remainder.

    Alignment pairs what it can positionally, so a run of mismatched steps is
    reported step by step -- that is where `delete → ask` is visible -- and the
    divergence line takes over once one run has events the other cannot be
    matched against at all. This is the shape the spec's example has.
    """
    a = trace([tool(0, "read"), tool(1, "plan"), tool(2, "delete"), tool(3, "report")])
    b = trace([tool(0, "read"), tool(1, "plan"), tool(2, "ask"),
               tool(3, "wait"), tool(4, "retry")], run_id="01B")

    result = tracediff.diff(a, b)
    assert [s.kind for s in result.steps] == [SAME, SAME, CHANGED, CHANGED, ADDED]
    assert result.divergence == 4
    assert result.tail == (0, 1)

    out = tracediff.render(result, glyphs=Glyphs(True))
    assert "delete" in out and "ask" in out          # the swap is still visible
    assert "divergent from here (A ended; B: 1 more event)" in out


def test_the_divergence_counts_a_long_unmatched_tail():
    common = [tool(i, "step", site="agent.py:{}".format(i)) for i in range(3)]
    a = trace(common + [tool(10 + i, "a_only", site="a.py:{}".format(i))
                        for i in range(6)])
    b = trace(common + [tool(20 + i, "b_only", site="b.py:{}".format(i))
                        for i in range(9)], run_id="01B")

    result = tracediff.diff(a, b)
    assert result.divergence == 3 + 6      # six paired replacements, then B's tail
    assert result.tail == (0, 3)
    assert "B: 3 more events" in tracediff.render(result, glyphs=Glyphs(True))


def test_identical_runs_have_no_divergence():
    events = [tool(0, "read"), tool(1, "plan")]
    result = tracediff.diff(trace(events), trace(events, run_id="01B"))

    assert result.divergence is None
    assert result.identical
    assert "the two runs are identical" in tracediff.render(result, glyphs=Glyphs(True))


def test_a_run_that_simply_stops_early_diverges_where_it_stopped():
    a = trace([tool(0, "read"), tool(1, "plan"), tool(2, "report")])
    b = trace([tool(0, "read"), tool(1, "plan")], run_id="01B")

    result = tracediff.diff(a, b)
    assert result.divergence == 2
    assert result.tail == (1, 0)
    assert "A: 1 more event; B ended" in tracediff.render(result, glyphs=Glyphs(True))


def test_a_difference_in_the_middle_is_not_a_divergence():
    # The runs disagree at step 1 and then get back in step. That is a changed
    # step, not two trajectories parting company.
    a = trace([tool(0, "read"), tool(1, "plan"), tool(2, "report")])
    b = trace([tool(0, "read"), tool(1, "plan", value="different"), tool(2, "report")],
              run_id="01B")

    result = tracediff.diff(a, b)
    assert result.divergence is None
    assert [s.kind for s in result.steps] == [SAME, CHANGED, SAME]


def test_an_empty_pair_of_runs_says_so():
    result = tracediff.diff(trace([]), trace([], run_id="01B"))
    assert "both runs are empty" in tracediff.render(result, glyphs=Glyphs(True))


# -- alignment -----------------------------------------------------------


def test_an_event_inserted_at_the_front_shifts_nothing_else():
    common = [tool(1, "read"), tool(2, "plan"), tool(3, "report")]
    a = trace(common)
    b = trace([tool(0, "setup", site="agent.py:5")] + common, run_id="01B")

    kinds = [s.kind for s in tracediff.diff(a, b).steps]
    assert kinds.count(ADDED) == 1
    assert kinds.count(SAME) == 3
    assert CHANGED not in kinds


def test_an_event_only_in_a_is_reported_as_removed():
    a = trace([tool(0, "read"), tool(1, "audit", site="agent.py:20"), tool(2, "report")])
    b = trace([tool(0, "read"), tool(2, "report")], run_id="01B")

    result = tracediff.diff(a, b)
    removed = [s for s in result.steps if s.kind == REMOVED]
    assert len(removed) == 1 and removed[0].a.name == "audit"
    assert "only in A: audit" in tracediff.render(result, glyphs=Glyphs(True))


def test_a_replaced_call_is_paired_so_the_swap_is_visible():
    a = trace([tool(0, "read"), tool(1, "delete_file", path="b.txt")])
    b = trace([tool(0, "read"), tool(1, "ask_user", prompt="confirm delete b.txt?")],
              run_id="01B")

    out = tracediff.render(tracediff.diff(a, b), glyphs=Glyphs(True))
    assert 'delete_file(path=b.txt)' in out
    assert 'ask_user(prompt=confirm delete b.txt?)' in out
    assert "→" in out


def test_changed_arguments_to_the_same_tool():
    a = trace([tool(0, "read_file", path="a.md")])
    b = trace([tool(0, "read_file", path="b.md")], run_id="01B")

    out = tracediff.render(tracediff.diff(a, b), glyphs=Glyphs(True))
    assert "read_file(path=a.md)" in out and "read_file(path=b.md)" in out


def test_llm_and_http_align_with_each_other():
    # `llm` is a label a decoder applies to an http event; a run recorded
    # before that decoder existed must still line up against one recorded after.
    a_event = llm(0, [{"role": "user", "content": "hi"}])
    b_event = llm(0, [{"role": "user", "content": "hi"}])
    b_event.kind = "http"
    assert tracediff.signature(a_event) == tracediff.signature(b_event)


# -- what changed --------------------------------------------------------


def test_a_changed_system_prompt_is_shown_as_two_lines():
    a = trace([llm(0, [{"role": "system", "content": "You are a file assistant."},
                       {"role": "user", "content": "clean up"}])])
    b = trace([llm(0, [{"role": "system",
                        "content": "You are a file assistant. Ask before destructive actions."},
                       {"role": "user", "content": "clean up"}])], run_id="01B")

    out = tracediff.render(tracediff.diff(a, b), glyphs=Glyphs(True))
    assert "system prompt changed" in out
    assert "- You are a file assistant." in out
    assert "+ You are a file assistant. Ask before destructive actions." in out


def test_a_changed_model_is_reported():
    a = trace([llm(0, [{"role": "user", "content": "hi"}], model="gpt-4o-mini")])
    b = trace([llm(0, [{"role": "user", "content": "hi"}], model="claude-sonnet-4-5")],
              run_id="01B")

    changes = tracediff.diff(a, b).steps[0].changes
    assert any(c.label == "model" and c.after == "claude-sonnet-4-5" for c in changes)


def test_an_injected_message_is_reported():
    a = trace([llm(0, [{"role": "user", "content": "hi"}])])
    b = trace([llm(0, [{"role": "system", "content": "Be terse."},
                       {"role": "user", "content": "hi"}])], run_id="01B")

    out = tracediff.render(tracediff.diff(a, b), glyphs=Glyphs(True))
    assert "injected" in out and "Be terse." in out


def test_a_truncated_message_is_flagged_as_truncated():
    long_text = "\n".join("line {}".format(i) for i in range(40))
    a = trace([llm(0, [{"role": "user", "content": long_text}])])
    b = trace([llm(0, [{"role": "user", "content": long_text[:40]}])], run_id="01B")

    out = tracediff.render(tracediff.diff(a, b), glyphs=Glyphs(True))
    assert "truncated" in out


def test_a_changed_result_is_reported():
    a = trace([tool(0, "read_file", value="contents")])
    b = trace([tool(0, "read_file", value="<empty file>")], run_id="01B")

    out = tracediff.render(tracediff.diff(a, b), glyphs=Glyphs(True))
    assert "result" in out and "<empty file>" in out


def test_an_error_appearing_in_one_run_is_reported():
    a = trace([tool(0, "read_file")])
    failed = tool(0, "read_file")
    failed.meta = {"error": {"type": "FileNotFoundError", "message": "gone"}}
    b = trace([failed], run_id="01B")

    out = tracediff.render(tracediff.diff(a, b), glyphs=Glyphs(True))
    assert "error" in out and "FileNotFoundError" in out


# -- filtering and totals ------------------------------------------------


def test_only_narrows_the_comparison_to_one_kind():
    a = trace([tool(0, "read"), llm(1, [{"role": "user", "content": "hi"}]),
               tool(2, "report")])
    b = trace([tool(0, "read"), llm(1, [{"role": "user", "content": "bye"}]),
               tool(2, "report")], run_id="01B")

    everything = tracediff.diff(a, b)
    assert len(everything.steps) == 3

    llm_only = tracediff.diff(a, b, only=["llm"])
    assert len(llm_only.steps) == 1
    assert llm_only.steps[0].event.kind == "llm"


def http(index, site="agent.py:5", url="https://example.com/health"):
    return Event(
        i=index, kind="http", site=site, qual="agent.py::check",
        req={"method": "GET", "url": url, "headers": []},
        res={"status": 200, "headers": []},
    )


def test_only_llm_does_not_hand_back_every_plain_http_call():
    """`--only` narrows. It used to fold llm to http and widen instead."""
    events = [http(0), llm(1, [{"role": "user", "content": "hi"}]), http(2, site="a.py:9")]
    a = trace(events)
    b = trace(events, run_id="01B")

    result = tracediff.diff(a, b, only=["llm"])
    assert [step.event.kind for step in result.steps] == ["llm"]


def test_only_http_still_includes_llm_events():
    """An llm event *is* an http event a decoder put a label on."""
    events = [http(0), llm(1, [{"role": "user", "content": "hi"}])]
    result = tracediff.diff(trace(events), trace(events, run_id="01B"), only=["http"])
    assert [step.event.kind for step in result.steps] == ["http", "llm"]


def test_only_is_still_repeatable():
    events = [http(0), llm(1, [{"role": "user", "content": "hi"}]), tool(2, "read")]
    result = tracediff.diff(trace(events), trace(events, run_id="01B"),
                            only=["llm", "tool"])
    assert [step.event.kind for step in result.steps] == ["llm", "tool"]


def test_totals_come_from_the_footers():
    a = trace([tool(0, "read")], cost=0.31, tokens=(14203, 0))
    b = trace([tool(0, "read")], run_id="01B", cost=0.44, tokens=(19881, 0))

    out = tracediff.render(tracediff.diff(a, b), glyphs=Glyphs(True))
    assert "cost   A $0.31" in out and "B $0.44" in out
    assert "14,203" in out and "19,881" in out


def test_a_long_identical_run_is_collapsed():
    events = [tool(i, "step") for i in range(13)]
    out = tracediff.render(
        tracediff.diff(trace(events), trace(events, run_id="01B")), glyphs=Glyphs(True))
    assert "step 0–12  identical (13 events)" in out


# -- machine output ------------------------------------------------------


def test_json_output_carries_the_divergence_and_the_steps():
    a = trace([tool(0, "read"), tool(1, "delete")])
    b = trace([tool(0, "read"), tool(1, "ask"), tool(2, "wait")], run_id="01B")

    payload = json.loads(json.dumps(tracediff.diff(a, b).to_dict()))
    assert payload["a"] == "01A" and payload["b"] == "01B"
    assert payload["divergence"] is not None
    assert payload["tail"] == {"a": 0, "b": 1}
    assert payload["counts"]["same"] == 1
    assert payload["steps"][0]["kind"] == "same"
    assert payload["totals"]["a"]["events"] == 2
