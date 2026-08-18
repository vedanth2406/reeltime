"""Every patch field the grammar accepts, proved to actually do something.

`tool.args` was declared in `REQUEST_FIELDS` for two releases with no code
anywhere that read it. It parsed, `check_patches` accepted it, the fork ran and
reported it as applied in the footer, and the tool was called with the original
arguments. That is worse than rejecting it: a patch that silently does nothing
sends you looking for the bug in your agent.

So this file is a table rather than a list of hand-written tests. Each case
forks a real run with one patch and asserts an effect that is only observable
if the patch reached the boundary. `test_every_declared_field_has_a_case` then
requires a case for every `(kind, field)` the grammar accepts, so adding a
field without wiring it up fails here rather than in someone's afternoon.
"""

import asyncio
import json

import httpx
import pytest

import reeltime as tape
from reeltime.core import mcp as mcp_mod
from reeltime.core.patch import declared_fields, parse_all
from reeltime.errors import TapeConfigError

try:
    import mcp as mcp_sdk
    import mcp.types as mcp_types
except ImportError:  # the SDK requires Python 3.10
    mcp_sdk = mcp_types = None


# -- the machinery each case runs on -------------------------------------


def _fork(tape_dir, fn, patches, at=0, parent="01PARENT", run_id="01FORK"):
    run = tape.install("fork", tape_dir=tape_dir, collect_git=False, replay=parent,
                       fork_at=at, run_id=run_id, patches=parse_all(list(patches)))
    try:
        return fn(), run
    finally:
        if not run.closed:
            tape.uninstall()


def _record(tape_dir, fn, run_id="01PARENT"):
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id=run_id):
        return fn()


