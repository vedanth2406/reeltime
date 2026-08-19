"""The LangChain adapter (M9).

Three layers, mirroring ``tests/test_mcp.py``:

* the payload, naming and version logic, which needs no framework at all and is
  where the rules in the module docstring are cheapest to pin down;
* record and replay through ``langchain-core`` on its own -- LCEL chains,
  tools, parallel branches, both the sync and the async dispatch paths;
* a real agent with real tools making real HTTP calls, driven end to end
  through the CLI. That last layer is the only place the central claim can be
  tested at all: that a model node produces **one** event and not two.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import reeltime as tape
from reeltime.core import doctor
from reeltime.core import langchain as lc
from reeltime.core import paths, tracediff
from reeltime.core.matching import align_key, content_key, filter_kinds, kind_key
from reeltime.core.trace import Event
from reeltime.errors import TapeConfigError, TapeError

try:
    import langchain_core
except ImportError:  # langchain-core requires Python 3.10; reeltime supports 3.9
    langchain_core = None

try:
    import langchain_openai  # noqa: F401
    from langchain.agents import create_agent  # noqa: F401

    HAS_AGENT = True
except ImportError:
    HAS_AGENT = False

needs_core = pytest.mark.skipif(
    langchain_core is None, reason="langchain-core needs Python 3.10+")
needs_agent = pytest.mark.skipif(
    not HAS_AGENT, reason="needs langchain and langchain-openai")

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


#: Set by the CI job that pins langchain-core to the *bottom* of the supported
#: range. That job deliberately uninstalls `langchain` and `langchain-openai`,
#: which pin a newer core, so the agent-level tests are expected to be missing
#: there and only there.
FLOOR_RUN = bool(os.environ.get("REELTIME_LANGCHAIN_FLOOR"))


def test_the_framework_is_installed_wherever_it_can_be():
    """A skipped section has to be a decision, not an accident.

    The same guard `tests/test_mcp.py` carries, for the same reason: a
    module-level importorskip once turned a missing dev dependency into 25
    silently skipped tests instead of a failure anybody noticed.
    """
    if sys.version_info < (3, 10):
        return
    assert langchain_core is not None, (
        "langchain-core is a dev dependency on 3.10+; without it every "
        "record/replay test in this file skips without anyone noticing")
    assert HAS_AGENT or FLOOR_RUN, (
        "langchain and langchain-openai are dev dependencies on 3.10+; "
        "without them the one-event-not-two claim is never tested")


def test_the_floor_run_is_actually_running_against_the_floor():
    """And the job that sets that variable has to earn it.

    Otherwise `REELTIME_LANGCHAIN_FLOOR=1` in the wrong place would quietly
    excuse the assertion above on an ordinary run.
    """
    if not FLOOR_RUN:
        return
    assert lc.parse_version(lc._installed_version())[:2] == lc.MINIMUM, (
        "REELTIME_LANGCHAIN_FLOOR is set but langchain-core is not at the "
        "bottom of the declared range")


# -- the version gate ----------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("1.5.6", (1, 5, 6)),
    ("0.3.86", (0, 3, 86)),
    ("2.0.0rc1", (2, 0, 0)),
    ("1.5", (1, 5)),
    ("", None),
    (None, None),
    ("not-a-version", None),
])
def test_a_version_string_reduces_to_its_release_numbers(text, expected):
    assert lc.parse_version(text) == expected


@pytest.mark.parametrize("version,ok", [
    ((0, 3, 0), True),
    ((1, 5, 6), True),
    ((0, 2, 9), False),
    ((2, 0, 0), False),
    ((3, 1, 0), False),
    (None, False),
])
def test_the_supported_range_is_the_range_that_was_tested(version, ok):
    assert lc.supported(version) is ok


def test_an_untested_version_is_refused_with_the_range_it_needs(monkeypatch):
    """Recording something subtly wrong is worse than recording nothing."""
    monkeypatch.setattr(lc, "_installed_version", lambda: "9.9.9")
    with pytest.raises(TapeError) as caught:
        lc.check_version()
    message = str(caught.value)
    assert "9.9.9" in message
    assert "0.3" in message and "2.0" in message
    assert "allow_unsupported" in message


def test_an_untested_version_can_be_overridden_deliberately(monkeypatch):
    monkeypatch.setattr(lc, "_installed_version", lambda: "9.9.9")
    assert lc.check_version(allow_unsupported=True) == (9, 9, 9)


def test_a_missing_framework_says_what_to_install(monkeypatch):
    monkeypatch.setattr(lc, "_installed_version", lambda: None)
    with pytest.raises(TapeError, match="pip install"):
        lc.check_version()


def test_the_declared_range_matches_what_is_installed_here():
    """The dev pin and the declared range must not drift apart.

    `pyproject.toml` pins the framework the suite runs against; this file's
    claims are only as good as that pin agreeing with the module's own range.
    """
    if langchain_core is None:
        pytest.skip("langchain-core is not installed")
    assert lc.supported(lc.parse_version(lc._installed_version()))
    assert lc.install() is None, "the installed version must not need forcing"
    lc.uninstall()


# -- naming a node -------------------------------------------------------


def test_a_node_name_is_found_in_whichever_place_this_version_keeps_it():
    # langchain-core 1.x: serialized is None and the name arrives as a keyword.
    assert lc.node_name(None, {"name": "AgentExecutor"}) == "AgentExecutor"
    # 0.x: a serialized dict with a dotted id path.
    assert lc.node_name({"id": ["langchain", "chains", "LLMChain"]}, {}) == "LLMChain"
    # tools serialize with a plain name.
    assert lc.node_name({"name": "shout"}, None) == "shout"


def test_an_unnameable_node_is_still_recordable():
    assert lc.node_name(None, None) == "chain"
    assert lc.node_name({}, {"name": ""}) == "chain"


def test_only_structural_tags_reach_the_match_key():
    """`seq:step:1` says where the node is; `production` says nothing."""
    assert lc.step_tag(["seq:step:2"]) == "seq:step:2"
    assert lc.step_tag(["map:key:answer"]) == "map:key:answer"
    assert lc.step_tag(["production", "v2"]) is None
    assert lc.step_tag(None) is None


def test_a_runaway_path_is_capped_rather_than_recorded_in_full():
    deep = ["node{}".format(i) for i in range(40)]
    path = lc.join(deep)
    assert path.startswith("…/")
    assert path.endswith("node39")
    assert len(path.split("/")) == lc.MAX_PATH + 1


def test_a_short_path_is_left_alone():
    assert lc.join(["a", "b"]) == "a/b"


# -- what does and does not become an event ------------------------------


def test_every_node_but_the_model_node_becomes_an_event():
    """One rule, because a rule with exceptions is one people get wrong."""
    for node_type in (lc.TYPE_CHAIN, lc.TYPE_TOOL, lc.TYPE_RETRIEVER,
                      lc.TYPE_PROMPT, lc.TYPE_PARSER,
                      lc.TYPE_AGENT_ACTION, lc.TYPE_AGENT_FINISH):
        assert lc.recordable(node_type), node_type
    assert not lc.recordable(lc.TYPE_LLM)


# -- payloads ------------------------------------------------------------


def test_a_per_run_message_id_never_reaches_the_trace():
    """Measured, not assumed: these ids are the only part of a LangChain
    payload that differs between two identical runs."""
    before = {"messages": [
        # The two spellings LangChain actually produces: a bare uuid4 for a
        # message it minted, and a run-derived id with a generation index for
        # one that came back from a model.
        {"content": "hi", "id": "a71294c3-745a-447e-99b5-6f01848fa737"},
        {"content": "ho", "id": "lc_run--01a0189e-850c-7db0-830e-eb505d8203cd-0"},
        # The same thing, spelled the way langchain-core 0.3 spells it.
        {"content": "hm", "id": "run--01a018b1-bd8b-73e0-a01a-dffabaf583b9-0"},
    ]}
    after = lc.stable(before)
    assert after == {"messages": [{"content": "hi"}, {"content": "ho"},
                                  {"content": "hm"}]}


def test_an_id_that_means_something_is_kept():
    """A tool call id is part of the conversation, not framework bookkeeping."""
    kept = lc.stable({"tool_calls": [{"id": "call_1", "name": "shout"}],
                      "id": "msg_42"})
    assert kept == {"tool_calls": [{"id": "call_1", "name": "shout"}], "id": "msg_42"}


def test_stripping_ids_reaches_into_nested_lists():
    value = lc.stable([[{"id": "a71294c3-745a-447e-99b5-6f01848fa737", "k": 1}]])
    assert value == [[{"k": 1}]]


def test_an_unserialisable_input_is_still_recordable():
    class Odd:
        def __repr__(self):
            return "<odd>"

    json.dumps(lc.request("n", "chain", "n", 0, {"x": Odd()}))


def test_a_node_result_carries_its_outputs_its_render_and_its_fan_out():
    res = lc.result({"output": "done"}, children=3)
    assert res["outputs"] == {"output": "done"}
    assert res["value"] == "done"
    assert res["children"] == 3


@pytest.mark.parametrize("outputs,expected", [
    ({"output": "done"}, "done"),
    ({"messages": [{"content": "a"}, {"content": "b"}]}, "b"),
    ([{"update": {"messages": [{"content": "z"}]}}], "z"),
    ("plain", "plain"),
    (7, 7),
    (None, None),
])
def test_a_node_output_is_rendered_through_the_shapes_frameworks_use(outputs, expected):
    assert lc.render_value(outputs) == expected


def test_an_unrecognised_output_shape_degrades_to_json_rather_than_to_nothing():
    assert lc.render_value({"weird": [1, 2]}) == '{"weird": [1, 2]}'


def test_a_long_render_is_truncated():
    assert len(lc.render_value("x" * 5000)) == lc.VALUE_LIMIT


# -- identity: structure, not payload ------------------------------------


def _chain_req(**over):
    base = dict(name="model", node_type="chain", path="graph/model", depth=1,
                inputs={"messages": ["hi"]}, step=None)
    base.update(over)
    return lc.request(**base)


def test_a_node_is_identified_by_where_it_sits_not_by_what_flowed_through_it():
    """A node's inputs are a consequence of the model calls above it; hashing
    them would report drift on every node downstream of a prompt tweak."""
    one = content_key("chain", _chain_req(inputs={"messages": ["hi"]}))
    two = content_key("chain", _chain_req(inputs={"messages": ["something else"]}))
    assert one == two


@pytest.mark.parametrize("difference", [
    {"name": "tools"},
    {"path": "graph/other/model"},
    {"depth": 2},
    {"step": "seq:step:2"},
    {"node_type": "tool"},
])
def test_a_node_that_moved_is_a_different_node(difference):
    assert content_key("chain", _chain_req()) != content_key(
        "chain", _chain_req(**difference))


def test_two_identical_branches_of_a_map_stay_distinct():
    """Both are `RunnableLambda` at the same depth; only the tag separates them."""
    left = _chain_req(name="RunnableLambda", step="map:key:a")
    right = _chain_req(name="RunnableLambda", step="map:key:b")
    assert content_key("chain", left) != content_key("chain", right)


# -- folding: for alignment, deliberately not for matching ---------------


def test_chain_folds_into_http_for_alignment():
    assert align_key("chain") == align_key("llm") == align_key("mcp") == "http"


def test_chain_does_not_fold_for_matching():
    """A wrong pairing in a diff is a confusing line; a wrong bucket in the
    matcher serves an HTTP request a chain node's payload."""
    assert kind_key("chain") == "chain"
    assert kind_key("llm") == kind_key("mcp") == "http"


