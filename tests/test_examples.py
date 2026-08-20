"""The examples, run for real as integration tests.

Each example is executed as a subprocess through `tape run`, then replayed
through `tape replay`, against a local server standing in for the provider. So
these cover the whole path a user takes -- CLI, sitecustomize injection, real
SDK, record, replay -- rather than any single layer.
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import reeltime as tape
from reeltime.core import paths
from reeltime.core.http import common as http_common

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

pytest.importorskip("openai")
pytest.importorskip("anthropic")
pytest.importorskip("boto3")

CHAT = {
    "object": "chat.completion",
    "model": "gpt-4o-mini",
    "choices": [{"message": {"content": "Jupiter."}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 42, "completion_tokens": 3},
}

CHAT_STREAM = [
    'data: {"id":"1","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{"content":"Jupiter"}}]}\n\n',
    'data: {"id":"1","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[{"index":0,"delta":{"content":" and Ganymede."},'
    '"finish_reason":"stop"}]}\n\n',
    'data: {"id":"1","object":"chat.completion.chunk","model":"gpt-4o-mini",'
    '"choices":[],"usage":{"prompt_tokens":50,"completion_tokens":4}}\n\n',
    "data: [DONE]\n\n",
]

TOOL_USE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-5",
    "content": [
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": "tu_1", "name": "get_weather",
         "input": {"city": "Austin"}},
    ],
    "stop_reason": "tool_use",
    "usage": {"input_tokens": 120, "output_tokens": 30},
}

FINAL_MESSAGE = {
    "id": "msg_2",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-5",
    "content": [{"type": "text", "text": "It is sunny and 21C in Austin."}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 200, "output_tokens": 12},
}


def run(command, tape_dir, cwd, **env_extra):
    env = dict(
        os.environ,
        OPENAI_API_KEY="sk-notreal-" + "0" * 24,
        ANTHROPIC_API_KEY="sk-ant-notreal-" + "0" * 24,
        **env_extra,
    )
    # `None` removes a variable rather than blanking it. Not the same thing to
    # everyone downstream: botocore reads `AWS_PROFILE=""` as a request for a
    # profile whose name is the empty string, and fails looking for it.
    env = {key: value for key, value in env.items() if value is not None}
    return subprocess.run(
        [sys.executable, "-m", "reeltime.cli", "--tape-dir", str(tape_dir)] + command,
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120,
    )


def only_run_id(tape_dir):
    run_ids = paths.list_run_ids(tape_dir)
    assert len(run_ids) == 1, run_ids
    return run_ids[0]


def trace_of(tape_dir):
    return tape.read_trace(paths.trace_path(tape_dir, only_run_id(tape_dir)))


# -- plain OpenAI SDK ----------------------------------------------------


def test_the_openai_example_records_and_replays(tmp_path, server):
    server.route("/v1/chat/completions", json=CHAT)
    tape_dir = tmp_path / ".tape"
    example = str(EXAMPLES / "openai_agent.py")
    env = {"OPENAI_BASE_URL": server.base_url + "/v1"}

    recorded = run(["run", sys.executable, example], tape_dir, tmp_path, **env)
    assert recorded.returncode == 0, recorded.stderr
    assert "turn 1: Jupiter." in recorded.stdout
    assert "recorded 3 events" in recorded.stderr

    trace = trace_of(tape_dir)
    assert [e.kind for e in trace.events] == ["llm"] * 3
    assert trace.events[0].req["model"] == "gpt-4o-mini"
    assert trace.footer["cost_usd"] > 0

    server.received.clear()
    replayed = run(["replay", only_run_id(tape_dir)], tape_dir, tmp_path, **env)
    assert replayed.returncode == 0, replayed.stderr
    assert replayed.stdout == recorded.stdout        # identical, from the tape
    assert "replayed 3 events" in replayed.stderr
    assert server.received == []                     # and no network at all


def test_the_openai_examples_stream_replays_chunk_for_chunk(tmp_path, server):
    # The third turn streams; its chunk boundaries have to survive the round trip.
    # The route carries both shapes and the mock picks by request, as a real
    # provider would.
    server.route("/v1/chat/completions", json=CHAT, sse=CHAT_STREAM)
    tape_dir = tmp_path / ".tape"
    env = {"OPENAI_BASE_URL": server.base_url + "/v1"}

    recorded = run(["run", sys.executable, str(EXAMPLES / "openai_agent.py")],
                   tape_dir, tmp_path, **env)
    assert recorded.returncode == 0, recorded.stderr
    assert "turn 3 (streamed): Jupiter and Ganymede." in recorded.stdout

    streamed = [e for e in trace_of(tape_dir).events if "stream" in (e.res or {})]
    assert streamed and streamed[-1].res["stream"]["chunks"] == CHAT_STREAM

    replayed = run(["replay", only_run_id(tape_dir)], tape_dir, tmp_path, **env)
    assert replayed.stdout == recorded.stdout


def test_context_shows_the_openai_history_growing(tmp_path, server):
    server.route("/v1/chat/completions", json=CHAT)
    tape_dir = tmp_path / ".tape"
    run(["run", sys.executable, str(EXAMPLES / "openai_agent.py")], tape_dir, tmp_path,
        OPENAI_BASE_URL=server.base_url + "/v1")
    run_id = only_run_id(tape_dir)

    shown = run(["show", run_id, "1", "--context"], tape_dir, tmp_path)
    assert shown.returncode == 0, shown.stderr
    assert "[0] system" in shown.stdout
    assert "Name one planet." in shown.stdout

    diffed = run(["show", run_id, "1", "--context", "--diff", "0"], tape_dir, tmp_path)
    assert "context diff" in diffed.stdout
    assert "INJECTED" in diffed.stdout           # turn 1's answer entered the history
    assert "+2 messages" in diffed.stdout


# -- plain Anthropic SDK -------------------------------------------------


def test_the_anthropic_example_records_and_replays(tmp_path, server):
    tape_dir = tmp_path / ".tape"
    env = {"ANTHROPIC_BASE_URL": server.base_url}

    # First call asks for the tool, second answers. One route, two shapes: the
    # server alternates so the example's two turns differ.
    server.route("/v1/messages", json=TOOL_USE)
    recorded = run(["run", sys.executable, str(EXAMPLES / "anthropic_agent.py")],
                   tape_dir, tmp_path, **env)
    assert recorded.returncode == 0, recorded.stderr
    assert "tool: get_weather" in recorded.stdout

    trace = trace_of(tape_dir)
    assert all(e.kind == "llm" for e in trace.events)
    assert trace.events[0].req["provider"] == "anthropic"
    assert trace.events[0].req["has_system"] is True

    server.received.clear()
    replayed = run(["replay", only_run_id(tape_dir)], tape_dir, tmp_path, **env)
    assert replayed.returncode == 0, replayed.stderr
    assert replayed.stdout == recorded.stdout
    assert server.received == []


def test_context_hoists_the_anthropic_system_prompt(tmp_path, server):
    server.route("/v1/messages", json=FINAL_MESSAGE)
    tape_dir = tmp_path / ".tape"
    run(["run", sys.executable, str(EXAMPLES / "anthropic_agent.py")], tape_dir,
        tmp_path, ANTHROPIC_BASE_URL=server.base_url)

    shown = run(["show", only_run_id(tape_dir), "0", "--context"], tape_dir, tmp_path)
    assert shown.returncode == 0, shown.stderr
    # The system prompt travels outside the array; the context view puts it where
    # a reader expects it.
    assert "[0] system" in shown.stdout
    assert "hoisted from `system`" in shown.stdout
    assert "Prefer using a tool" in shown.stdout
    assert "tools: get_weather" in shown.stdout


# -- multi-tool agent ----------------------------------------------------


def test_the_multi_tool_example_does_not_delete_twice(tmp_path, server):
    """The whole reason a replayed tool body must not run."""
    server.route("/v1/chat/completions", json=CHAT)
    tape_dir = tmp_path / ".tape"
    env = {"OPENAI_BASE_URL": server.base_url + "/v1"}

    recorded = run(["run", sys.executable, str(EXAMPLES / "multi_tool_agent.py")],
                   tape_dir, tmp_path, **env)
    assert recorded.returncode == 0, recorded.stderr
    assert "deleted temp.log" in recorded.stdout
    assert "remaining: ['keep.txt']" in recorded.stdout

    trace = trace_of(tape_dir)
    kinds = [e.kind for e in trace.events]
    assert "tool" in kinds and "llm" in kinds and "rand" in kinds
    # Spans group the phases, so concurrent work would replay order-independently.
    assert {e.span for e in trace.events} >= {"root/survey", "root/plan", "root/act"}

    # The replay runs in a fresh process with a fresh scratch directory, so the
    # file it "deleted" never existed there. It still reports the same thing,
    # because the result came from the tape and the body did not run.
    server.received.clear()
    replayed = run(["replay", only_run_id(tape_dir)], tape_dir, tmp_path, **env)
    assert replayed.returncode == 0, replayed.stderr
    assert replayed.stdout == recorded.stdout
    assert server.received == []


def test_the_multi_tool_example_replays_its_random_choice(tmp_path, server):
    server.route("/v1/chat/completions", json=CHAT)
    tape_dir = tmp_path / ".tape"
    env = {"OPENAI_BASE_URL": server.base_url + "/v1"}

    recorded = run(["run", sys.executable, str(EXAMPLES / "multi_tool_agent.py")],
                   tape_dir, tmp_path, **env)
    strategy = [line for line in recorded.stdout.splitlines()
                if line.startswith("strategy:")][0]

    replayed = run(["replay", only_run_id(tape_dir)], tape_dir, tmp_path, **env)
    assert strategy in replayed.stdout


# -- Bedrock over boto3, which embeds its own endpoint -------------------


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


#: The example starts its own mock Bedrock, so unlike every example above it
#: needs no `server` fixture -- but it does need a port nothing else is on.
#: The default 8424 is fine for a person running it by hand and not for a
#: suite that may share a machine with one.
SERVER_LINES = """    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Bedrock)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
"""

NO_SERVER_LINES = """    httpd = None
    # The endpoint is gone. A replay that reaches the network cannot succeed
    # from here, which is the point -- and the line count is unchanged.
