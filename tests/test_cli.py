import json
import subprocess
import sys
import textwrap

import pytest

import reeltime as tape
from reeltime.cli import main
from reeltime.core import paths


def run_cli(*argv):
    return main(list(argv))


@pytest.fixture
def recorded(tape_dir, server):
    """Two finished runs on disk, one of them an LLM call."""
    url = server.route("/v1/chat/completions", json={
        "object": "chat.completion",
        "model": "gpt-4o-mini",
        "choices": [{"message": {"content": "Hello there"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    })
    import httpx

    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01AAAAA") as first:
        tape.record_event("tool", {"name": "read_file", "args": {"path": "a.md"}},
                          {"value": "hi"})
        httpx.post(url, json={"model": "gpt-4o-mini",
                              "messages": [{"role": "user", "content": "hi"}]})
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01BBBBB") as second:
        tape.record_event("tool", {"name": "search"}, {"value": []})
    return first, second


# -- ls ------------------------------------------------------------------


def test_ls_lists_runs_newest_first(recorded, tape_dir, capsys):
    assert run_cli("--tape-dir", str(tape_dir), "ls") == 0
    out = capsys.readouterr().out
    assert out.index("01BBBBB") < out.index("01AAAAA")
    assert "EVENTS" in out


def test_ls_json_is_machine_readable(recorded, tape_dir, capsys):
    assert run_cli("--tape-dir", str(tape_dir), "ls", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["run_id"] for r in rows] == ["01BBBBB", "01AAAAA"]
    assert rows[1]["events"] == 2
    assert rows[1]["cost_usd"] > 0


def test_ls_on_an_empty_directory_says_so(tmp_path, capsys):
    assert run_cli("--tape-dir", str(tmp_path / ".tape"), "ls") == 0
    assert "no runs" in capsys.readouterr().out


def test_ls_respects_a_limit(recorded, tape_dir, capsys):
    run_cli("--tape-dir", str(tape_dir), "ls", "-n", "1", "--json")
    assert len(json.loads(capsys.readouterr().out)) == 1


# -- show ----------------------------------------------------------------


def test_show_prints_the_run_and_its_events(recorded, tape_dir, capsys):
    assert run_cli("--tape-dir", str(tape_dir), "show", "01AAAAA") == 0
    out = capsys.readouterr().out
    assert "01AAAAA" in out
    assert "read_file" in out
    assert "gpt-4o-mini 10→4 Hello there" in out


def test_show_accepts_a_run_id_prefix(recorded, tape_dir, capsys):
    assert run_cli("--tape-dir", str(tape_dir), "show", "01A") == 0
    assert "01AAAAA" in capsys.readouterr().out


def test_an_ambiguous_prefix_is_refused(recorded, tape_dir, capsys):
    assert run_cli("--tape-dir", str(tape_dir), "show", "01") == 1
    assert "ambiguous" in capsys.readouterr().err


def test_show_one_event_prints_it_in_full(recorded, tape_dir, capsys):
    assert run_cli("--tape-dir", str(tape_dir), "show", "01AAAAA", "0") == 0
    event = json.loads(capsys.readouterr().out)
    assert event["kind"] == "tool"
    assert event["req"]["args"] == {"path": "a.md"}


def test_show_resolves_blobs_by_default(tape_dir, capsys):
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01BIG"):
        tape.record_event("tool", {"name": "t", "args": {"big": "x" * 20_000}})

    run_cli("--tape-dir", str(tape_dir), "show", "01BIG", "0")
    assert json.loads(capsys.readouterr().out)["req"]["args"]["big"] == "x" * 20_000

    run_cli("--tape-dir", str(tape_dir), "show", "01BIG", "0", "--raw")
    assert json.loads(capsys.readouterr().out)["req"]["args"].startswith("blob:")


def test_show_a_missing_event_says_how_many_there_are(recorded, tape_dir, capsys):
    assert run_cli("--tape-dir", str(tape_dir), "show", "01AAAAA", "99") == 1
    assert "has no event 99" in capsys.readouterr().err


def test_show_without_any_runs_is_a_clean_error(tmp_path, capsys):
    assert run_cli("--tape-dir", str(tmp_path / ".tape"), "show", "01X") == 1
    assert "no runs recorded" in capsys.readouterr().err


# -- run -----------------------------------------------------------------


def test_run_records_an_unmodified_script(tmp_path, capsys):
    # The zero-edit promise: no reeltime import anywhere in this file.
    script = tmp_path / "agent.py"
    script.write_text(textwrap.dedent("""
        import random, uuid
        print("id", uuid.uuid4(), "n", random.random())
    """))
    tape_dir = tmp_path / ".tape"

    code = run_cli("--tape-dir", str(tape_dir), "run", sys.executable, str(script))
    assert code == 0
    assert "recorded 2 events" in capsys.readouterr().err

    run_ids = paths.list_run_ids(tape_dir)
    assert len(run_ids) == 1
    trace = tape.read_trace(paths.trace_path(tape_dir, run_ids[0]))
    assert sorted(e.kind for e in trace.events) == ["rand", "uuid"]
    assert trace.complete


def test_run_forwards_the_exit_code(tmp_path, capsys):
    script = tmp_path / "agent.py"
    script.write_text("import sys; sys.exit(3)")
    code = run_cli("--tape-dir", str(tmp_path / ".tape"), "run",
                   sys.executable, str(script))
    assert code == 3
    assert "✗" in capsys.readouterr().err


def test_a_crashed_run_keeps_its_events_and_says_so(tmp_path, capsys):
    script = tmp_path / "agent.py"
    script.write_text(textwrap.dedent("""
        import os, random
        random.random()
        os._exit(9)
    """))
    tape_dir = tmp_path / ".tape"
    assert run_cli("--tape-dir", str(tape_dir), "run", sys.executable, str(script)) == 9

    err = capsys.readouterr().err
    assert "did not exit cleanly" in err
    run_ids = paths.list_run_ids(tape_dir)
    trace = tape.read_trace(paths.trace_path(tape_dir, run_ids[0]))
    assert [e.kind for e in trace.events] == ["rand"]
    assert not trace.complete


def test_run_needs_a_command(capsys):
    assert run_cli("run") == 2
    assert "give me a command" in capsys.readouterr().err


def test_run_reports_an_unknown_command(tmp_path, capsys):
    assert run_cli("--tape-dir", str(tmp_path / ".tape"), "run",
                   "definitely-not-a-real-binary") == 127
    assert "no such command" in capsys.readouterr().err


def test_the_users_own_sitecustomize_still_runs(tmp_path):
    # Our shim goes to the front of PYTHONPATH, which shadows the name
    # `sitecustomize`. Silently disabling someone's startup hook would be a
    # nasty thing for a debugger to do, so ours re-imports theirs.
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    (theirs / "sitecustomize.py").write_text(
        "import sys; sys.stderr.write('THEIR SITECUSTOMIZE RAN\\n')"
    )
    script = tmp_path / "agent.py"
    script.write_text("import random; random.random()")

    import os

    proc = subprocess.run(
        [sys.executable, "-m", "reeltime.cli", "--tape-dir", str(tmp_path / ".tape"),
         "run", sys.executable, str(script)],
        cwd=str(tmp_path), capture_output=True, text=True,
        env=dict(os.environ, PYTHONPATH=str(theirs)),
    )
    assert "THEIR SITECUSTOMIZE RAN" in proc.stderr
    assert "recorded 1 event" in proc.stderr


# -- misc ----------------------------------------------------------------


def test_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        run_cli("--version")
    assert exit_info.value.code == 0
    assert tape.__version__ in capsys.readouterr().out


def test_bare_invocation_prints_help(capsys):
    assert run_cli() == 0
    out = capsys.readouterr().out
    assert "run" in out and "planned:" in out


def test_sub_cent_costs_are_not_rounded_to_zero(recorded, tape_dir, capsys):
    # A single call usually costs a fraction of a cent. Showing that as $0.00
    # makes the one number a user might act on look like nothing.
    run_cli("--tape-dir", str(tape_dir), "ls")
    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()[1:]}
    # A few millionths of a dollar: too small for four decimals, but not zero.
    assert "<$0.0001" in rows["01AAAAA"]
    assert "$0.00" in rows["01BBBBB"]     # genuinely zero, shown as zero


@pytest.mark.parametrize(
    "amount,shown",
    [(None, "–"), (0, "$0.00"), (0.0000039, "<$0.0001"), (0.0031, "$0.0031"),
     (0.31, "$0.31"), (12.5, "$12.50")],
)
def test_cost_formatting(amount, shown):
    from reeltime.core import fmt

    assert fmt.usd(amount) == shown


# -- replay --------------------------------------------------------------


@pytest.fixture
def recorded_script(tmp_path, capfd):
    """A script recorded through `tape run`, ready to replay.

    Depends on capfd so that fd-level capture is active before the recording
    subprocess runs; otherwise the replay tests have nothing to compare to.
    """
    script = tmp_path / "agent.py"
    script.write_text(textwrap.dedent("""
        import random, uuid
        for i in range(3):
            print("step", i, round(random.random(), 6), uuid.uuid4())
    """))
    tape_dir = tmp_path / ".tape"
    assert run_cli("--tape-dir", str(tape_dir), "run", sys.executable, str(script)) == 0
    return tape_dir, script


def test_replay_reruns_the_recorded_command(recorded_script, capfd):
    tape_dir, _ = recorded_script
    original = capfd.readouterr().out

    run_id = paths.list_run_ids(tape_dir)[0]
    assert run_cli("--tape-dir", str(tape_dir), "replay", run_id[:8]) == 0

    captured = capfd.readouterr()
    # Same output, from the tape.
    assert captured.out == original
    assert "replayed 6 events" in captured.err
    assert "wall clock" in captured.err


def test_replay_to_stops_early(recorded_script, capfd):
    tape_dir, _ = recorded_script
    capfd.readouterr()
    run_id = paths.list_run_ids(tape_dir)[0]

    run_cli("--tape-dir", str(tape_dir), "replay", run_id[:8], "--to", "1")
    captured = capfd.readouterr()
    assert "stopped after event 1" in captured.err
    assert captured.out.count("step") == 1


def test_replay_strict_rejects_a_changed_script(recorded_script, capfd):
    tape_dir, script = recorded_script
    capfd.readouterr()
    # A cosmetic edit: one more line above the calls, nothing else.
    script.write_text("# a new comment\n" + script.read_text())

    run_id = paths.list_run_ids(tape_dir)[0]
    code = run_cli("--tape-dir", str(tape_dir), "replay", run_id[:8], "--strict")
    assert code != 0
    assert "TapeMiss" in capfd.readouterr().err


def test_replay_default_survives_the_same_edit(recorded_script, capfd):
    tape_dir, script = recorded_script
    original = capfd.readouterr().out
    script.write_text("# a new comment\n" + script.read_text())

    run_id = paths.list_run_ids(tape_dir)[0]
    assert run_cli("--tape-dir", str(tape_dir), "replay", run_id[:8]) == 0

    captured = capfd.readouterr()
    assert captured.out == original          # identical values, edited source
    assert "matched with drifted content" in captured.err


def test_replay_needs_a_recorded_command(tape_dir, capsys):
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01NOARGV",
                      argv=[]) as run:
        tape.record_event("tool", {"name": "t"})
    assert run_cli("--tape-dir", str(tape_dir), "replay", "01NOARGV") == 1
    assert "nothing to re-run" in capsys.readouterr().err