def test_only_chain_means_chain():
    assert filter_kinds(["chain"]) == {"chain"}
    assert filter_kinds(["http"]) == {"http", "llm"}


def test_an_older_recording_without_the_adapter_still_aligns():
    old = _trace([_http(0, site="agent.py:10")], run_id="01OLD")
    new = _trace([_http(0, site="agent.py:10"),
                  _chain(1, "model", site="agent.py:10")], run_id="01NEW")
    result = tracediff.diff(old, new)
    assert result.steps[0].paired, "the two runs would otherwise share nothing"
    assert result.steps[1].kind == tracediff.ADDED


# -- diff reports a changed shape as a changed shape ---------------------


def test_a_node_that_moved_in_the_graph_is_its_own_line():
    a = _trace([_chain(0, "model", path="graph/model", depth=1)], run_id="01A")
    b = _trace([_chain(0, "model", path="graph/agent/model", depth=2)], run_id="01B")
    step = tracediff.diff(a, b).steps[0]
    change = next(c for c in step.changes if c.label == "chain structure changed")
    assert change.lines == ["- graph/model", "+ graph/agent/model"]
    assert change.before == "depth 1" and change.after == "depth 2"


def test_a_changed_shape_is_rendered_not_buried():
    a = _trace([_chain(0, "model", path="graph/model", depth=1)], run_id="01A")
    b = _trace([_chain(0, "model", path="graph/agent/model", depth=2)], run_id="01B")
    rendered = tracediff.render(tracediff.diff(a, b))
    assert "chain structure changed" in rendered
    assert "+ graph/agent/model" in rendered


