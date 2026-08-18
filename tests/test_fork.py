"""Forking: replay a prefix, then run live.

``--at N`` replays events 0..N-1 and makes event N the first live one. That
boundary is asserted from both ends here, because an off-by-one would be
invisible until someone forked a run that mattered.
"""

import json

import httpx
import pytest

import reeltime as tape
from reeltime.core.patch import parse_all
from reeltime.errors import TapeConfigError

CHAT = {
    "object": "chat.completion",
    "model": "gpt-4o-mini",
    "choices": [{"message": {"content": "live answer"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
}


@pytest.fixture
def url(server):
    return server.route("/v1/chat/completions", json=CHAT)


def record(tape_dir, fn, run_id="01PARENT"):
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id=run_id):
        return fn()


def fork(tape_dir, fn, at, patches=(), parent="01PARENT", run_id="01FORK", **kwargs):
    run = tape.install("fork", tape_dir=tape_dir, collect_git=False, replay=parent,
                       fork_at=at, run_id=run_id, patches=parse_all(list(patches)),
                       **kwargs)
    try:
        result = fn()
    finally:
        tape.uninstall()
    return result, run


def events(tape_dir, run_id):
    return tape.read_trace(tape_dir / "runs" / "{}.jsonl".format(run_id)).events


# -- the boundary --------------------------------------------------------


@pytest.fixture
def three_tools(tape_dir):
    calls = []

    @tape.tool
    def step(n):
        calls.append(n)
        return "did {}".format(n)

    def agent():
        return [step(0), step(1), step(2)]

    record(tape_dir, agent)
    calls.clear()
    return agent, calls


def test_at_n_replays_the_first_n_events_and_runs_the_rest(three_tools, tape_dir):
    agent, calls = three_tools
    result, run = fork(tape_dir, agent, at=1)

    assert result == ["did 0", "did 1", "did 2"]
    # Event 0 came off the tape; 1 and 2 ran for real.
    assert calls == [1, 2]
    assert run.engine.replayed == 1


def test_fork_at_zero_runs_everything_live(three_tools, tape_dir):
    agent, calls = three_tools
    _, run = fork(tape_dir, agent, at=0)
    assert calls == [0, 1, 2]
    assert run.engine.replayed == 0


def test_fork_at_the_end_replays_everything(three_tools, tape_dir):
    agent, calls = three_tools
    result, run = fork(tape_dir, agent, at=3)
    assert result == ["did 0", "did 1", "did 2"]
    assert calls == []                      # nothing executed
    assert run.engine.replayed == 3


def test_the_fork_records_both_halves(three_tools, tape_dir):
    agent, _ = three_tools
    fork(tape_dir, agent, at=1)

    forked = events(tape_dir, "01FORK")
    assert len(forked) == 3
    assert [e.i for e in forked] == [0, 1, 2]
    assert forked[0].meta.get("replayed_from") == "01PARENT"
    assert "replayed_from" not in forked[1].meta


def test_a_destructive_tool_in_the_prefix_does_not_run(tape_dir):
    deleted = []

    @tape.tool
    def delete_file(path):
        deleted.append(path)
        return "deleted " + path

    @tape.tool
    def report():
        return "done"

    def agent():
        return [delete_file("b.txt"), report()]

    record(tape_dir, agent)
    assert deleted == ["b.txt"]

    result, _ = fork(tape_dir, agent, at=1)
    assert result == ["deleted b.txt", "done"]
    assert deleted == ["b.txt"]             # not deleted a second time


# -- the parent is never touched -----------------------------------------


def test_the_parent_trace_is_byte_identical_after_a_fork(three_tools, tape_dir):
    agent, _ = three_tools
    path = tape_dir / "runs" / "01PARENT.jsonl"
    before = path.read_bytes()

    fork(tape_dir, agent, at=1)
    fork(tape_dir, agent, at=2, run_id="01FORK2")

    assert path.read_bytes() == before


def test_forking_does_not_disturb_the_parents_blobs(tape_dir, url):
    def agent():
        return httpx.post(url, json={"model": "gpt-4o-mini", "padding": "x" * 20_000,
                                     "messages": []}).json()

    record(tape_dir, agent)
    blobs = sorted(p.name for p in (tape_dir / "blobs").iterdir())
    fork(tape_dir, agent, at=1)
    assert sorted(p.name for p in (tape_dir / "blobs").iterdir()) == blobs


# -- patches -------------------------------------------------------------


def sent_bodies(server):
    return [json.loads(r["body"]) for r in server.received if r["body"]]


def test_replacing_the_model_on_the_live_call(tape_dir, server, url):
    def agent():
        return httpx.post(url, json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}]}).json()

    record(tape_dir, agent)
    server.received.clear()
    fork(tape_dir, agent, at=0, patches=["llm.model=claude-sonnet-4-5"])

    assert sent_bodies(server)[0]["model"] == "claude-sonnet-4-5"


