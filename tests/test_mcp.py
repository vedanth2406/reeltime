"""The MCP adapter (M5.5).

Three layers, deliberately:

* payload and naming logic, which needs no SDK at all;
* record/replay through an in-process fake session, which is where the
  behaviour lives and where it is cheap to cover exhaustively;
* both real transports, driven end to end, because a fake session cannot show
  that replay leaves the server process unstarted -- which is the claim.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import reeltime as tape
from reeltime.core import mcp as mcp_mod
from reeltime.core import tracediff
from reeltime.core.matching import filter_kinds, kind_key
from reeltime.core.trace import Event
from reeltime.errors import TapeError, TapeMiss

try:
    import mcp as mcp_sdk
    import mcp.types as mcp_types
except ImportError:  # the SDK requires Python 3.10; reeltime supports 3.9
    mcp_sdk = mcp_types = None

needs_sdk = pytest.mark.skipif(mcp_sdk is None, reason="the MCP SDK needs Python 3.10+")

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_the_sdk_is_installed_wherever_it_can_be():
    """A skipped section has to be a decision, not an accident.

    `numpy` was once a missing dev dependency, and a module-level
    `importorskip` turned that into 25 silently skipped tests rather than 2.
    This asserts the same thing cannot happen here: on any interpreter the SDK
    supports, it must be installed and the transport tests must actually run.
    """
    if sys.version_info >= (3, 10):
        assert mcp_sdk is not None, (
            "mcp is a dev dependency on 3.10+; without it every transport test "
            "in this file skips without anyone noticing"
        )


# -- naming a server -----------------------------------------------------


def test_a_url_names_itself():
    assert mcp_mod.server_id(url="http://localhost:8931/mcp") == "http://localhost:8931/mcp"


def test_a_stdio_command_is_named_without_its_absolute_path():
    """Server identity is part of the match key, so it must not move."""
    first = mcp_mod.server_id("/usr/bin/python3.11", ["/tmp/pytest-abc/server.py"])
    second = mcp_mod.server_id("/opt/homebrew/bin/python3.11", ["/tmp/pytest-xyz/server.py"])
    assert first == second == "python3.11 server.py"


def test_a_windows_style_path_is_shortened_too():
    assert mcp_mod.server_id(r"C:\Python\python.exe", []) == "python.exe"


def test_a_session_says_whether_anything_is_on_the_other_end():
    live = mcp_mod.TapedSession(object(), server="files", transport="stdio")
    replaying = mcp_mod.TapedSession(None, server="files", transport="stdio")
    assert live.live and not replaying.live
    assert "files" in repr(replaying) and "replay" in repr(replaying)


# -- what a server subprocess is allowed to inherit ----------------------


def test_the_server_subprocess_does_not_inherit_the_recording():
    """Otherwise it opens the same trace file and appends its own run to it."""
    cleaned = mcp_mod._clean_env({
        "REELTIME_AUTOINSTALL": "1",
        "REELTIME_RUN_ID": "01ABC",
        "REELTIME_MODE": "record",
        "TAPE_DIR": "/x/.tape",
        "HOME": "/home/v",
        "MCP_EXAMPLE_TOOLS": "extended",
    })
    assert cleaned == {"HOME": "/home/v", "MCP_EXAMPLE_TOOLS": "extended"}


def test_the_bootstrap_shim_is_stripped_off_pythonpath():
    cleaned = mcp_mod._clean_env(
        {"PYTHONPATH": "/site/reeltime/_bootstrap:/my/libs"})
    assert cleaned["PYTHONPATH"] == "/my/libs"


def test_a_pythonpath_that_was_only_the_shim_is_removed_entirely():
    cleaned = mcp_mod._clean_env({"PYTHONPATH": "/site/reeltime/_bootstrap"})
    assert "PYTHONPATH" not in cleaned


def test_no_env_stays_no_env():
    assert mcp_mod._clean_env(None) is None


# -- event payloads ------------------------------------------------------


def test_a_discovery_result_keeps_the_tool_names_inline():
    """`tape diff` has no blob store, so the names cannot live in a blob."""
    wire = {"tools": [{"name": "read", "inputSchema": {}},
                      {"name": "write", "inputSchema": {}}]}
    res = mcp_mod.list_result(wire)
    assert res["tools"] == ["read", "write"]
    assert res["count"] == 2
    assert res["result"] is wire


def test_a_result_the_sdk_did_not_produce_is_still_recordable():
    """Not every session hands back a pydantic model; none of them may crash."""
    assert mcp_mod._to_wire({"tools": []}) == {"tools": []}


def test_a_discovery_result_survives_a_server_with_no_tools():
    assert mcp_mod.list_result({})["tools"] == []


def test_a_call_result_prefers_structured_output():
    wire = {"content": [{"type": "text", "text": "5"}],
            "structuredContent": {"result": 5}}
    assert mcp_mod.call_result(wire)["value"] == {"result": 5}


def test_a_call_result_falls_back_to_the_text_blocks():
    wire = {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    assert mcp_mod.call_result(wire)["value"] == "a\nb"


def test_a_call_result_keeps_non_text_content_rather_than_dropping_it():
    wire = {"content": [{"type": "image", "data": "…"}]}
    assert mcp_mod.call_result(wire)["value"] == [{"type": "image", "data": "…"}]


def test_a_tool_error_is_flagged_without_being_an_exception():
    wire = {"content": [{"type": "text", "text": "nope"}], "isError": True}
    assert mcp_mod.call_result(wire)["is_error"] is True


def test_the_handshake_records_who_answered():
    wire = {"protocolVersion": "2025-11-25",
            "serverInfo": {"name": "files", "version": "1.2.0"},
            "capabilities": {"tools": {}, "resources": {}}}
    res = mcp_mod.init_result(wire)
    assert res["server_name"] == "files"
    assert res["server_version"] == "1.2.0"
    assert res["protocol"] == "2025-11-25"
    assert res["capabilities"] == ["resources", "tools"]


def test_the_op_separates_the_handshake_from_a_tool_of_the_same_name():
    """A server may legitimately expose a tool called `initialize`."""
    handshake = mcp_mod.init_request("files")
    collision = mcp_mod.call_request("files", "initialize", {})
    assert handshake["op"] != collision["op"]
    assert handshake["name"] == collision["name"] == "initialize"

    from reeltime.core.matching import content_key

    assert content_key("mcp", handshake) != content_key("mcp", collision)


def test_arguments_are_made_jsonable_before_they_are_recorded():
    request = mcp_mod.call_request("files", "read", {"when": {1, 2}})
    json.dumps(request)  # would raise if a set had survived


def test_missing_arguments_record_as_an_empty_mapping():
    assert mcp_mod.call_request("files", "ping", None)["args"] == {}


# -- folding, so an older trace still lines up ---------------------------


def test_mcp_folds_into_http_for_alignment():
    """Before the adapter, MCP over HTTP was recorded as opaque http."""
    assert kind_key("mcp") == kind_key("http") == kind_key("llm") == "http"


def test_an_older_opaque_http_recording_still_aligns_with_an_mcp_event():
    old = _trace([_http_event(0, site="agent.py:10")], run_id="01OLD")
    new = _trace([_mcp_call(0, "read", site="agent.py:10")], run_id="01NEW")

    result = tracediff.diff(old, new)
    assert len(result.steps) == 1
    assert result.steps[0].paired, "the two runs would otherwise share nothing"
    assert result.divergence is None


def test_only_mcp_means_mcp_and_not_every_http_call():
    """Folding is for alignment. Filtering is a question the user asked."""
    assert filter_kinds(["mcp"]) == {"mcp"}
    assert filter_kinds(["llm"]) == {"llm"}
    assert filter_kinds(["http"]) == {"http", "llm"}


def test_only_mcp_narrows_a_mixed_trace_to_the_mcp_events():
    events = [_http_event(0), _mcp_call(1, "read"), _http_event(2, site="a.py:9")]
    a = _trace(events, run_id="01A")
    b = _trace(events, run_id="01B")
    result = tracediff.diff(a, b, only=["mcp"])
    assert [s.event.kind for s in result.steps] == ["mcp"]


# -- diff reports a changed tool set as a tool set change ----------------


def test_a_changed_tool_set_is_its_own_line():
    a = _trace([_mcp_list(0, ["read_file", "list_files"])], run_id="01A")
    b = _trace([_mcp_list(0, ["read_file", "delete_file"])], run_id="01B")

    step = tracediff.diff(a, b).steps[0]
    labels = [c.label for c in step.changes]
    assert "tool set changed" in labels

    change = next(c for c in step.changes if c.label == "tool set changed")
    assert change.lines == ["- list_files", "+ delete_file"]
    assert change.before == "2 tools" and change.after == "2 tools"


def test_a_changed_tool_set_is_rendered_not_buried():
    a = _trace([_mcp_list(0, ["read_file"])], run_id="01A")
    b = _trace([_mcp_list(0, ["read_file", "delete_file"])], run_id="01B")
    rendered = tracediff.render(tracediff.diff(a, b))
    assert "tool set changed" in rendered
    assert "+ delete_file" in rendered


def test_the_same_tools_with_changed_schemas_are_reported_separately():
    """Content addressing answers this without either payload being read."""
    a = _trace([_mcp_list(0, ["read_file"], definitions="blob:" + "a" * 64)], run_id="01A")
    b = _trace([_mcp_list(0, ["read_file"], definitions="blob:" + "b" * 64)], run_id="01B")
    labels = [c.label for c in tracediff.diff(a, b).steps[0].changes]
    assert labels == ["tool definitions changed"]


def test_identical_discovery_reports_nothing():
    a = _trace([_mcp_list(0, ["read_file"])], run_id="01A")
    b = _trace([_mcp_list(0, ["read_file"])], run_id="01B")
    assert tracediff.diff(a, b).identical


def test_a_different_server_at_the_same_call_site_is_reported():
    a = _trace([_mcp_call(0, "read", server="files")], run_id="01A")
    b = _trace([_mcp_call(0, "read", server="s3")], run_id="01B")
    labels = [c.label for c in tracediff.diff(a, b).steps[0].changes]
    assert "server" in labels


def test_changed_tool_arguments_are_reported():
    a = _trace([_mcp_call(0, "read", args={"path": "a.txt"})], run_id="01A")
    b = _trace([_mcp_call(0, "read", args={"path": "b.txt"})], run_id="01B")
    labels = [c.label for c in tracediff.diff(a, b).steps[0].changes]
    assert "arguments" in labels


def test_a_tool_that_started_failing_is_reported():
    a = _trace([_mcp_call(0, "read", is_error=False)], run_id="01A")
    b = _trace([_mcp_call(0, "read", is_error=True)], run_id="01B")
    labels = [c.label for c in tracediff.diff(a, b).steps[0].changes]
    assert "tool error" in labels


# -- reading one back out ------------------------------------------------


def test_tool_names_are_only_read_off_a_discovery_event():
    assert mcp_mod.tool_names(_mcp_list(0, ["a"])) == ["a"]
    assert mcp_mod.tool_names(_mcp_call(0, "read")) is None
    assert mcp_mod.tool_names(_http_event(0)) is None


# -- record and replay, through a fake session ---------------------------


class FakeSession:
    """An MCP session with no transport under it.

    Returns real SDK result objects, so everything the adapter does to them --
    dumping to the wire form, rebuilding from a tape -- is exercised for real.
    """

    def __init__(self, tools=("read_file", "list_files"), fail=None):
        self.tools = list(tools)
        self.fail = fail
        self.calls = []
        self.initialized = 0

    async def initialize(self):
        self.initialized += 1
        return mcp_types.InitializeResult.model_validate({
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1.0.0"},
        })

    async def list_tools(self):
        return mcp_types.ListToolsResult.model_validate({
            "tools": [{"name": name, "description": name.replace("_", " "),
                       "inputSchema": {"type": "object",
                                       "properties": {"path": {"type": "string"}}}}
                      for name in self.tools],
        })

    async def call_tool(self, name, arguments=None, *args, **kwargs):
        self.calls.append((name, arguments))
        if self.fail is not None:
            raise self.fail
        return mcp_types.CallToolResult.model_validate({
            "content": [{"type": "text", "text": "{}:{}".format(name, arguments)}],
            "isError": name == "explode",
        })


def _record(tape_dir, coro_fn, run_id="01REC", **kwargs):
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id=run_id, **kwargs):
        return asyncio.run(coro_fn())


def _replay(tape_dir, coro_fn, run_id="01REC", **kwargs):
    with tape.session("replay", tape_dir=tape_dir, replay=run_id,
                      collect_git=False, **kwargs) as run:
        result = asyncio.run(coro_fn())
    return result, run


@needs_sdk
def test_a_wrapped_session_records_discovery_and_calls(tape_dir):
    inner = FakeSession()

    async def go():
        session = tape.mcp.wrap(inner, server="files")
        await session.initialize()
        await session.list_tools()
        await session.call_tool("read_file", {"path": "a.txt"})

    _record(tape_dir, go)
    events = tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events

    assert [e.kind for e in events] == ["mcp", "mcp", "mcp"]
    assert [e.req["op"] for e in events] == ["initialize", "list", "call"]
    assert events[1].res["tools"] == ["read_file", "list_files"]
    assert events[2].req["server"] == "files"
    assert events[2].req["name"] == "read_file"
    assert events[2].req["args"] == {"path": "a.txt"}


@needs_sdk
def test_a_replayed_call_never_reaches_the_session(tape_dir):
    live = FakeSession()

    async def go():
        session = tape.mcp.wrap(live, server="files")
        await session.initialize()
        result = await session.call_tool("read_file", {"path": "a.txt"})
        return result.content[0].text

    recorded = _record(tape_dir, go)
    assert len(live.calls) == 1

    replayed, _ = _replay(tape_dir, go)
    assert replayed == recorded
    assert len(live.calls) == 1, "the replay called the live session"


@needs_sdk
def test_a_replayed_discovery_rebuilds_real_sdk_objects(tape_dir):
    async def go():
        session = tape.mcp.wrap(FakeSession(), server="files")
        await session.initialize()
        listing = await session.list_tools()
        return [(t.name, t.description) for t in listing.tools]

    recorded = _record(tape_dir, go)
    replayed, _ = _replay(tape_dir, go)
    assert replayed == recorded == [("read_file", "read file"),
                                    ("list_files", "list files")]


@needs_sdk
def test_a_replayed_result_keeps_its_error_flag(tape_dir):
    async def go():
        session = tape.mcp.wrap(FakeSession(tools=["explode"]), server="files")
        await session.initialize()
        result = await session.call_tool("explode", {})
        return _is_error(result)

    assert _record(tape_dir, go) is True
    assert _replay(tape_dir, go)[0] is True


@needs_sdk
def test_a_raising_call_is_recorded_and_raised_again(tape_dir):
    async def go():
        session = tape.mcp.wrap(FakeSession(fail=RuntimeError("server gone")),
                                server="files")
        await session.call_tool("read_file", {})

    with pytest.raises(RuntimeError):
        _record(tape_dir, go)

    event = tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events[-1]
    assert event.kind == "mcp"
    assert event.meta["error"]["type"] == "RuntimeError"


@needs_sdk
def test_calling_a_tool_that_was_not_recorded_misses(tape_dir):
    async def record():
        session = tape.mcp.wrap(FakeSession(), server="files")
        await session.call_tool("read_file", {"path": "a.txt"})

    async def replay():
        session = tape.mcp.wrap(FakeSession(), server="files")
        await session.call_tool("delete_file", {"path": "a.txt"})

    _record(tape_dir, record)
    with pytest.raises(TapeMiss):
        _replay(tape_dir, replay)


@needs_sdk
def test_the_same_tool_called_twice_replays_in_order(tape_dir):
    async def go():
        session = tape.mcp.wrap(FakeSession(), server="files")
        first = await session.call_tool("read_file", {"path": "a.txt"})
        second = await session.call_tool("read_file", {"path": "b.txt"})
        return first.content[0].text, second.content[0].text

    recorded = _record(tape_dir, go)
    assert _replay(tape_dir, go)[0] == recorded


@needs_sdk
def test_two_servers_at_one_call_site_stay_distinct(tape_dir):
    """Server identity is in the match key, so these must not swap."""
    async def go():
        results = []
        for name in ("files", "s3"):
            session = tape.mcp.wrap(FakeSession(), server=name)
            results.append((await session.call_tool("read_file", {"n": name})).content[0].text)
        return results

    recorded = _record(tape_dir, go)
    assert _replay(tape_dir, go)[0] == recorded

    events = tape.read_trace(tape_dir / "runs" / "01REC.jsonl").events
    assert [e.req["server"] for e in events] == ["files", "s3"]


@needs_sdk
def test_discovery_outside_a_tape_just_works():
    inner = FakeSession()
    names = [t.name for t in asyncio.run(tape.mcp.wrap(inner, "files").list_tools()).tools]
    assert names == ["read_file", "list_files"]


@needs_sdk
def test_anything_reeltime_does_not_record_reaches_the_real_session():
    class WithExtras(FakeSession):
        async def read_resource(self, uri):
            return "resource:" + uri

    session = tape.mcp.wrap(WithExtras(), "files")
    assert asyncio.run(session.read_resource("file:///a")) == "resource:file:///a"


@needs_sdk
def test_a_forked_tool_result_is_substituted_without_calling_the_server(tape_dir):
    """`--patch mcp.read_file.result=` is the point of forking an MCP run."""
    from reeltime.core.patch import parse_all

    live = FakeSession()

    def agent():
        async def go():
            session = tape.mcp.wrap(live, server="files")
            await session.initialize()
            result = await session.call_tool("read_file", {"path": "a.txt"})
            return result.content[0].text

        return asyncio.run(go())

    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01PARENT"):
        agent()
    assert len(live.calls) == 1

    tape.install("fork", tape_dir=tape_dir, collect_git=False, replay="01PARENT",
                 fork_at=1, run_id="01FORK",
                 patches=parse_all(['mcp.read_file.result="<empty file>"']))
    try:
        forked = agent()
    finally:
        tape.uninstall()

    assert forked == "<empty file>"
    assert len(live.calls) == 1, "a substituted result must not call the server"

    event = tape.read_trace(tape_dir / "runs" / "01FORK.jsonl").events[-1]
    assert event.kind == "mcp" and event.meta["patched"] is True


@needs_sdk
def test_a_substituted_result_that_is_not_a_string_is_still_recordable(tape_dir):
    from reeltime.core.patch import parse_all

    def agent():
        async def go():
            session = tape.mcp.wrap(FakeSession(), server="files")
            await session.initialize()
            return (await session.call_tool("read_file", {})).content[0].text

        return asyncio.run(go())

    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01PARENT"):
        agent()

    tape.install("fork", tape_dir=tape_dir, collect_git=False, replay="01PARENT",
                 fork_at=1, run_id="01FORK",
                 patches=parse_all(['mcp.read_file.result=["a", "b"]']))
    try:
        assert agent() == '["a", "b"]'
    finally:
        tape.uninstall()


@needs_sdk
def test_an_mcp_session_outside_a_tape_just_works(tape_dir):
    """A wrapped session has to be safe to leave in shipped code."""
    inner = FakeSession()

    async def go():
        session = tape.mcp.wrap(inner, server="files")
        return (await session.call_tool("read_file", {})).content[0].text

    assert asyncio.run(go()).startswith("read_file")
    assert len(inner.calls) == 1


@needs_sdk
def test_a_replaying_session_refuses_to_forward_anything_else(tape_dir):
    async def record():
        await tape.mcp.wrap(FakeSession(), server="files").initialize()

    _record(tape_dir, record)

    async def go():
        session = tape.mcp.connect
        return session

    replaying = mcp_mod.TapedSession(None, server="files")
    with pytest.raises(AttributeError, match="replaying from a tape"):
        replaying.read_resource


@needs_sdk
def test_a_replay_that_the_tape_declines_fails_rather_than_going_live():
    """A debugger that quietly does the real thing is a debugger that lies."""
    replaying = mcp_mod.TapedSession(None, server="files")
    with pytest.raises(TapeError, match="never quietly do the real thing"):
        asyncio.run(replaying.initialize())


@needs_sdk
def test_a_recorded_result_that_no_longer_validates_says_so():
    with pytest.raises(TapeError, match="no longer validates"):
        mcp_mod._from_wire("CallToolResult", {"content": "not a list"})


# -- the transports, end to end ------------------------------------------


def _tape(tape_dir, *command, **env_extra):
    env = dict(os.environ, **env_extra)
    return subprocess.run(
        [sys.executable, "-m", "reeltime.cli", "--tape-dir", str(tape_dir)] + list(command),
        env=env, capture_output=True, text=True, timeout=180,
    )


@needs_sdk
def test_replay_does_not_start_the_stdio_server(tmp_path):
    """The claim of this milestone, tested the only way it can be.

    A fake session cannot show this: the point is that no subprocess exists.
    The mock server appends a line to a log file when it starts, so the file's
    length is a direct count of how many times the server ran.
    """
    tape_dir = tmp_path / ".tape"
    spawn_log = tmp_path / "spawn.log"
    env = {"MCP_EXAMPLE_SPAWN_LOG": str(spawn_log)}

    recorded = _tape(tape_dir, "run", sys.executable, str(EXAMPLES / "mcp_agent.py"), **env)
    assert recorded.returncode == 0, recorded.stderr
    assert spawn_log.read_text().count("started") == 1

    replayed = _tape(tape_dir, "replay", "last", **env)
    assert replayed.returncode == 0, replayed.stderr
    assert spawn_log.read_text().count("started") == 1, (
        "replay started the MCP server; it must serve every call from the tape"
    )
    # And it replayed the run rather than doing nothing at all.
    assert "tools offered: list_files, read_file" in replayed.stdout
    assert "notes.txt: buy milk / call the bank" in replayed.stdout


@needs_sdk
def test_the_stdio_example_records_every_boundary_as_mcp(tmp_path):
    tape_dir = tmp_path / ".tape"
    result = _tape(tape_dir, "run", sys.executable, str(EXAMPLES / "mcp_agent.py"))
    assert result.returncode == 0, result.stderr

    trace = _only_trace(tape_dir)
    assert {e.kind for e in trace.events} == {"mcp"}
    assert [e.req["op"] for e in trace.events] == ["initialize", "list", "call", "call", "call"]
    assert trace.events[1].res["tools"] == ["list_files", "read_file"]
    assert trace.events[-1].res["is_error"] is True


@needs_sdk
def test_a_changed_tool_set_shows_up_in_the_diff_of_two_real_runs(tmp_path):
    tape_dir = tmp_path / ".tape"
    agent = str(EXAMPLES / "mcp_agent.py")
    assert _tape(tape_dir, "run", sys.executable, agent).returncode == 0
    assert _tape(tape_dir, "run", sys.executable, agent,
                 MCP_EXAMPLE_TOOLS="extended").returncode == 0

    from reeltime.core import paths

    a, b = sorted(paths.list_run_ids(tape_dir))
    result = _tape(tape_dir, "diff", a, b)
    assert result.returncode == 0, result.stderr
    assert "tool set changed" in result.stdout
    assert "+ delete_file" in result.stdout


@needs_sdk
def test_the_http_transport_records_and_replays(tmp_path, http_mcp_server):
    """The other transport, over a real socket.

    It also proves the boundary rule holds for MCP: the streamable-HTTP client
    runs on httpx, which reeltime already intercepts, so without the outermost
    boundary winning, every call here would be recorded twice.
    """
    tape_dir = tmp_path / ".tape"

    async def go():
        async with tape.mcp.connect(url=http_mcp_server, server="web") as session:
            listing = await session.list_tools()
            result = await session.call_tool("echo", {"text": "hi"})
            return [t.name for t in listing.tools], result.content[0].text

    recorded = _record(tape_dir, go)
    assert recorded == (["echo"], "hi")

    trace = tape.read_trace(tape_dir / "runs" / "01REC.jsonl")
    assert {e.kind for e in trace.events} == {"mcp"}, (
        "the transport's own HTTP request was recorded as a second event")

    replayed, _ = _replay(tape_dir, go)
    assert replayed == recorded


def _echo_agent(url):
    """One call site for both halves, so the matcher is testing the transport.

    Two separately-defined coroutines would sit on different lines, and the
    replay would miss on call site rather than on anything to do with MCP.
    """
    async def go():
        async with tape.mcp.connect(url=url, server="web") as session:
            return (await session.call_tool("echo", {"text": "hi"})).content[0].text

    return go


@needs_sdk
def test_the_http_transport_does_not_contact_the_server_on_replay(tmp_path,
                                                                 http_mcp_server):
    tape_dir = tmp_path / ".tape"
    assert _record(tape_dir, _echo_agent(http_mcp_server)) == "hi"

    # Point the replay at a port with nothing behind it. If the session were to
    # connect, this would fail rather than pass.
    dead = "http://127.0.0.1:{}/mcp".format(_free_port())
    assert _replay(tape_dir, _echo_agent(dead))[0] == "hi"


def _stdio_agent(spawn_log=None, cwd=None):
    """One call site shared by record and replay -- see _echo_agent."""
    env = dict(os.environ)
    if spawn_log is not None:
        env["MCP_EXAMPLE_SPAWN_LOG"] = str(spawn_log)

    async def go():
        async with tape.mcp.connect(sys.executable, [str(EXAMPLES / "mcp_server.py")],
                                    server="files", env=env, cwd=cwd) as session:
            listing = await session.list_tools()
            result = await session.call_tool("read_file", {"path": "notes.txt"})
            return [t.name for t in listing.tools], result.content[0].text

    return go


@needs_sdk
def test_the_stdio_transport_records_and_replays_in_process(tmp_path, tape_dir):
    """The same claim as the subprocess test, close enough to see the machinery.

    The subprocess test proves it for a user running `tape replay`; this one
    runs the transport in the test process, so the connect path itself is
    covered rather than merely driven.
    """
    spawn_log = tmp_path / "spawn.log"

    recorded = _record(tape_dir, _stdio_agent(spawn_log, cwd=tmp_path))
    assert recorded[0] == ["list_files", "read_file"]
    assert recorded[1].strip() == "buy milk\ncall the bank"
    assert spawn_log.read_text().count("started") == 1

    replayed, run = _replay(tape_dir, _stdio_agent(spawn_log, cwd=tmp_path))
    assert replayed == recorded
    assert spawn_log.read_text().count("started") == 1, "replay started the server"
    assert run.summary.events == 3


@needs_sdk
def test_a_replaying_stdio_session_is_not_live(tape_dir, tmp_path):
    _record(tape_dir, _stdio_agent(tmp_path / "spawn.log"))

    seen = {}

    async def inspect():
        # initialize=False: this is asking what the session *is*, not replaying
        # a handshake, and consuming one here would match on this line rather
        # than on the line the recording was made from.
        async with tape.mcp.connect(sys.executable, ["nonexistent-server.py"],
                                    server="files", initialize=False) as session:
            seen["live"] = session.live
            seen["transport"] = session.transport

    with tape.session("replay", tape_dir=tape_dir, replay="01REC", collect_git=False):
        asyncio.run(inspect())

    assert seen == {"live": False, "transport": "stdio"}


@needs_sdk
def test_the_sse_transport_records_and_replays(tmp_path, sse_mcp_server):
    """SSE is a second HTTP transport with its own client, not a flag on one."""
    tape_dir = tmp_path / ".tape"

    def agent(url):
        async def go():
            async with tape.mcp.connect(url=url, transport="sse", server="web") as s:
                return (await s.call_tool("echo", {"text": "hi"})).content[0].text

        return go

    assert _record(tape_dir, agent(sse_mcp_server)) == "hi"
    trace = tape.read_trace(tape_dir / "runs" / "01REC.jsonl")
    assert {e.kind for e in trace.events} == {"mcp"}

    dead = "http://127.0.0.1:{}/sse".format(_free_port())
    assert _replay(tape_dir, agent(dead))[0] == "hi"


@needs_sdk
def test_connect_needs_exactly_one_transport():
    async def both():
        async with tape.mcp.connect("python", url="http://x/mcp"):
            pass

    with pytest.raises(TapeError, match="exactly one of"):
        asyncio.run(both())

    async def neither():
        async with tape.mcp.connect():
            pass

    with pytest.raises(TapeError, match="exactly one of"):
        asyncio.run(neither())


# -- fixtures and builders -----------------------------------------------


def _is_error(result):
    """`isError` in SDK 1.x, `is_error` in 2.x. The wire name never changed."""
    flag = getattr(result, "is_error", None)
    return bool(getattr(result, "isError", None) if flag is None else flag)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def http_mcp_server():
    """A real MCP server over streamable HTTP, in a background thread."""
    pytest.importorskip("uvicorn")
    port = _free_port()
    running, thread = _serve(_echo_server().streamable_http_app(), port)
    try:
        yield "http://127.0.0.1:{}/mcp".format(port)
    finally:
        running.should_exit = True
        thread.join(timeout=10)


def _only_trace(tape_dir):
    from reeltime.core import paths

    run_ids = paths.list_run_ids(tape_dir)
    assert len(run_ids) == 1, run_ids
    return tape.read_trace(paths.trace_path(tape_dir, run_ids[0]))


def _serve(app, port):
    """Run a Starlette app on a background thread until the test is done."""
    uvicorn = pytest.importorskip("uvicorn")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    running = uvicorn.Server(config)
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not running.started and time.time() < deadline:
        time.sleep(0.05)
    if not running.started:  # pragma: no cover - a wedged fixture
        pytest.fail("the MCP HTTP fixture never came up")
    return running, thread


def _echo_server():
    from mcp.server import MCPServer

    server = MCPServer("http-fixture", version="1.0.0")

    @server.tool()
    def echo(text: str) -> str:
        """Echo the text back."""
        return text

    return server


@pytest.fixture
def sse_mcp_server():
    """A real MCP server over SSE."""
    pytest.importorskip("uvicorn")
    port = _free_port()
    running, thread = _serve(_echo_server().sse_app(), port)
    try:
        yield "http://127.0.0.1:{}/sse".format(port)
    finally:
        running.should_exit = True
        thread.join(timeout=10)


def _trace(events, run_id="01A"):
    from reeltime.core.trace import Header, Trace

    return Trace(
        header=Header(run_id=run_id, started="", argv=["agent.py"], cwd="", python=""),
        events=list(events),
        footer={"events": len(events), "cost_usd": 0.0, "tokens": {"in": 0, "out": 0}},
    )


def _http_event(index, site="agent.py:1"):
    return Event(i=index, kind="http", site=site,
                 req={"method": "POST", "url": "http://x/mcp", "body": {}},
                 res={"status": 200})


def _mcp_list(index, tools, site="agent.py:1", server="files", definitions=None):
    return Event(
        i=index, kind="mcp", site=site,
        req=mcp_mod.list_request(server),
        res={"tools": list(tools), "count": len(tools),
             "result": definitions if definitions is not None
             else {"tools": [{"name": n} for n in tools]}},
    )


def _mcp_call(index, name, site="agent.py:1", server="files", args=None,
              is_error=False, value="ok"):
    return Event(
        i=index, kind="mcp", site=site,
        req=mcp_mod.call_request(server, name, args or {}),
        res={"value": value, "is_error": is_error,
             "result": {"content": [{"type": "text", "text": value}]}},
    )