def test_a_node_that_fanned_out_differently_is_reported():
    a = _trace([_chain(0, "tools", children=1)], run_id="01A")
    b = _trace([_chain(0, "tools", children=3)], run_id="01B")
    labels = [c.label for c in tracediff.diff(a, b).steps[0].changes]
    assert "chain fan-out changed" in labels


def test_a_node_whose_role_changed_is_reported():
    a = _trace([_chain(0, "step", node_type="parser")], run_id="01A")
    b = _trace([_chain(0, "step", node_type="tool")], run_id="01B")
    labels = [c.label for c in tracediff.diff(0 * 0 or a, b).steps[0].changes]
    assert "node type" in labels


def test_a_node_that_produced_something_else_says_outputs_not_result():
    """A chain node did not return anything; it produced outputs."""
    a = _trace([_chain(0, "model", value="yes")], run_id="01A")
    b = _trace([_chain(0, "model", value="no")], run_id="01B")
    labels = [c.label for c in tracediff.diff(a, b).steps[0].changes]
    assert "outputs" in labels and "result" not in labels


def test_identical_structure_reports_nothing():
    a = _trace([_chain(0, "model")], run_id="01A")
    b = _trace([_chain(0, "model")], run_id="01B")
    assert tracediff.diff(a, b).identical


# -- doctor: a node is structure, never a source -------------------------


def test_a_chain_node_is_never_reported_as_a_nondeterminism_source():
    """Its outputs are what the boundaries underneath it produced. Blaming the
    node that carried the difference points at the wrong line -- the same
    mistake `doctor` already corrects for an unlike pairing."""
    a = _trace([_chain(0, "model", value="yes")], run_id="01A")
    b = _trace([_chain(0, "model", value="no")], run_id="01B")
    report = doctor.analyse([a, b])
    assert report.sources == []


def test_a_node_that_moved_still_shows_up_as_a_path_split():
    a = _trace([_chain(0, "model"), _chain(1, "tools")], run_id="01A")
    b = _trace([_chain(0, "model"), _chain(1, "retry")], run_id="01B")
    report = doctor.analyse([a, b])
    assert report.split is not None and report.split.step == 1


# -- the tracker, driven without a framework -----------------------------


def _drive(tracker, node_type, name, run_id, parent=None, inputs=None, tags=None):
    tracker.start(node_type, {"name": name}, inputs, run_id, parent, tags, None)


def test_the_tracker_records_a_tree_without_langchain_anywhere_near_it(tape_dir):
    tracker = lc.Tracker()
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01REC"):
        _drive(tracker, lc.TYPE_CHAIN, "root", "r", inputs={"q": 1})
        _drive(tracker, lc.TYPE_TOOL, "shout", "t", parent="r", inputs={"text": "hi"})
        tracker.end("t", "HI")
        tracker.end("r", {"output": "done"})

    events = tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events
    assert [e.kind for e in events] == ["chain", "chain"]
    assert [e.req["name"] for e in events] == ["shout", "root"]
    assert [e.req["depth"] for e in events] == [1, 0]
    assert [e.req["path"] for e in events] == ["root/shout", "root"]
    assert events[0].req["type"] == "tool"
    assert events[1].res["children"] == 1
    assert events[1].res["value"] == "done"


def test_a_model_node_is_tracked_for_depth_and_never_written(tape_dir):
    """The transport shim owns that crossing; two events would be two
    recordings of one boundary."""
    tracker = lc.Tracker()
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01REC"):
        _drive(tracker, lc.TYPE_CHAIN, "root", "r")
        _drive(tracker, lc.TYPE_LLM, "ChatOpenAI", "m", parent="r")
        tracker.end("m", "answer")
        tracker.end("r", "done")

    events = tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events
    assert [e.req["name"] for e in events] == ["root"]
    assert events[0].res["children"] == 1, "the model node was not counted"