def test_appending_to_the_system_prompt_on_the_live_call(tape_dir, server, url):
    def agent():
        return httpx.post(url, json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "system", "content": "Be terse."},
                         {"role": "user", "content": "hi"}]}).json()

    record(tape_dir, agent)
    server.received.clear()
    fork(tape_dir, agent, at=0,
         patches=['llm.system+="Ask before destructive actions."'])

    messages = sent_bodies(server)[0]["messages"]
    assert messages[0]["content"] == "Be terse. Ask before destructive actions."
    assert messages[1]["content"] == "hi"        # nothing else moved


def test_regex_substituting_the_system_prompt(tape_dir, server, url):
    def agent():
        return httpx.post(url, json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "system", "content": "Delete freely."}]}).json()

    record(tape_dir, agent)
    server.received.clear()
    fork(tape_dir, agent, at=0, patches=["llm.system~=/freely/carefully/"])

    assert sent_bodies(server)[0]["messages"][0]["content"] == "Delete carefully."


def test_setting_a_numeric_parameter(tape_dir, server, url):
    def agent():
        return httpx.post(url, json={"model": "gpt-4o-mini", "temperature": 0.9,
                                     "messages": []}).json()

    record(tape_dir, agent)
    server.received.clear()
    fork(tape_dir, agent, at=0, patches=["llm.temperature=0.0"])
    assert sent_bodies(server)[0]["temperature"] == 0.0


def test_substituting_a_tool_result_skips_the_body(tape_dir):
    ran = []

    @tape.tool
    def read_file(path):
        ran.append(path)
        return "the real contents"

    def agent():
        return read_file("notes.md")

    record(tape_dir, agent)
    ran.clear()

    result, _ = fork(tape_dir, agent, at=0,
                     patches=['tool.read_file.result="<empty file>"'])
    assert result == "<empty file>"
    assert ran == []                    # the point: the file was never read


def test_a_substituted_tool_result_is_recorded_as_an_event(tape_dir):
    @tape.tool
    def read_file(path):
        return "the real contents"

    record(tape_dir, lambda: read_file("notes.md"))
    fork(tape_dir, lambda: read_file("notes.md"), at=0,
         patches=['tool.read_file.result="<empty file>"'])

    event = events(tape_dir, "01FORK")[0]
    assert event.res == {"value": "<empty file>"}
    assert event.meta["patched"] is True


def test_appending_to_a_tool_result_builds_on_the_recorded_one(tape_dir):
    @tape.tool
    def read_file(path):
        return "line one"

    def agent():
        return read_file("notes.md")

    record(tape_dir, agent)
    result, _ = fork(tape_dir, agent, at=0,
                     patches=['tool.read_file.result+="line two"'])
    assert result == "line one line two"


def test_regex_substituting_a_tool_result(tape_dir):
    @tape.tool
    def read_file(path):
        return "status: ok"

    def agent():
        return read_file("notes.md")

    record(tape_dir, agent)
    result, _ = fork(tape_dir, agent, at=0, patches=["tool.read_file.result~=/ok/FAILED/"])
    assert result == "status: FAILED"


def test_a_patch_applies_once_not_to_every_later_call(tape_dir):
    @tape.tool
    def read_file(path):
        return "real:" + path

    def agent():
        return [read_file("a.md"), read_file("b.md")]

    record(tape_dir, agent)
    result, _ = fork(tape_dir, agent, at=0, patches=['tool.read_file.result="patched"'])
    # Only event 0 is patched; the second call runs for real.
    assert result == ["patched", "real:b.md"]


# -- validation, before anything is replayed -----------------------------


def test_a_patch_that_cannot_apply_is_refused_up_front(three_tools, tape_dir):
    agent, calls = three_tools
    with pytest.raises(TapeConfigError) as caught:
        fork(tape_dir, agent, at=1, patches=["llm.model=gpt-4o"])

    message = str(caught.value)
    assert "does not apply to event 1" in message
    assert "tool event" in message
    assert "tape show" in message
    assert calls == []                  # nothing ran, nothing was replayed


def test_a_malformed_patch_is_refused_before_forking(three_tools, tape_dir):
    agent, calls = three_tools
    with pytest.raises(TapeConfigError, match="Expected <kind>"):
        fork(tape_dir, agent, at=1, patches=["not a patch"])
    assert calls == []


def test_forking_past_the_end_is_refused(three_tools, tape_dir):
    agent, _ = three_tools
    with pytest.raises(TapeConfigError, match="past the end"):
        fork(tape_dir, agent, at=99)