@pytest.fixture
def llm_server(server):
    """A server that echoes the request back, so a rewrite is observable."""
    server.route("/v1/chat/completions", json={
        "object": "chat.completion", "model": "gpt-4o-mini",
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    })
    server.route("/v2/chat/completions", json={
        "object": "chat.completion", "model": "gpt-4o-mini",
        "choices": [{"message": {"content": "second endpoint"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    })
    return server


def _llm_agent(url):
    def go():
        response = httpx.post(url, json={
            "model": "gpt-4o-mini",
            "temperature": 0.7,
            "messages": [{"role": "system", "content": "Be brief."},
                         {"role": "user", "content": "hi"}],
        })
        return response.json()

    return go


# -- llm ------------------------------------------------------------------


def _sent(server):
    """The last request body the server actually received."""
    return json.loads(server.received[-1]["body"])


def test_llm_model_reaches_the_wire(tape_dir, llm_server):
    url = llm_server.base_url + "/v1/chat/completions"
    _record(tape_dir, _llm_agent(url))
    _fork(tape_dir, _llm_agent(url), ["llm.model=gpt-4o"])
    assert _sent(llm_server)["model"] == "gpt-4o"


def test_llm_system_reaches_the_wire(tape_dir, llm_server):
    url = llm_server.base_url + "/v1/chat/completions"
    _record(tape_dir, _llm_agent(url))
    _fork(tape_dir, _llm_agent(url), ['llm.system+="Ask first."'])
    assert _sent(llm_server)["messages"][0]["content"] == "Be brief. Ask first."


@pytest.mark.parametrize("field,value", [
    ("temperature", 0.0), ("top_p", 0.5), ("max_tokens", 32), ("seed", 7),
])
def test_llm_request_parameters_reach_the_wire(tape_dir, llm_server, field, value):
    url = llm_server.base_url + "/v1/chat/completions"
    _record(tape_dir, _llm_agent(url))
    _fork(tape_dir, _llm_agent(url), ["llm.{}={}".format(field, value)])
    assert _sent(llm_server)[field] == value


def test_llm_response_substitutes_without_a_call(tape_dir, llm_server):
    url = llm_server.base_url + "/v1/chat/completions"
    _record(tape_dir, _llm_agent(url))
    before = len(llm_server.received)

    result, _ = _fork(tape_dir, _llm_agent(url), ['llm.response="patched answer"'])
    assert result["choices"][0]["message"]["content"] == "patched answer"
    assert len(llm_server.received) == before, "a substituted result still called out"


# -- http -----------------------------------------------------------------


def test_http_url_rewrites_the_request_url(tape_dir, llm_server):
    """The regression this file exists for: this used to write a `url` key
    into the JSON body and leave the request pointed where it was."""
    url = llm_server.base_url + "/v1/chat/completions"
    _record(tape_dir, _llm_agent(url))

    target = llm_server.base_url + "/v2/chat/completions"
    result, _ = _fork(tape_dir, _llm_agent(url), ["http.url=" + target])

    assert result["choices"][0]["message"]["content"] == "second endpoint"
    assert llm_server.received[-1]["path"] == "/v2/chat/completions"
    assert "url" not in _sent(llm_server), "the URL was written into the body"


def test_http_body_replaces_the_whole_request_body(tape_dir, llm_server):
    url = llm_server.base_url + "/v1/chat/completions"
    _record(tape_dir, _llm_agent(url))
    _fork(tape_dir, _llm_agent(url),
          ['http.body={"model": "gpt-4o-mini", "messages": [], "replaced": true}'])
    assert _sent(llm_server)["replaced"] is True


def test_http_body_response_substitutes_without_a_call(tape_dir, llm_server):
    url = llm_server.base_url + "/v1/chat/completions"
    _record(tape_dir, _llm_agent(url))
    before = len(llm_server.received)

    result, _ = _fork(tape_dir, _llm_agent(url), ['http.body_response="from the patch"'])
    assert result["choices"][0]["message"]["content"] == "from the patch"
    assert len(llm_server.received) == before


# -- tool -----------------------------------------------------------------


def _tool_agent(seen):
    @tape.tool
    def read_file(path, encoding="utf-8"):
        seen.append((path, encoding))
        return "contents of " + path

    def go():
        return read_file("a.txt")

    return go


def test_tool_args_reaches_the_tool(tape_dir):
    """The field that was declared and never read."""
    seen = []
    _record(tape_dir, _tool_agent(seen))
    assert seen == [("a.txt", "utf-8")]

    result, _ = _fork(tape_dir, _tool_agent(seen),
                      ['tool.read_file.args={"path": "b.txt", "encoding": "utf-8"}'])
    assert seen[-1] == ("b.txt", "utf-8"), "the tool ran with the original arguments"
    assert result == "contents of b.txt"


def test_a_patched_tool_call_is_recorded_as_the_call_that_happened(tape_dir):
    seen = []
    _record(tape_dir, _tool_agent(seen))
    _fork(tape_dir, _tool_agent(seen),
          ['tool.read_file.args={"path": "b.txt", "encoding": "utf-8"}'])

    event = tape.read_trace(tape_dir / "runs" / "01FORK.jsonl").events[-1]
    assert event.req["args"] == {"path": "b.txt", "encoding": "utf-8"}


def test_tool_args_is_reported_in_the_fork_footer(tape_dir):
    seen = []
    _record(tape_dir, _tool_agent(seen))
    _, run = _fork(tape_dir, _tool_agent(seen),
                   ['tool.read_file.args={"path": "b.txt"}'])
    tape.uninstall()
    footer = tape.read_trace(tape_dir / "runs" / "01FORK.jsonl").footer
    assert any("args" in line for line in footer["patched"])


def test_tool_args_on_an_unbindable_callable_says_so_rather_than_lying(tape_dir):
    """A patch that cannot be honoured must fail, not quietly not happen."""
    seen = []

    def agent():
        wrapped = tape.wrap(max, name="max")
        return wrapped(*seen) if seen else wrapped(3, 1)

    _record(tape_dir, agent)
    with pytest.raises(TapeConfigError, match="could not be bound"):
        _fork(tape_dir, agent, ['tool.max.args={"a": 1}'])
    tape.uninstall()


def test_tool_result_substitutes_without_running_the_body(tape_dir):
    seen = []
    _record(tape_dir, _tool_agent(seen))
    calls = len(seen)

    result, _ = _fork(tape_dir, _tool_agent(seen), ['tool.read_file.result="<empty>"'])
    assert result == "<empty>"
    assert len(seen) == calls, "the tool body ran despite a substituted result"


# -- mcp ------------------------------------------------------------------


needs_sdk = pytest.mark.skipif(mcp_sdk is None, reason="the MCP SDK needs Python 3.10+")


class _Session:
    def __init__(self, seen):
        self.seen = seen

    async def initialize(self):
        return mcp_types.InitializeResult.model_validate({
            "protocolVersion": "2025-11-25", "capabilities": {},
            "serverInfo": {"name": "fake", "version": "1.0.0"}})

    async def call_tool(self, name, arguments=None, *args, **kwargs):
        self.seen.append((name, arguments))
        return mcp_types.CallToolResult.model_validate(
            {"content": [{"type": "text", "text": json.dumps(arguments)}]})


def _mcp_agent(seen):
    def go():
        async def inner():
            session = tape.mcp.wrap(_Session(seen), server="files")
            await session.initialize()
            result = await session.call_tool("read_file", {"path": "a.txt"})
            return result.content[0].text

        return asyncio.run(inner())

    return go


@needs_sdk
def test_mcp_args_reaches_the_server(tape_dir):
    seen = []
    _record(tape_dir, _mcp_agent(seen))
    assert seen == [("read_file", {"path": "a.txt"})]

    result, _ = _fork(tape_dir, _mcp_agent(seen),
                      ['mcp.read_file.args={"path": "b.txt"}'], at=1)
    assert seen[-1] == ("read_file", {"path": "b.txt"})
    assert json.loads(result) == {"path": "b.txt"}


@needs_sdk
def test_a_patched_mcp_call_is_recorded_as_the_call_that_happened(tape_dir):
    seen = []
    _record(tape_dir, _mcp_agent(seen))
    _fork(tape_dir, _mcp_agent(seen), ['mcp.read_file.args={"path": "b.txt"}'], at=1)

    event = tape.read_trace(tape_dir / "runs" / "01FORK.jsonl").events[-1]
    assert event.req["args"] == {"path": "b.txt"}


@needs_sdk
def test_mcp_result_substitutes_without_calling_the_server(tape_dir):
    seen = []
    _record(tape_dir, _mcp_agent(seen))
    calls = len(seen)

    result, _ = _fork(tape_dir, _mcp_agent(seen),
                      ['mcp.read_file.result="<empty>"'], at=1)
    assert result == "<empty>"
    assert len(seen) == calls


# -- the guard that keeps this file honest --------------------------------

#: `(kind, field)` -> the test above that proves it reaches its boundary.
COVERED = {
    ("llm", "model"): "test_llm_model_reaches_the_wire",
    ("llm", "system"): "test_llm_system_reaches_the_wire",
    ("llm", "temperature"): "test_llm_request_parameters_reach_the_wire",
    ("llm", "top_p"): "test_llm_request_parameters_reach_the_wire",
    ("llm", "max_tokens"): "test_llm_request_parameters_reach_the_wire",
    ("llm", "seed"): "test_llm_request_parameters_reach_the_wire",
    ("llm", "response"): "test_llm_response_substitutes_without_a_call",
    ("tool", "args"): "test_tool_args_reaches_the_tool",
    ("tool", "result"): "test_tool_result_substitutes_without_running_the_body",
    ("mcp", "args"): "test_mcp_args_reaches_the_server",
    ("mcp", "result"): "test_mcp_result_substitutes_without_calling_the_server",
    ("http", "url"): "test_http_url_rewrites_the_request_url",
    ("http", "body"): "test_http_body_replaces_the_whole_request_body",
    ("http", "body_response"): "test_http_body_response_substitutes_without_a_call",
}


def test_every_declared_field_has_a_case():
    """Adding a field to the grammar without wiring it up fails here."""
    declared = set(declared_fields())
    assert declared == set(COVERED), {
        "declared but unproven": sorted(declared - set(COVERED)),
        "proven but no longer declared": sorted(set(COVERED) - declared),
    }


def test_every_named_case_exists():
    """A rename that orphans an entry above would otherwise pass silently."""
    import sys

    module = sys.modules[__name__]
    missing = sorted({name for name in COVERED.values()
                      if not hasattr(module, name)})
    assert not missing, missing


def test_every_declared_field_is_documented():
    """Declared, applied, and undocumented is the other half of the problem."""
    from pathlib import Path

    from reeltime.core import patch as patch_mod

    docstring = patch_mod.__doc__ or ""
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    grammar = readme.split("### The patch grammar", 1)[-1].split("\n## ", 1)[0]

    for kind, field in declared_fields():
        assert field in docstring, "{}.{} is not in the patch.py table".format(kind, field)
        assert field in grammar, "{}.{} is not in the README table".format(kind, field)


# -- what the grammar now refuses -----------------------------------------


@pytest.mark.parametrize("expression", [
    'http.body+={"a": 1}',
    'tool.read_file.args~=/a/b/',
    'mcp.read_file.args+={"a": 1}',
])
def test_a_whole_document_field_refuses_an_operator_it_cannot_honour(expression):
    """These used to parse, run, and change nothing."""
    with pytest.raises(TapeConfigError, match="whole document"):
        parse_all([expression])


def test_an_undeclared_field_is_still_rejected():
    with pytest.raises(TapeConfigError, match="no field"):
        parse_all(["tool.read_file.arguments=1"])