def test_a_node_that_raised_is_recorded_as_a_crossing_that_failed(tape_dir):
    tracker = lc.Tracker()
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01REC"):
        _drive(tracker, lc.TYPE_TOOL, "shout", "t")
        tracker.error("t", RuntimeError("no"))

    event = tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events[0]
    assert event.meta["error"] == {"type": "RuntimeError", "message": "no"}


def test_an_agent_step_is_recorded_with_no_duration(tape_dir):
    tracker = lc.Tracker()
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01REC"):
        _drive(tracker, lc.TYPE_CHAIN, "AgentExecutor", "r")
        tracker.point(lc.TYPE_AGENT_ACTION, "search", "r", "r",
                      {"tool": "search", "tool_input": "cats"})
        tracker.end("r", "done")

    events = tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events
    assert events[0].req["type"] == "agent_action"
    assert events[0].req["name"] == "search"
    assert events[0].res["outputs"]["tool_input"] == "cats"


def test_a_node_that_never_ends_does_not_leak_into_the_next_run(tape_dir):
    tracker = lc.Tracker()
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01REC"):
        _drive(tracker, lc.TYPE_CHAIN, "root", "r")
    assert tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events == []


def test_the_tracker_is_inert_outside_a_tape():
    """A handler left in shipped code must not need a tape to exist."""
    tracker = lc.Tracker()
    _drive(tracker, lc.TYPE_CHAIN, "root", "r")
    tracker.end("r", "done")


def test_replaying_a_run_recorded_without_the_adapter_says_so(tape_dir):
    """An ordinary TapeMiss cannot express "the adapter was switched on after
    the recording was made"."""
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01REC"):
        tape.record_event("tool", {"name": "x"}, {"value": 1})

    tracker = lc.Tracker()
    with tape.session("replay", tape_dir=tape_dir, replay="01REC", collect_git=False):
        _drive(tracker, lc.TYPE_CHAIN, "root", "r")
        with pytest.raises(TapeConfigError, match="recorded without the LangChain"):
            tracker.end("r", "done")


# -- install / uninstall -------------------------------------------------


@needs_core
def test_install_arms_the_adapter_and_uninstall_disarms_it():
    assert not lc.installed()
    lc.install()
    try:
        assert lc.installed()
        assert os.environ[lc.ENV_VAR]
    finally:
        lc.uninstall()
    assert not lc.installed()


@needs_core
def test_installing_twice_is_harmless():
    lc.install()
    lc.install()
    lc.uninstall()


@needs_core
def test_recording_scopes_the_adapter_to_a_block():
    with lc.recording():
        assert lc.installed()
    assert not lc.installed()


@needs_core
def test_the_handler_refuses_to_swallow_a_replay_failure():
    """LangChain logs and continues past any exception a handler raises, unless
    the handler says otherwise. A swallowed TapeMiss is a replay that sailed
    past a call it could not match."""
    handler = lc.handler()
    assert handler.raise_error is True


@needs_core
def test_the_handler_refuses_to_be_dispatched_to_a_worker_thread():
    """Without this, every async path runs the handler in an executor whose
    stack holds no user frame at all, and the handlers are gathered
    concurrently -- so call sites and ordering both stop being real."""
    assert lc.handler().run_inline is True


# -- the callback surface itself -----------------------------------------
#
# Driven directly rather than through a chain. These twelve methods *are* the
# contract with LangChain, and their signatures are the thing most likely to
# move under a version bump -- so each one is called the way the framework
# calls it, with keywords, and checked for the event it should produce.


@pytest.fixture
def handler_on(tape_dir):
    """A handler writing into a fresh run, and the run's events afterwards."""
    import uuid as _uuid

    # Minted before the tape is installed, on purpose: `uuid4` is one of the
    # ambient sources reeltime patches, so drawing one inside the run would
    # record a `uuid` event of the test's own making.
    made = {label: _uuid.uuid4() for label in ("a", "p", "m", "r", "t")}

    class Session:
        def __init__(self, run):
            self.run = run
            self.handler = lc.handler()

        def uid(self, label):
            return made[label]

    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01REC") as run:
        yield Session(run)


def _events(tape_dir):
    return tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events


@needs_core
def test_a_chain_callback_pair_becomes_one_event(handler_on, tape_dir):
    run_id = handler_on.uid("a")
    handler_on.handler.on_chain_start({"name": "s"}, {"q": 1}, run_id=run_id,
                                      parent_run_id=None, tags=[], metadata={})
    handler_on.handler.on_chain_end({"output": "done"}, run_id=run_id)
    handler_on.run.close()

    event = _events(tape_dir)[0]
    assert event.kind == "chain" and event.req["type"] == "chain"
    assert event.res["value"] == "done"


@needs_core
def test_a_chain_that_errored_is_recorded_through_the_handler(handler_on, tape_dir):
    run_id = handler_on.uid("a")
    handler_on.handler.on_chain_start(None, {}, run_id=run_id, parent_run_id=None,
                                      tags=[], metadata={})
    handler_on.handler.on_chain_error(ValueError("bad"), run_id=run_id)
    handler_on.run.close()
    assert _events(tape_dir)[0].meta["error"]["type"] == "ValueError"


@needs_core
def test_a_tool_that_errored_is_recorded_through_the_handler(handler_on, tape_dir):
    run_id = handler_on.uid("t")
    handler_on.handler.on_tool_start({"name": "shout"}, "hi", run_id=run_id,
                                     parent_run_id=None, inputs={"text": "hi"})
    handler_on.handler.on_tool_error(RuntimeError("gone"), run_id=run_id)
    handler_on.run.close()
    event = _events(tape_dir)[0]
    assert event.req["type"] == "tool"
    assert event.meta["error"]["type"] == "RuntimeError"