def test_a_negative_fork_point_is_refused(three_tools, tape_dir):
    agent, _ = three_tools
    with pytest.raises(TapeConfigError, match="zero or more"):
        fork(tape_dir, agent, at=-1)


def test_fork_needs_a_point(three_tools, tape_dir):
    with pytest.raises(TapeConfigError, match="needs --at"):
        tape.install("fork", tape_dir=tape_dir, collect_git=False, replay="01PARENT")


def test_missing_credentials_are_named_before_replaying(tape_dir, monkeypatch):
    """A fork that will die at event N for want of a key says so at event 0."""
    from reeltime.core.fork import missing_credentials
    from reeltime.core.trace import Event, Header, Trace

    trace = Trace(
        header=Header(run_id="01X", started="", argv=[], cwd="", python=""),
        events=[
            Event(i=0, kind="llm", site="a.py:1",
                  req={"url": "https://api.openai.com/v1/chat/completions"}),
            Event(i=1, kind="llm", site="a.py:2",
                  req={"url": "https://api.anthropic.com/v1/messages"}),
        ],
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Forking at 1 only runs the Anthropic call live, so only that key matters.
    assert missing_credentials(trace, 1) == [("api.anthropic.com", "ANTHROPIC_API_KEY")]
    assert len(missing_credentials(trace, 0)) == 2

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert missing_credentials(trace, 1) == []


def test_a_localhost_fork_needs_no_credentials(tape_dir, url):
    from reeltime.core.fork import missing_credentials

    def agent():
        return httpx.post(url, json={"model": "gpt-4o-mini", "messages": []}).json()

    record(tape_dir, agent)
    trace = tape.read_trace(tape_dir / "runs" / "01PARENT.jsonl")
    assert missing_credentials(trace, 0) == []


# -- lineage -------------------------------------------------------------


def test_the_header_records_its_parent(three_tools, tape_dir):
    agent, _ = three_tools
    fork(tape_dir, agent, at=1)

    header = tape.read_trace(tape_dir / "runs" / "01FORK.jsonl").header
    assert header.forked_from == "01PARENT"
    assert header.fork_at == 1
    assert header.mode == "fork"


def test_a_fork_of_a_fork_keeps_the_chain(three_tools, tape_dir):
    agent, calls = three_tools

    fork(tape_dir, agent, at=1)
    calls.clear()
    # The fork is a complete run, so it can be forked in turn.
    fork(tape_dir, agent, at=2, parent="01FORK", run_id="01GRANDCHILD")

    child = tape.read_trace(tape_dir / "runs" / "01FORK.jsonl").header
    grandchild = tape.read_trace(tape_dir / "runs" / "01GRANDCHILD.jsonl").header
    assert child.forked_from == "01PARENT" and child.fork_at == 1
    assert grandchild.forked_from == "01FORK" and grandchild.fork_at == 2
    assert calls == [2]                 # only the tail ran live

    # And the chain is walkable back to the root.
    chain, node = [], grandchild
    while node.forked_from:
        chain.append(node.forked_from)
        node = tape.read_trace(tape_dir / "runs" / "{}.jsonl".format(node.forked_from)).header
    assert chain == ["01FORK", "01PARENT"]


def test_a_fork_can_be_replayed_like_any_other_run(three_tools, tape_dir):
    agent, calls = three_tools
    fork(tape_dir, agent, at=1)
    calls.clear()

    with tape.session("replay", tape_dir=tape_dir, replay="01FORK"):
        result = agent()
    assert result == ["did 0", "did 1", "did 2"]
    assert calls == []                  # a replay of a fork runs nothing


def test_the_summary_counts_both_halves(three_tools, tape_dir):
    agent, _ = three_tools
    _, run = fork(tape_dir, agent, at=1)
    summary = run.engine.summary
    assert summary.parent == "01PARENT"
    assert summary.fork_at == 1
    assert (summary.replayed, summary.live) == (1, 2)
    assert "1 replayed, 2 live" in summary.line()


def test_fork_patches_do_not_collide_with_the_ambient_patch_setting(monkeypatch):
    """REELTIME_PATCH configures ambient groups; fork expressions use their own.

    Reusing the name once made every forked child record nothing at all: the
    JSON list of expressions parsed as a list of ambient group names, none of
    which existed, so nothing was patched and nothing was seen.
    """
    from reeltime.core.config import Config

    monkeypatch.setenv("REELTIME_FORK_PATCH", '["llm.model=gpt-4o"]')
    monkeypatch.delenv("REELTIME_PATCH", raising=False)
    assert "random" in Config.resolve(tape_dir="/tmp/x").patch

    monkeypatch.setenv("REELTIME_PATCH", "random,uuid")
    assert Config.resolve(tape_dir="/tmp/x").patch == ("random", "uuid")
