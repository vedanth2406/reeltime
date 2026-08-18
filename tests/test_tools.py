import asyncio
import random

import httpx
import pytest

import reeltime as tape


def events(run):
    return tape.read_trace(run.path).events


def test_a_tool_records_its_arguments_and_result(recording):
    @tape.tool
    def read_file(path: str, encoding: str = "utf-8") -> str:
        return "contents of " + path

    assert read_file("notes.md") == "contents of notes.md"
    tape.uninstall()

    event = events(recording)[0]
    assert event.kind == "tool"
    assert event.req == {"name": "read_file", "args": {"path": "notes.md",
                                                       "encoding": "utf-8"}}
    assert event.res == {"value": "contents of notes.md"}
    assert event.dur_ms >= 0


def test_positional_and_keyword_calls_record_identically(recording):
    @tape.tool
    def add(a, b):
        return a + b

    add(1, 2)
    add(a=1, b=2)
    add(1, b=2)
    tape.uninstall()

    # The M3 matcher compares recorded requests; three spellings of the same
    # call have to produce the same one.
    assert {str(e.req["args"]) for e in events(recording)} == {"{'a': 1, 'b': 2}"}


def test_an_async_tool_is_recorded(recording):
    @tape.tool
    async def fetch(key: str) -> str:
        await asyncio.sleep(0)
        return "value:" + key

    assert asyncio.run(fetch("k")) == "value:k"
    tape.uninstall()

    event = events(recording)[0]
    assert event.req == {"name": "fetch", "args": {"key": "k"}}
    assert event.res == {"value": "value:k"}


def test_a_custom_name_can_be_given(recording):
    @tape.tool(name="search_web")
    def search(q):
        return []

    search("cats")
    tape.uninstall()
    assert events(recording)[0].req["name"] == "search_web"


def test_wrap_handles_functions_you_do_not_own(recording):
    wrapped = tape.wrap(len, name="length")
    assert wrapped([1, 2, 3]) == 3
    tape.uninstall()

    event = events(recording)[0]
    assert event.req["name"] == "length"
    assert event.res == {"value": 3}


def test_wrap_all_wraps_a_registry(recording):
    tools = tape.wrap_all({"upper": str.upper, "strip": str.strip})
    assert tools["upper"]("hi") == "HI"
    assert tools["strip"]("  hi  ") == "hi"
    tape.uninstall()

    assert [e.req["name"] for e in events(recording)] == ["upper", "strip"]
    assert all(tape.is_wrapped(fn) for fn in tools.values())


def test_a_failing_tool_is_recorded_and_still_raises(recording):
    @tape.tool
    def read_file(path):
        raise FileNotFoundError(path)

    with pytest.raises(FileNotFoundError):
        read_file("missing.md")
    tape.uninstall()

    event = events(recording)[0]
    assert event.meta["error"] == {"type": "FileNotFoundError",
                                   "message": "missing.md"}
    assert event.res is None


def test_a_wrapped_tool_is_transparent_outside_a_session():
    @tape.tool
    def double(x):
        return x * 2

    assert double(21) == 42          # no tape installed at all
    assert double.__name__ == "double"


def test_the_call_site_is_the_caller_not_the_wrapper(recording):
    import inspect

    @tape.tool
    def noop():
        return None

    expected = inspect.currentframe().f_lineno + 1
    noop()
    tape.uninstall()
    assert events(recording)[0].site.endswith("test_tools.py:{}".format(expected))


# -- nesting: the outermost boundary is the one recorded -----------------


def test_an_http_call_inside_a_tool_does_not_double_record(recording, server):
    url = server.route("/api", json={"ok": True})

    @tape.tool
    def search(q):
        return httpx.post(url, json={"q": q}).json()

    assert search("cats") == {"ok": True}
    tape.uninstall()

    # One boundary crossed, one event. On replay the tool result is served and
    # this body never runs, so a nested http event could never be matched.
    recorded = events(recording)
    assert len(recorded) == 1
    assert recorded[0].kind == "tool"


def test_a_tool_inside_a_tool_records_only_the_outer_one(recording):
    @tape.tool
    def inner(x):
        return x + 1

    @tape.tool
    def outer(x):
        return inner(x) * 2

    assert outer(1) == 4
    tape.uninstall()

    recorded = events(recording)
    assert [e.req["name"] for e in recorded] == ["outer"]
    assert recorded[0].res == {"value": 4}


def test_ambient_reads_inside_a_tool_are_not_recorded(recording):
    @tape.tool
    def roll():
        return random.random()

    roll()
    tape.uninstall()

    recorded = events(recording)
    assert [e.kind for e in recorded] == ["tool"]


def test_the_nesting_guard_is_released_after_the_tool_returns(recording, server):
    url = server.route("/api", json={"ok": True})

    @tape.tool
    def step():
        return httpx.post(url, json={}).status_code

    step()
    httpx.post(url, json={})   # back outside: recorded again
    tape.uninstall()

    assert [e.kind for e in events(recording)] == ["tool", "http"]


def test_the_guard_is_released_when_a_tool_raises(recording, server):
    url = server.route("/api", json={"ok": True})

    @tape.tool
    def boom():
        raise ValueError("no")

    with pytest.raises(ValueError):
        boom()
    httpx.post(url, json={})
    tape.uninstall()

    assert [e.kind for e in events(recording)] == ["tool", "http"]