@needs_core
def test_a_tool_with_no_structured_inputs_records_the_raw_string(handler_on, tape_dir):
    """Older tools are called with a string; `inputs` arrived later."""
    run_id = handler_on.uid("t")
    handler_on.handler.on_tool_start({"name": "shout"}, "hi", run_id=run_id,
                                     parent_run_id=None, inputs=None)
    handler_on.handler.on_tool_end("HI", run_id=run_id)
    handler_on.run.close()
    assert _events(tape_dir)[0].req["inputs"] == "hi"


@needs_core
def test_a_retriever_records_its_query_and_its_documents(handler_on, tape_dir):
    run_id = handler_on.uid("r")
    handler_on.handler.on_retriever_start({"name": "vectors"}, "cats", run_id=run_id,
                                          parent_run_id=None, tags=[], metadata={})
    handler_on.handler.on_retriever_end([{"page_content": "a cat"}], run_id=run_id)
    handler_on.run.close()
    event = _events(tape_dir)[0]
    assert event.req["type"] == "retriever"
    assert event.req["inputs"] == {"query": "cats"}
    assert event.res["outputs"] == [{"page_content": "a cat"}]


@needs_core
def test_a_retriever_that_errored_is_recorded(handler_on, tape_dir):
    run_id = handler_on.uid("r")
    handler_on.handler.on_retriever_start(None, "cats", run_id=run_id,
                                          parent_run_id=None)
    handler_on.handler.on_retriever_error(OSError("index gone"), run_id=run_id)
    handler_on.run.close()
    assert _events(tape_dir)[0].meta["error"]["type"] == "OSError"


@needs_core
def test_a_completion_model_node_is_skipped_like_a_chat_model_node(handler_on, tape_dir):
    """`on_llm_start` is the non-chat spelling of the same crossing."""
    parent, model = handler_on.uid("p"), handler_on.uid("m")
    handler_on.handler.on_chain_start(None, {}, run_id=parent, parent_run_id=None)
    handler_on.handler.on_llm_start({"name": "OpenAI"}, ["say hi"], run_id=model,
                                    parent_run_id=parent, tags=[], metadata={})
    handler_on.handler.on_llm_end(object(), run_id=model, parent_run_id=parent)
    handler_on.handler.on_chain_end({}, run_id=parent)
    handler_on.run.close()

    events = _events(tape_dir)
    assert len(events) == 1
    assert events[0].res["children"] == 1


@needs_core
def test_a_model_that_errored_writes_nothing_either(handler_on, tape_dir):
    """The transport shim records the failed request; this would be its double."""
    model = handler_on.uid("m")
    handler_on.handler.on_chat_model_start({"name": "ChatOpenAI"}, [[]], run_id=model,
                                           parent_run_id=None)
    handler_on.handler.on_llm_error(TimeoutError("slow"), run_id=model)
    handler_on.run.close()
    assert _events(tape_dir) == []


@needs_core
def test_an_agent_action_and_finish_are_recorded_as_agent_steps(handler_on, tape_dir):
    class Action:
        tool = "search"
        tool_input = "cats"

    class Finish:
        return_values = {"output": "done"}

    parent = handler_on.uid("p")
    handler_on.handler.on_chain_start({"name": "AgentExecutor"}, {}, run_id=parent,
                                      parent_run_id=None)
    handler_on.handler.on_agent_action(Action(), run_id=parent, parent_run_id=parent)
    handler_on.handler.on_agent_finish(Finish(), run_id=parent, parent_run_id=parent)
    handler_on.handler.on_chain_end({"output": "done"}, run_id=parent)
    handler_on.run.close()

    events = _events(tape_dir)
    assert [e.req["type"] for e in events] == [
        "agent_action", "agent_finish", "chain"]
    assert events[0].req["name"] == "search"
    assert events[0].req["path"] == "AgentExecutor/search"
    assert events[1].res["outputs"] == {"output": "done"}


@needs_core
def test_a_handler_can_be_built_with_a_tracker_of_its_own():
    tracker = lc.Tracker()
    assert lc.handler_class()(tracker).tracker is tracker


# -- the awkward corners -------------------------------------------------


def test_importing_reeltime_does_not_import_the_framework():
    """`import reeltime` costs nothing for the majority of users who have no
    LangChain, and reeltime's runtime stays standard-library-only.

    A subprocess, because this suite has already imported langchain_core.
    """
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys, reeltime; "
         "print(sorted(m for m in sys.modules if m.startswith('langchain')))"],
        capture_output=True, text=True, timeout=120)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "[]"


def test_a_framework_that_is_not_installed_is_reported_as_such(monkeypatch):
    monkeypatch.setattr(lc, "PACKAGE", "a-package-that-does-not-exist")
    assert lc._installed_version() is None
    with pytest.raises(TapeError, match="pip install"):
        lc.check_version()


def test_the_handler_class_needs_the_framework(monkeypatch):
    import importlib

    def refuse(name):
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", refuse)
    with pytest.raises(TapeError, match="pip install"):
        lc._base_handler()


def test_the_environment_can_force_an_unsupported_version(monkeypatch):
    monkeypatch.setattr(lc, "_installed_version", lambda: "9.9.9")
    monkeypatch.setenv(lc.ENV_VAR, "force")
    monkeypatch.setattr(lc, "_register", lambda: None)
    lc.install()
    assert lc.installed()


def test_unwrapping_a_pathological_nest_stops_rather_than_recursing():
    value = {"output": {"output": {"output": {"output": {"output": {
        "output": {"output": "deep"}}}}}}}
    assert lc._unwrap(value) == {"output": "deep"}


def test_stable_stops_at_a_depth_no_recorded_payload_can_reach():
    assert lc.stable({"id": "a71294c3-745a-447e-99b5-6f01848fa737"}, _depth=99) == {
        "id": "a71294c3-745a-447e-99b5-6f01848fa737"}


