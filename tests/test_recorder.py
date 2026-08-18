import inspect

import pytest

import reeltime as tape
from reeltime.core import recorder as recorder_mod


def test_events_are_numbered_from_zero(recording):
    for n in range(3):
        tape.record_event("tool", {"name": "t{}".format(n)})
    tape.uninstall()
    events = tape.read_trace(recording.path).events
    assert [e.i for e in events] == [0, 1, 2]


def test_events_carry_the_call_site_and_span(recording):
    with tape.span("plan"):
        expected_line = inspect.currentframe().f_lineno + 1
        tape.record_event("tool", {"name": "read_file"})
    tape.uninstall()

    event = tape.read_trace(recording.path).events[0]
    assert event.site.endswith("test_recorder.py:{}".format(expected_line))
    assert event.qual.endswith("::test_events_carry_the_call_site_and_span")
    assert event.span == "root/plan"


def test_arbitrary_python_in_a_payload_does_not_break_recording(recording):
    class Opaque:
        pass

    tape.record_event("tool", {"name": "t", "args": {"obj": Opaque(), "b": b"x"}})
    tape.uninstall()
    event = tape.read_trace(recording.path).events[0]
    assert event.req["args"]["obj"]["__type__"].endswith("Opaque")
    assert event.req["args"]["b"] == {"__bytes__": "eA==", "len": 1}


def test_large_fields_are_externalised_to_blobs(recording):
    messages = [{"role": "user", "content": "x" * 200} for _ in range(100)]
    tape.record_event("llm", {"model": "gpt-4o", "messages": messages})
    tape.uninstall()

    event = tape.read_trace(recording.path).events[0]
    assert event.req["model"] == "gpt-4o"
    assert event.req["messages"].startswith("blob:")
    assert recording.blobs.resolve(event.req)["messages"] == messages
    # The point of externalising: the JSONL line stays readable.
    assert len(recording.path.read_text().splitlines()[1]) < 1000


def test_capture_times_the_block_and_records_the_result(recording):
    expected_line = inspect.currentframe().f_lineno + 1
    with recording.recorder.capture("tool", {"name": "slow"}) as cap:
        sum(range(50_000))
        cap.res = {"value": "done"}
    tape.uninstall()

    event = tape.read_trace(recording.path).events[0]
    assert event.res == {"value": "done"}
    assert event.dur_ms > 0
    # The `with` line, not a frame inside contextlib or inside reeltime.
    assert event.site.endswith("test_recorder.py:{}".format(expected_line))


def test_a_failing_call_is_still_a_recorded_boundary(recording):
    with pytest.raises(ValueError):
        with recording.recorder.capture("tool", {"name": "boom"}):
            raise ValueError("no such file")
    tape.uninstall()

    event = tape.read_trace(recording.path).events[0]
    assert event.meta["error"] == {"type": "ValueError", "message": "no such file"}
    assert event.res is None


def test_the_recorder_does_not_re_enter_itself(recording):
    # A payload whose repr reads the clock would otherwise recurse into the
    # recorder from inside serialisation.
    import time

    class Nosy:
        def __repr__(self):
            time.time()
            return "<nosy>"

    tape.record_event("tool", {"name": "t", "args": {"o": Nosy()}})
    tape.uninstall()
    assert len(tape.read_trace(recording.path).events) == 1


def test_busy_flag_is_clear_outside_the_recorder(recording):
    assert not recorder_mod.is_busy()
    tape.record_event("tool", {"name": "t"})
    assert not recorder_mod.is_busy()


def test_stats_accumulate_cost_and_tokens(recording):
    tape.record_event(
        "llm", {"model": "m"}, {"tokens": {"in": 100, "out": 20}}, meta={"cost_usd": 0.01}
    )
    tape.record_event(
        "llm", {"model": "m"}, {"tokens": {"in": 300, "out": 5}}, meta={"cost_usd": 0.02}
    )
    summary = tape.uninstall()

    assert summary.events == 2
    assert summary.cost_usd == pytest.approx(0.03)
    assert summary.tokens == {"in": 400, "out": 25}
    assert summary.kinds == {"llm": 2}
    assert "recorded 2 events" in summary.line()


def test_paused_blocks_are_not_recorded(recording):
    tape.record_event("tool", {"name": "before"})
    with recording.paused():
        tape.record_event("tool", {"name": "during"})
    tape.record_event("tool", {"name": "after"})
    tape.uninstall()

    names = [e.name for e in tape.read_trace(recording.path).events]
    assert names == ["before", "after"]


def test_record_event_is_a_no_op_with_no_tape():
    assert tape.record_event("tool", {"name": "t"}) is None