"""


def test_the_bedrock_example_records_and_replays(tmp_path):
    """boto3 through urllib3, end to end, including the binary event stream.

    The milestone in one test: before this shim existed the run below recorded
    *nothing at all* -- no event, no error, and a replay that went silently to
    the real API.
    """
    tape_dir = tmp_path / ".tape"
    example = tmp_path / "bedrock_agent.py"
    source = (EXAMPLES / "bedrock_agent.py").read_text()
    example.write_text(source)
    env = {"BEDROCK_EXAMPLE_PORT": str(free_port())}

    recorded = run(["run", sys.executable, str(example)], tape_dir, tmp_path, **env)
    assert recorded.returncode == 0, recorded.stderr
    assert "invoke_model: A tape you can rewind." in recorded.stdout
    assert "streamed:     A tape you can rewind." in recorded.stdout
    assert "recorded 2 events" in recorded.stderr

    trace = trace_of(tape_dir)
    assert [e.kind for e in trace.events] == ["llm", "llm"]
    assert all(e.req["provider"] == "bedrock" for e in trace.events)
    assert all(e.res["tokens"] == {"in": 137, "out": 6} for e in trace.events)
    # Tokens populate and the cost stays null: Claude-on-Bedrock is served
    # through cross-region inference profiles whose rate depends on the routing
    # tier, and the request does not say which tier answered. `pricing.py`
    # carries no row for it on purpose, and a missing number beats a wrong one.
    assert trace.footer["cost_usd"] == 0
    assert all("cost_usd" not in e.meta for e in trace.events)

    # The second call streams `application/vnd.amazon.eventstream`, and its six
    # frames are recorded as six chunks rather than one coalesced blob.
    streamed = trace.events[1]
    assert streamed.req["streamed"] is True
    assert len(http_common.decode_chunks(streamed.res["stream"])) == 6

    # Now take the endpoint away entirely and replay against the tape. Editing
    # the source rather than blocking a port is what makes this conclusive: if
    # anything reached the network, there is nothing there to answer it.
    without_server = source.replace(SERVER_LINES, NO_SERVER_LINES).replace(
        "    httpd.shutdown()\n", "    pass  # no server to shut down\n")
    assert without_server != source, "the example's server block moved"
    # Line for line, so every call site keeps the number it was recorded at.
    assert len(without_server.splitlines()) == len(source.splitlines())
    example.write_text(without_server)

    replayed = run(["replay", only_run_id(tape_dir)], tape_dir, tmp_path, **env)
    assert replayed.returncode == 0, replayed.stderr
    assert replayed.stdout == recorded.stdout        # identical, from the tape
    assert "replayed 2 events" in replayed.stderr


def test_the_bedrock_example_needs_no_aws_account(tmp_path):
    """No credentials in the environment, and no config file to find.

    The example passes obviously fake keys of its own because botocore signs
    before it sends. What this pins is that nothing *else* is required: a
    reader who copies it onto a laptop that has never seen `aws configure`
    gets the same run.
    """
    tape_dir = tmp_path / ".tape"
    env = {"BEDROCK_EXAMPLE_PORT": str(free_port()),
           "AWS_CONFIG_FILE": str(tmp_path / "no-such-config"),
           "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "no-such-credentials"),
           "AWS_EC2_METADATA_DISABLED": "true"}
    scrubbed = {key: None for key in
                ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                 "AWS_PROFILE", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE")}

    result = run(["run", sys.executable, str(EXAMPLES / "bedrock_agent.py")],
                 tape_dir, tmp_path, **dict(scrubbed, **env))
    assert result.returncode == 0, result.stderr
    assert "recorded 2 events" in result.stderr


# -- the examples stand alone -------------------------------------------


#: Read off the directory rather than listed by hand, so an example added
#: later is covered by default instead of by remembering.
ALL_EXAMPLES = sorted(path.name for path in EXAMPLES.glob("*.py"))


def test_the_example_list_is_not_empty():
    """A glob that stops matching would turn the test below into a no-op."""
    assert len(ALL_EXAMPLES) >= 6, ALL_EXAMPLES


@pytest.mark.parametrize("name", ALL_EXAMPLES)
def test_every_example_compiles(name):
    subprocess.run([sys.executable, "-m", "py_compile", str(EXAMPLES / name)],
                   check=True)


@pytest.mark.parametrize("name", ["openai_agent.py", "anthropic_agent.py"])
def test_the_sdk_examples_do_not_import_reeltime(name):
    # Zero-edit adoption is the first design principle and these two files are
    # the proof, so a stray import would quietly undermine the claim. Checked
    # against the parse tree, not the text, so the docstring may say the word.
    import ast

    tree = ast.parse((EXAMPLES / name).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "reeltime" not in imported