def test_only_a_chain_event_has_a_structure():
    assert lc.structure(_http(0)) is None
    assert lc.structure(_chain(0, "model", path="a/b", depth=1, children=2)) == (
        "a/b", 1, 2)


def test_a_forked_run_finds_the_trace_it_is_replaying(tape_dir):
    """`_guard` has to reach the parent trace through a fork engine too."""
    from reeltime.core.fork import ForkEngine

    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01REC"):
        tape.record_event("tool", {"name": "x"}, {"value": 1})

    tape.install("fork", tape_dir=tape_dir, collect_git=False, replay="01REC",
                 fork_at=1, run_id="01FORK")
    try:
        engine = tape.current().engine
        assert isinstance(engine, ForkEngine)
        assert lc._trace_of(engine).run_id == "01REC"
    finally:
        tape.uninstall()


# -- record and replay through langchain-core ----------------------------


def _lcel_agent():
    """One call site for both halves of a record/replay pair.

    Two separately-defined closures would sit on different lines, and a replay
    would then miss on the call site rather than on anything to do with the
    adapter.
    """
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate

    chain = (ChatPromptTemplate.from_messages([("human", "{q}")])
             | FakeListChatModel(responses=["hello there"])
             | StrOutputParser())

    def go():
        return chain.invoke({"q": "hi"}, config={"callbacks": [lc.handler()]})

    return go


def _record(tape_dir, fn, run_id="01REC", **kwargs):
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id=run_id, **kwargs):
        return fn()


def _replay(tape_dir, fn, run_id="01REC", **kwargs):
    with tape.session("replay", tape_dir=tape_dir, replay=run_id,
                      collect_git=False, **kwargs) as run:
        result = fn()
    return result, run


@needs_core
def test_an_lcel_chain_records_its_nodes(tape_dir):
    agent = _lcel_agent()
    assert _record(tape_dir, agent) == "hello there"

    events = tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events
    assert {e.kind for e in events} == {"chain"}
    by_name = {e.req["name"]: e for e in events}
    assert set(by_name) == {"ChatPromptTemplate", "StrOutputParser", "RunnableSequence"}
    assert by_name["ChatPromptTemplate"].req["type"] == "prompt"
    assert by_name["StrOutputParser"].req["type"] == "parser"
    assert by_name["RunnableSequence"].req["depth"] == 0
    assert by_name["ChatPromptTemplate"].req["depth"] == 1


@needs_core
def test_a_recorded_chain_replays_with_no_drift(tape_dir):
    agent = _lcel_agent()
    recorded = _record(tape_dir, agent)
    replayed, run = _replay(tape_dir, agent)
    assert replayed == recorded
    assert run.summary.events == 3
    assert run.summary.drifts == []
    assert run.summary.unconsumed == []


@needs_core
def test_every_node_in_a_run_tree_shares_the_call_site_of_the_invoke(tape_dir):
    """LangGraph runs nodes on a thread pool, so a child's own stack often has
    no user frame on it -- and the nearest answer would be a line number inside
    langchain, which moves on every upgrade."""
    _record(tape_dir, _lcel_agent())
    events = tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events
    sites = {e.site for e in events}
    assert len(sites) == 1
    assert "site-packages" not in sites.pop()


@needs_core
def test_parallel_branches_are_told_apart_by_their_map_key(tape_dir):
    from langchain_core.runnables import RunnableLambda, RunnableParallel

    par = RunnableParallel(a=RunnableLambda(lambda x: x + 1),
                           b=RunnableLambda(lambda x: x + 2))

    def go():
        return par.invoke(1, config={"callbacks": [lc.handler()]})

    assert _record(tape_dir, go) == {"a": 2, "b": 3}
    events = tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events
    steps = sorted(e.req.get("step") for e in events if e.req.get("step"))
    assert steps == ["map:key:a", "map:key:b"]
    assert _replay(tape_dir, go)[1].summary.drifts == []


@needs_core
def test_a_tool_records_its_arguments_and_its_result(tape_dir):
    from langchain_core.tools import tool

    @tool
    def shout(text: str) -> str:
        """Shout it."""
        return text.upper()

    def go():
        return shout.invoke({"text": "hi"}, config={"callbacks": [lc.handler()]})

    assert _record(tape_dir, go) == "HI"
    event = tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events[0]
    assert event.req["type"] == "tool"
    assert event.req["name"] == "shout"
    assert event.req["inputs"] == {"text": "hi"}
    assert event.res["value"] == "HI"


@needs_core
def test_a_tool_that_raised_is_recorded_and_raised_again(tape_dir):
    from langchain_core.tools import tool

    @tool
    def explode(text: str) -> str:
        """Fail."""
        raise ValueError("nope")

    def go():
        return explode.invoke({"text": "hi"}, config={"callbacks": [lc.handler()]})

    with pytest.raises(ValueError):
        _record(tape_dir, go)
    event = tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events[-1]
    assert event.kind == "chain"
    assert event.meta["error"]["type"] == "ValueError"


@needs_core
def test_the_async_path_records_the_same_tree_as_the_sync_one(tape_dir, tmp_path):
    """`run_inline` is what makes this true: dispatched to an executor the
    handler would see neither the user's stack nor a reliable order."""
    from langchain_core.runnables import RunnableLambda

    inner = RunnableLambda(lambda x: x + 1).with_config(run_name="inner")
    outer = RunnableLambda(lambda x: inner.invoke(x) * 2).with_config(run_name="outer")

    def go():
        return asyncio.run(outer.ainvoke(3, config={"callbacks": [lc.handler()]}))

    assert _record(tape_dir, go) == 8
    events = tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events
    assert [e.req["path"] for e in events] == ["outer/inner", "outer"]
    assert [e.req["depth"] for e in events] == [1, 0]
    assert _replay(tape_dir, go)[1].summary.drifts == []


