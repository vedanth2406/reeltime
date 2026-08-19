"""`tape doctor` (M7).

The analysis is a pure function over traces, so most of this builds traces by
hand: two runs that differ in exactly one way, and an assertion about what the
report says. The end of the file drives the real command through subprocesses,
because "run it twice" is the part a synthetic trace cannot check.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

import reeltime as tape
from reeltime.core import doctor, paths
from reeltime.core.trace import Event, Header, Trace


# -- builders -------------------------------------------------------------


def ev(index, kind, value, site=None, name=None, **req):
    request = dict(req)
    if name is not None:
        request["name"] = name
    return Event(
        i=index, kind=kind, site=site or "agent.py:{}".format(10 + index),
        qual="agent.py::main", req=request, res={"value": value},
    )


def llm(index, content, site="agent.py:88", temperature=0.7, model="gpt-4o-mini",
        tokens_in=100):
    return Event(
        i=index, kind="llm", site=site, qual="agent.py::ask",
        req={"method": "POST", "url": "https://api.openai.com/v1/chat/completions",
             "body": {"json": {"model": model, "temperature": temperature,
                               "messages": [{"role": "user", "content": "hi"}]}}},
        res={"status": 200, "preview": content,
             "body": {"json": {"choices": [{"message": {"content": content}}]}},
             "tokens": {"in": tokens_in, "out": 5}},
    )


def http(index, body, site="tools.py:12", url="https://api.weather.com/now"):
    return Event(
        i=index, kind="http", site=site, qual="tools.py::weather",
        req={"method": "GET", "url": url},
        res={"status": 200, "body": {"json": body}},
    )


def trace(events, run_id="01A"):
    return Trace(
        header=Header(run_id=run_id, started="", argv=["agent.py"], cwd="", python=""),
        events=list(events),
        footer={"events": len(events)},
    )


def sources_by_kind(report):
    return {s.kind: s for s in report.sources}


# -- the clean answer -----------------------------------------------------


def test_two_identical_runs_report_nothing():
    events = [ev(0, "tool", "ok", name="read"), llm(1, "same answer")]
    report = doctor.analyse([trace(events, "01A"), trace(events, "01B")])
    assert report.clean
    assert report.sources == []
    assert report.split is None
    assert report.compared == 2


def test_the_clean_report_says_how_much_it_compared():
    events = [ev(0, "tool", "ok", name="read")]
    rendered = doctor.render(doctor.analyse([trace(events, "01A"), trace(events, "01B")]))
    assert "no nondeterminism found" in rendered
    assert "1 compared event" in rendered
    assert "not a guarantee" in rendered, "a clean report must not overclaim"


def test_fewer_than_two_runs_is_not_a_comparison():
    with pytest.raises(ValueError, match="at least two"):
        doctor.analyse([trace([])])


# -- one source at a time -------------------------------------------------


def test_a_clock_read_is_reported_with_its_call_site():
    a = trace([ev(0, "time", 1000.5, site="agent.py:34", name="time.time")], "01A")
    b = trace([ev(0, "time", 1000.9, site="agent.py:34", name="time.time")], "01B")
    report = doctor.analyse([a, b])

    source = sources_by_kind(report)["time"]
    assert source.site == "agent.py:34"
    assert source.name == "time.time"
    assert (source.diverged, source.observed) == (1, 1)
    assert "inject a clock" in source.suggestion()


def test_a_random_value_is_reported():
    a = trace([ev(0, "rand", 0.1, name="random")], "01A")
    b = trace([ev(0, "rand", 0.9, name="random")], "01B")
    assert "seed the RNG" in sources_by_kind(doctor.analyse([a, b]))["rand"].suggestion()


def test_a_uuid_is_reported():
    a = trace([ev(0, "uuid", "aaaa", name="uuid4")], "01A")
    b = trace([ev(0, "uuid", "bbbb", name="uuid4")], "01B")
    assert "pass ids in" in sources_by_kind(doctor.analyse([a, b]))["uuid"].suggestion()


def test_a_tool_that_answers_differently_is_reported():
    a = trace([ev(0, "tool", "shard-1", name="pick_shard")], "01A")
    b = trace([ev(0, "tool", "shard-2", name="pick_shard")], "01B")
    source = sources_by_kind(doctor.analyse([a, b]))["tool"]
    assert source.name == "pick_shard"
    assert source.label == "tool·pick_shard"
    assert "make the varying part an argument" in source.suggestion()


def test_an_upstream_response_that_changed_is_reported():
    a = trace([http(0, {"temp": 12})], "01A")
    b = trace([http(0, {"temp": 15})], "01B")
    source = sources_by_kind(doctor.analyse([a, b]))["http"]
    assert "1 of 1 response differed" == source.detail()
    assert "stub it or pin" in source.suggestion()


def test_an_llm_call_carries_its_model_and_temperature():
    a = trace([llm(0, "Paris.")], "01A")
    b = trace([llm(0, "Paris, France.")], "01B")
    source = sources_by_kind(doctor.analyse([a, b]))["llm"]
    assert "gpt-4o-mini" in source.note and "temperature 0.7" in source.note
    assert "1 of 1 completion differed" in source.detail()
    assert "set temperature=0" in source.suggestion()


def test_sampling_already_off_gets_a_different_suggestion():
    """Blaming the temperature when it is already 0 sends the user nowhere."""
    a = trace([llm(0, "Paris.", temperature=0)], "01A")
    b = trace([llm(0, "Lyon.", temperature=0)], "01B")
    suggestion = sources_by_kind(doctor.analyse([a, b]))["llm"].suggestion()
    assert "sampling already off" in suggestion
    assert "set temperature=0" not in suggestion


def test_a_boundary_that_agrees_is_not_a_source():
    a = trace([ev(0, "tool", "ok", name="steady"), ev(1, "rand", 0.1, name="random")], "01A")
    b = trace([ev(0, "tool", "ok", name="steady"), ev(1, "rand", 0.9, name="random")], "01B")
    report = doctor.analyse([a, b])
    assert [s.kind for s in report.sources] == ["rand"]


# -- grouping, which is what keeps the report readable --------------------


def test_many_reads_at_one_site_are_one_finding():
    a = trace([ev(i, "time", 1000.0 + i, site="agent.py:34", name="time.time")
               for i in range(40)], "01A")
    b = trace([ev(i, "time", 2000.0 + i, site="agent.py:34", name="time.time")
               for i in range(40)], "01B")
    report = doctor.analyse([a, b])

    assert len(report.sources) == 1, "forty findings would bury the one that matters"
    assert (report.sources[0].diverged, report.sources[0].observed) == (40, 40)


def test_the_same_kind_at_two_sites_is_two_findings():
    a = trace([ev(0, "time", 1.0, site="a.py:1", name="time.time"),
               ev(1, "time", 2.0, site="b.py:9", name="time.time")], "01A")
    b = trace([ev(0, "time", 5.0, site="a.py:1", name="time.time"),
               ev(1, "time", 6.0, site="b.py:9", name="time.time")], "01B")
    assert sorted(s.site for s in doctor.analyse([a, b]).sources) == ["a.py:1", "b.py:9"]


def test_evidence_is_capped():
    a = trace([ev(i, "rand", i / 10.0, site="a.py:1", name="random") for i in range(10)], "01A")
    b = trace([ev(i, "rand", 1 + i / 10.0, site="a.py:1", name="random") for i in range(10)], "01B")
    assert len(doctor.analyse([a, b]).sources[0].samples) <= doctor.MAX_SAMPLES


def test_a_source_seen_only_in_the_third_run_is_still_found():
    steady = [ev(0, "tool", "ok", name="read")]
    flaky = [ev(0, "tool", "different", name="read")]
    report = doctor.analyse([trace(steady, "01A"), trace(steady, "01B"),
                             trace(flaky, "01C")])
    assert [s.kind for s in report.sources] == ["tool"]


# -- ranking --------------------------------------------------------------


def test_decision_changing_boundaries_are_reported_above_clock_reads():
    a = trace([ev(0, "time", 1.0, site="a.py:1", name="time.time"),
               ev(1, "tool", "x", site="a.py:2", name="pick"),
               llm(2, "one", site="a.py:3")], "01A")
    b = trace([ev(0, "time", 2.0, site="a.py:1", name="time.time"),
               ev(1, "tool", "y", site="a.py:2", name="pick"),
               llm(2, "two", site="a.py:3")], "01B")
    assert [s.kind for s in doctor.analyse([a, b]).sources] == ["llm", "tool", "time"]


# -- the path split -------------------------------------------------------


def test_runs_that_call_different_things_report_a_split():
    a = trace([ev(0, "tool", True, site="a.py:1", name="coin"),
               ev(1, "tool", "a", site="a.py:2", name="path_a")], "01A")
    b = trace([ev(0, "tool", False, site="a.py:1", name="coin"),
               ev(1, "tool", "b", site="a.py:4", name="path_b")], "01B")
    report = doctor.analyse([a, b])

    assert report.split is not None and report.split.step == 1
    assert report.split.a.name == "path_a" and report.split.b.name == "path_b"


def test_a_split_is_not_reported_as_a_boundary_that_answered_differently():
    """`align` pairs unlike events so a diff can show what replaced what.

    Read as a source, that pairing blames `path_a` for returning "b" -- a line
    pointing at code that is behaving perfectly.
    """
    a = trace([ev(0, "tool", True, site="a.py:1", name="coin"),
               ev(1, "tool", "a", site="a.py:2", name="path_a")], "01A")
    b = trace([ev(0, "tool", False, site="a.py:1", name="coin"),
               ev(1, "tool", "b", site="a.py:4", name="path_b")], "01B")

    names = {s.name for s in doctor.analyse([a, b]).sources}
    assert names == {"coin"}
    assert "path_a" not in names and "path_b" not in names


def test_a_run_that_stopped_early_reports_a_split():
    a = trace([ev(0, "tool", "ok", name="read"), ev(1, "tool", "ok", name="write")], "01A")
    b = trace([ev(0, "tool", "ok", name="read")], "01B")
    report = doctor.analyse([a, b])
    assert report.split is not None and report.split.step == 1
    assert report.split.b is None


def test_the_split_is_rendered_with_both_sites():
    a = trace([ev(0, "tool", True, site="a.py:1", name="coin"),
               ev(1, "tool", "a", site="a.py:2", name="path_a")], "01A")
    b = trace([ev(0, "tool", False, site="a.py:1", name="coin"),
               ev(1, "tool", "b", site="a.py:4", name="path_b")], "01B")
    rendered = doctor.render(doctor.analyse([a, b]))

    assert "stopped making the same calls at step 1" in rendered
    assert "tool·path_a at a.py:2" in rendered
    assert "tool·path_b at a.py:4" in rendered
    assert "incomparable" in rendered


def test_propagation_is_counted_when_the_path_holds():
    """Same calls, different inputs: the source spread but did not fork."""
    a = trace([ev(0, "rand", 0.1, site="a.py:1", name="random"),
               ev(1, "tool", "ok", site="a.py:2", name="use", args={"n": 0.1})], "01A")
    b = trace([ev(0, "rand", 0.9, site="a.py:1", name="random"),
               ev(1, "tool", "ok", site="a.py:2", name="use", args={"n": 0.9})], "01B")
    report = doctor.analyse([a, b])

    assert report.split is None
    assert report.propagated == 1
    assert "1 later step was reached with a different request" in doctor.render(report)


# -- rendering ------------------------------------------------------------


def test_the_report_names_the_site_the_kind_and_the_evidence():
    a = trace([ev(0, "tool", "shard-1", site="agent.py:88", name="pick")], "01A")
    b = trace([ev(0, "tool", "shard-2", site="agent.py:88", name="pick")], "01B")
    rendered = doctor.render(doctor.analyse([a, b]))

    assert "1 nondeterminism source found" in rendered
    assert "agent.py:88" in rendered
    assert "tool·pick" in rendered
    assert "shard-1" in rendered and "shard-2" in rendered
    assert "suggestions:" in rendered


def test_a_long_path_keeps_the_end_that_you_click():
    site = "/very/long/path/that/goes/on/and/on/for/ages/deep/module.py:412"
    a = trace([ev(0, "tool", "x", site=site, name="pick")], "01A")
    b = trace([ev(0, "tool", "y", site=site, name="pick")], "01B")
    rendered = doctor.render(doctor.analyse([a, b]))
    assert "module.py:412" in rendered


def test_one_suggestion_per_distinct_piece_of_advice():
    a = trace([ev(0, "time", 1.0, site="a.py:1", name="time.time"),
               ev(1, "time", 2.0, site="b.py:2", name="time.time")], "01A")
    b = trace([ev(0, "time", 9.0, site="a.py:1", name="time.time"),
               ev(1, "time", 8.0, site="b.py:2", name="time.time")], "01B")
    rendered = doctor.render(doctor.analyse([a, b]))
    assert rendered.count("inject a clock") == 1


def test_the_json_report_carries_everything_the_text_does():
    a = trace([ev(0, "tool", "x", site="agent.py:88", name="pick")], "01A")
    b = trace([ev(0, "tool", "y", site="agent.py:88", name="pick")], "01B")
    payload = json.loads(json.dumps(doctor.analyse([a, b]).to_dict()))

    assert payload["runs"] == ["01A", "01B"]
    assert payload["clean"] is False
    source = payload["sources"][0]
    assert source["site"] == "agent.py:88"
    assert source["kind"] == "tool" and source["name"] == "pick"
    assert source["suggestion"] and source["detail"]


# -- blob references ------------------------------------------------------


def test_two_bodies_with_the_same_hash_are_not_a_source():
    """Content addressing answers this without either payload being read."""
    ref = "blob:" + "a" * 64
    a = trace([Event(i=0, kind="llm", site="a.py:1", req={}, res={"body": ref})], "01A")
    b = trace([Event(i=0, kind="llm", site="a.py:1", req={}, res={"body": ref})], "01B")
    assert doctor.analyse([a, b]).clean


def test_two_bodies_with_different_hashes_are_a_source():
    a = trace([Event(i=0, kind="llm", site="a.py:1", req={},
                     res={"body": "blob:" + "a" * 64})], "01A")
    b = trace([Event(i=0, kind="llm", site="a.py:1", req={},
                     res={"body": "blob:" + "b" * 64})], "01B")
    report = doctor.analyse([a, b])
    assert len(report.sources) == 1
    assert "<aaaaaaaaaaaa" in report.sources[0].samples[0]


# -- the odd shapes ------------------------------------------------------


def test_a_missing_value_renders_as_a_dash_rather_than_none():
    a = trace([Event(i=0, kind="tool", site="a.py:1", req={"name": "t"}, res={})], "01A")
    b = trace([Event(i=0, kind="tool", site="a.py:1", req={"name": "t"},
                     res={"value": "x"})], "01B")
    assert "—" in doctor.render(doctor.analyse([a, b]))


def test_a_kind_with_no_special_handling_still_compares():
    """A kind added to the trace format before doctor learns about it."""
    a = trace([Event(i=0, kind="future", site="a.py:1", req={}, res={"x": 1})], "01A")
    b = trace([Event(i=0, kind="future", site="a.py:1", req={}, res={"x": 2})], "01B")
    assert doctor.outcome(a.events[0]) != doctor.outcome(b.events[0])


def test_a_text_response_body_is_rendered_as_its_text():
    a = trace([Event(i=0, kind="http", site="a.py:1", req={},
                     res={"body": {"text": "first"}})], "01A")
    b = trace([Event(i=0, kind="http", site="a.py:1", req={},
                     res={"body": {"text": "second"}})], "01B")
    rendered = doctor.render(doctor.analyse([a, b]))
    assert "first" in rendered and "second" in rendered


def test_a_raw_response_body_still_renders():
    a = trace([Event(i=0, kind="http", site="a.py:1", req={},
                     res={"body": {"raw": "AAA="}})], "01A")
    b = trace([Event(i=0, kind="http", site="a.py:1", req={},
                     res={"body": {"raw": "BBB="}})], "01B")
    assert "AAA=" in doctor.render(doctor.analyse([a, b]))


def test_a_request_parameter_is_found_at_the_top_level_too():
    """Not every shape buries the model inside a JSON body."""
    event = Event(i=0, kind="llm", site="a.py:1", req={"model": "claude-sonnet-4-5"})
    assert doctor.request_param(event, "model") == "claude-sonnet-4-5"
    assert doctor.request_param(event, "temperature") is None


def test_the_split_json_names_both_sides():
    a = trace([ev(0, "tool", "a", site="a.py:2", name="path_a")], "01A")
    b = trace([ev(0, "tool", "b", site="a.py:4", name="path_b")], "01B")
    payload = doctor.analyse([a, b]).to_dict()
    assert payload["split"]["a"]["name"] == "path_a"
    assert payload["split"]["b"]["name"] == "path_b"


def test_a_run_that_stopped_early_renders_without_a_second_side():
    a = trace([ev(0, "tool", "ok", name="read"), ev(1, "tool", "ok", name="write")], "01A")
    b = trace([ev(0, "tool", "ok", name="read")], "01B")
    assert "run 2 had nothing here" in doctor.render(doctor.analyse([a, b]))


def test_a_split_with_no_source_above_it_does_not_read_as_a_clean_bill():
    """The branch was decided by something reeltime is not watching."""
    a = trace([ev(0, "tool", "a", site="a.py:2", name="path_a")], "01A")
    b = trace([ev(0, "tool", "b", site="a.py:4", name="path_b")], "01B")
    report = doctor.analyse([a, b])

    assert report.sources == [] and report.split is not None
    assert not report.clean

    rendered = doctor.render(report)
    assert "no nondeterminism found" not in rendered
    assert "not at any boundary reeltime records" in rendered
    assert "reeltime does not see" in rendered
    assert "suggestions:" not in rendered, "there is nothing to suggest"


# -- the command, run for real --------------------------------------------


AGENT = '''
import random, time
import reeltime as tape

@tape.tool
def steady():
    return "always the same"

@tape.tool
def pick_shard():
    return "shard-" + str(random.randint(0, 99))

for _ in range(2):
    time.time()
steady()
pick_shard()
'''

STEADY_AGENT = '''
import reeltime as tape

@tape.tool
def steady():
    return "always the same"

steady()
steady()
'''


def _write(tmp_path, name, source):
    path = tmp_path / name
    path.write_text(textwrap.dedent(source))
    return str(path)


def _doctor(tape_dir, *extra, **kwargs):
    command = [sys.executable, "-m", "reeltime.cli", "--tape-dir", str(tape_dir),
               "doctor"] + list(extra)
    return subprocess.run(command, capture_output=True, text=True, timeout=180,
                          env=dict(os.environ), **kwargs)


def test_doctor_runs_the_command_twice_and_finds_the_flaky_tool(tmp_path):
    agent = _write(tmp_path, "flaky_agent.py", AGENT)
    tape_dir = tmp_path / ".tape"

    result = _doctor(tape_dir, sys.executable, agent)
    assert result.returncode == 0, result.stderr

    assert "run 1 of 2" in result.stderr and "run 2 of 2" in result.stderr
    assert len(paths.list_run_ids(tape_dir)) == 2, "both runs should be kept"

    assert "nondeterminism source" in result.stdout
    assert "tool·pick_shard" in result.stdout
    assert "steady" not in result.stdout, "a boundary that agrees is not a source"
    assert "time·time.time" in result.stdout


def test_doctor_says_what_it_is_about_to_do_before_it_does_it(tmp_path):
    agent = _write(tmp_path, "flaky_agent.py", AGENT)
    result = _doctor(tmp_path / ".tape", sys.executable, agent)
    warning = result.stderr.split("run 1 of 2")[0]
    assert "real runs, real calls, real cost" in warning


def test_doctor_reports_a_clean_run_as_clean(tmp_path):
    agent = _write(tmp_path, "steady_agent.py", STEADY_AGENT)
    result = _doctor(tmp_path / ".tape", sys.executable, agent)
    assert result.returncode == 0, result.stderr
    assert "no nondeterminism found" in result.stdout


def test_doctor_honours_runs(tmp_path):
    agent = _write(tmp_path, "steady_agent.py", STEADY_AGENT)
    tape_dir = tmp_path / ".tape"
    result = _doctor(tape_dir, "--runs", "3", sys.executable, agent)
    assert result.returncode == 0, result.stderr
    assert len(paths.list_run_ids(tape_dir)) == 3


def test_doctor_refuses_a_single_run(tmp_path):
    agent = _write(tmp_path, "steady_agent.py", STEADY_AGENT)
    result = _doctor(tmp_path / ".tape", "--runs", "1", sys.executable, agent)
    assert result.returncode != 0
    assert "at least 2" in result.stderr


def test_doctor_needs_a_command(tmp_path):
    result = _doctor(tmp_path / ".tape")
    assert result.returncode == 2
    assert "give me a command" in result.stderr


def test_doctor_reports_a_command_that_does_not_exist(tmp_path):
    result = _doctor(tmp_path / ".tape", "definitely-not-a-real-binary-xyz")
    assert result.returncode == 127
    assert "no such command" in result.stderr


def test_fail_on_findings_is_a_ci_gate(tmp_path):
    flaky = _write(tmp_path, "flaky_agent.py", AGENT)
    steady = _write(tmp_path, "steady_agent.py", STEADY_AGENT)

    found = _doctor(tmp_path / ".tape", "--fail-on-findings", sys.executable, flaky)
    assert found.returncode == 1

    clean = _doctor(tmp_path / ".clean", "--fail-on-findings", sys.executable, steady)
    assert clean.returncode == 0, clean.stderr


def test_the_json_output_is_the_whole_report(tmp_path):
    agent = _write(tmp_path, "flaky_agent.py", AGENT)
    result = _doctor(tmp_path / ".tape", "--json", sys.executable, agent)
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout)
    assert len(payload["runs"]) == 2
    assert payload["clean"] is False
    assert any(s["name"] == "pick_shard" for s in payload["sources"])


def test_doctor_analyses_a_command_that_fails_every_time(tmp_path):
    """A command that fails identically is a finding of its own: not flaky."""
    agent = _write(tmp_path, "boom.py", '''
        import reeltime as tape

        @tape.tool
        def step():
            return "ok"

        step()
        raise SystemExit(3)
    ''')
    result = _doctor(tmp_path / ".tape", sys.executable, agent)
    assert result.returncode == 0, result.stderr
    assert "(exited 3)" in result.stderr
    assert "no nondeterminism found" in result.stdout