@needs_core
def test_a_chain_inside_a_recorded_tool_body_is_not_recorded_twice(tape_dir):
    """The outermost boundary is the one recorded: on replay a `@tape.tool`
    body does not run at all, so anything recorded inside it could never be
    matched."""
    from langchain_core.runnables import RunnableLambda

    inner = RunnableLambda(lambda x: x + 1).with_config(run_name="inner")

    @tape.tool
    def compute(n):
        return inner.invoke(n, config={"callbacks": [lc.handler()]})

    def go():
        return compute(1)

    assert _record(tape_dir, go) == 2
    kinds = [e.kind for e in tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events]
    assert kinds == ["tool"]


@needs_core
def test_two_identical_runs_record_identical_node_payloads(tape_dir, tmp_path):
    """The whole reason per-run message ids are stripped: without it `tape diff`
    reports noise at every step of two runs that did the same thing."""
    agent = _lcel_agent()
    _record(tape_dir, agent, run_id="01A")
    _record(tape_dir, agent, run_id="01B")

    a = tape.read_trace(tape_dir / "runs" / "01A.jsonl")
    b = tape.read_trace(tape_dir / "runs" / "01B.jsonl")
    assert [e.req for e in a.events] == [e.req for e in b.events]
    assert [e.res for e in a.events] == [e.res for e in b.events]
    assert tracediff.diff(a, b).identical


@needs_core
def test_a_node_that_disappeared_is_reported_rather_than_replayed(tape_dir):
    """Nothing falls through: a chain whose shape changed says so."""
    from langchain_core.runnables import RunnableLambda

    def build(names):
        chain = RunnableLambda(lambda x: x).with_config(run_name=names[0])
        for name in names[1:]:
            chain = chain | RunnableLambda(lambda x: x).with_config(run_name=name)
        return chain

    recorded = build(["a", "b"])
    changed = build(["a"])

    def go(chain):
        return chain.invoke(1, config={"callbacks": [lc.handler()]})

    _record(tape_dir, lambda: go(recorded))
    _, run = _replay(tape_dir, lambda: go(changed))
    assert run.summary.unconsumed, "a node that vanished went unreported"


# -- a real agent, end to end -------------------------------------------


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _tape(tape_dir, *command, **env_extra):
    env = dict(os.environ, **env_extra)
    return subprocess.run(
        [sys.executable, "-m", "reeltime.cli", "--tape-dir", str(tape_dir)]
        + list(command),
        env=env, capture_output=True, text=True, timeout=300,
    )


def _only_trace(tape_dir):
    run_ids = paths.list_run_ids(tape_dir)
    assert len(run_ids) == 1, run_ids
    return tape.read_trace(paths.trace_path(tape_dir, run_ids[0]))


@pytest.fixture
def example_port():
    """A free port, held fixed across a record and its replay.

    The request URL is part of what replay matches on, so an ephemeral port
    chosen twice would report every model call as drifted.
    """
    return str(_free_port())


@needs_agent
def test_the_example_records_the_agent_as_a_tree(tmp_path, example_port):
    tape_dir = tmp_path / ".tape"
    result = _tape(tape_dir, "run", sys.executable, str(EXAMPLES / "langchain_agent.py"),
                   LANGCHAIN_EXAMPLE_PORT=example_port)
    assert result.returncode == 0, result.stderr
    assert "answer: Counted 9 words." in result.stdout

    trace = _only_trace(tape_dir)
    assert {e.kind for e in trace.events} == {"llm", "chain"}

    chains = [e for e in trace.events if e.kind == "chain"]
    assert [e.req["name"] for e in chains] == [
        "model", "word_count", "tools", "model", "LangGraph"]
    assert [e.req["depth"] for e in chains] == [1, 2, 1, 1, 0]
    root = chains[-1]
    assert root.req["path"] == "LangGraph"
    assert root.res["children"] == 3


@needs_agent
def test_a_model_node_produces_one_event_and_not_two(tmp_path, example_port):
    """The boundary rule, and the reason the adapter skips LLM nodes.

    A `ChatOpenAI` node fires `on_chat_model_start` *and* issues an HTTP POST
    that the transport shim records. Recording both would be two events for one
    crossing -- so the count of `llm` events must equal the count of requests
    the provider actually received, and no `chain` event may describe a model.
    """
    tape_dir = tmp_path / ".tape"
    assert _tape(tape_dir, "run", sys.executable,
                 str(EXAMPLES / "langchain_agent.py"),
                 LANGCHAIN_EXAMPLE_PORT=example_port).returncode == 0

    trace = _only_trace(tape_dir)
    # The mock answers twice: a tool call, then the final message.
    assert len([e for e in trace.events if e.kind == "llm"]) == 2
    assert [e for e in trace.events if e.kind == "chain"
            and e.req.get("type") == "llm"] == []
    # And the model node's HTTP call is not swallowed by the node around it.
    assert all(e.res.get("status") == 200
               for e in trace.events if e.kind == "llm")


@needs_agent
def test_the_example_replays_offline_and_identically(tmp_path, example_port):
    tape_dir = tmp_path / ".tape"
    example = str(EXAMPLES / "langchain_agent.py")
    recorded = _tape(tape_dir, "run", sys.executable, example,
                     LANGCHAIN_EXAMPLE_PORT=example_port)
    assert recorded.returncode == 0, recorded.stderr

    replayed = _tape(tape_dir, "replay", "last", LANGCHAIN_EXAMPLE_PORT=example_port)
    assert replayed.returncode == 0, replayed.stderr
    assert replayed.stdout == recorded.stdout
    assert "replayed 7 events" in replayed.stderr
    assert "never requested" not in replayed.stderr


@needs_agent
def test_replay_turns_the_adapter_on_because_the_tape_needs_it(tmp_path, example_port):
    """The tape knows which adapters were on; the replay should not need to be
    told again, or every node would go unclaimed and the summary would report
    that the agent took a different path."""
    tape_dir = tmp_path / ".tape"
    env = {"LANGCHAIN_EXAMPLE_PORT": example_port}
    assert _tape(tape_dir, "run", sys.executable,
                 str(EXAMPLES / "langchain_agent.py"), **env).returncode == 0

    trace = _only_trace(tape_dir)
    child_env = {}
    from reeltime.cli import _match_adapters

    _match_adapters(child_env, trace)
    assert child_env == {lc.ENV_VAR: "1"}


@needs_agent
def test_tape_run_langchain_records_a_script_that_never_imports_reeltime(
        tmp_path, example_port):
    """Zero-edit adoption, the first design principle, for a framework agent."""
    script = tmp_path / "agent.py"
    script.write_text((EXAMPLES / "langchain_agent.py").read_text()
                      .replace("import reeltime as tape\n", "")
                      .replace("    tape.langchain.install()\n", ""))
    tape_dir = tmp_path / ".tape"

    result = _tape(tape_dir, "run", "--langchain", sys.executable, str(script),
                   LANGCHAIN_EXAMPLE_PORT=example_port)
    assert result.returncode == 0, result.stderr
    assert any(e.kind == "chain" for e in _only_trace(tape_dir).events)


@needs_agent
def test_without_the_flag_the_same_script_records_only_its_http_calls(
        tmp_path, example_port):
    """The adapter is opt-in, and the cost of that is one import of
    langchain-core at startup -- about a second."""
    script = tmp_path / "agent.py"
    script.write_text((EXAMPLES / "langchain_agent.py").read_text()
                      .replace("import reeltime as tape\n", "")
                      .replace("    tape.langchain.install()\n", ""))
    tape_dir = tmp_path / ".tape"

    assert _tape(tape_dir, "run", sys.executable, str(script),
                 LANGCHAIN_EXAMPLE_PORT=example_port).returncode == 0
    assert {e.kind for e in _only_trace(tape_dir).events} == {"llm"}


@needs_agent
def test_a_bigger_tool_set_shows_up_as_a_changed_graph(tmp_path, example_port):
    """The example's own claim: an extra tool sends the agent round the graph
    once more, and the diff names that as structure rather than as a payload."""
    tape_dir = tmp_path / ".tape"
    example = str(EXAMPLES / "langchain_agent.py")
    assert _tape(tape_dir, "run", sys.executable, example,
                 LANGCHAIN_EXAMPLE_PORT=example_port).returncode == 0
    assert _tape(tape_dir, "run", sys.executable, example,
                 LANGCHAIN_EXAMPLE_PORT=example_port,
                 LANGCHAIN_EXAMPLE_TOOLS="extended").returncode == 0

    a, b = sorted(paths.list_run_ids(tape_dir))
    result = _tape(tape_dir, "diff", a, b)
    assert result.returncode == 0, result.stderr
    assert "only in B: reverse" in result.stdout
    assert "chain fan-out changed" in result.stdout


@needs_agent
def test_show_renders_a_node_as_its_place_in_the_tree(tmp_path, example_port):
    tape_dir = tmp_path / ".tape"
    assert _tape(tape_dir, "run", sys.executable,
                 str(EXAMPLES / "langchain_agent.py"),
                 LANGCHAIN_EXAMPLE_PORT=example_port).returncode == 0
    run_id = paths.list_run_ids(tape_dir)[0]

    listing = _tape(tape_dir, "show", run_id)
    assert listing.returncode == 0, listing.stderr
    assert "word_count [tool]" in listing.stdout
    assert "(3 children)" in listing.stdout

    node = _tape(tape_dir, "show", run_id, "2")
    assert node.returncode == 0, node.stderr
    assert "path     LangGraph/tools/word_count" in node.stdout
    assert "type     tool" in node.stdout
    assert "structure, not a boundary" in node.stdout

    raw = _tape(tape_dir, "show", run_id, "2", "--raw")
    assert '"kind": "chain"' in raw.stdout


@needs_agent
def test_only_chain_narrows_a_mixed_trace(tmp_path, example_port):
    tape_dir = tmp_path / ".tape"
    example = str(EXAMPLES / "langchain_agent.py")
    for _ in range(2):
        assert _tape(tape_dir, "run", sys.executable, example,
                     LANGCHAIN_EXAMPLE_PORT=example_port).returncode == 0

    a, b = sorted(paths.list_run_ids(tape_dir))
    result = _tape(tape_dir, "diff", a, b, "--only", "chain", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert {step["event_kind"] for step in payload["steps"]} == {"chain"}
    assert payload["identical"] is True


# -- builders ------------------------------------------------------------


def _trace(events, run_id="01A"):
    from reeltime.core.trace import Header, Trace

    return Trace(
        header=Header(run_id=run_id, started="", argv=["agent.py"], cwd="", python=""),
        events=list(events),
        footer={"events": len(events), "cost_usd": 0.0, "tokens": {"in": 0, "out": 0}},
    )


def _http(index, site="agent.py:1"):
    return Event(i=index, kind="http", site=site,
                 req={"method": "POST", "url": "http://x/v1", "body": {}},
                 res={"status": 200})


def _chain(index, name, site="agent.py:1", path=None, depth=0, node_type="chain",
           children=0, value="ok", step=None):
    return Event(
        i=index, kind="chain", site=site,
        req=lc.request(name, node_type, path or name, depth, {"in": 1}, step),
        res={"outputs": {"out": value}, "value": value, "children": children},
    )
