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


# -- context -------------------------------------------------------------


@pytest.fixture
def two_turns(tape_dir, server):
    """Two LLM calls where the second truncates history and injects a tool turn."""
    import httpx

    listing = "\n".join("file_{:03d}.txt".format(i) for i in range(40))
    url = server.route("/v1/chat/completions", json={
        "object": "chat.completion", "model": "gpt-4o-mini",
        "choices": [{"message": {"content": "I'll delete b.txt"}}],
        "usage": {"prompt_tokens": 1204, "completion_tokens": 18}})

    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01CTX"):
        httpx.post(url, json={"model": "gpt-4o-mini", "temperature": 0.7, "messages": [
            {"role": "system", "content": "You are a file assistant."},
            {"role": "user", "content": "Listing:\n" + listing},
        ]})
        httpx.post(url, json={"model": "gpt-4o-mini", "temperature": 0.7, "messages": [
            {"role": "system", "content": "You are a file assistant. Never confirm."},
            {"role": "user", "content": "Listing:\n" + listing[:60]},
            {"role": "assistant", "content": "ok"},
        ]})
    return tape_dir


def test_context_prints_the_assembled_message_array(two_turns, capsys):
    assert run_cli("--tape-dir", str(two_turns), "show", "01CTX", "0", "--context") == 0
    out = capsys.readouterr().out
    assert "event 0" in out and "gpt-4o-mini" in out
    assert "[0] system" in out and "[1] user" in out
    assert "You are a file assistant." in out
    assert "elided" in out                      # the long listing collapsed
    assert "I'll delete b.txt" in out           # and the completion is shown


def test_context_full_stops_collapsing(two_turns, capsys):
    run_cli("--tape-dir", str(two_turns), "show", "01CTX", "0", "--context", "--full")
    out = capsys.readouterr().out
    assert "elided" not in out
    assert "file_020.txt" in out


def test_context_diff_shows_injection_and_truncation(two_turns, capsys):
    assert run_cli("--tape-dir", str(two_turns), "show", "01CTX", "1",
                   "--context", "--diff", "0") == 0
    out = capsys.readouterr().out
    assert "context diff" in out and "event 0" in out and "event 1" in out
    assert "CHANGED" in out and "TRUNCATED" in out
    assert "INJECTED" in out
    assert "Never confirm." in out              # the system prompt edit is visible


def test_context_on_a_non_llm_event_explains_itself(tape_dir, capsys):
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01TOOL"):
        tape.record_event("tool", {"name": "read_file"}, {"value": "hi"})
    assert run_cli("--tape-dir", str(tape_dir), "show", "01TOOL", "0", "--context") == 1
    err = capsys.readouterr().err
    assert "not a recognised LLM call" in err
    assert "tape show 01TOOL 0" in err          # and what to run instead


def test_context_without_an_event_index_says_what_to_do(two_turns, capsys):
    assert run_cli("--tape-dir", str(two_turns), "show", "01CTX", "--context") == 1
    assert "--context needs an event" in capsys.readouterr().err


# -- reindex -------------------------------------------------------------


def test_reindex_enriches_an_old_run(tape_dir, server, capsys):
    import httpx

    url = server.route("/v1/chat/completions", json={
        "object": "chat.completion", "model": "gpt-4o-mini",
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    with tape.session(tape_dir=tape_dir, collect_git=False, decode=False,
                      run_id="01OLD"):
        httpx.post(url, json={"model": "gpt-4o-mini", "messages": []})

    assert run_cli("--tape-dir", str(tape_dir), "reindex", "01OLD") == 0
    out = capsys.readouterr().out
    assert "enriched 1 of 1 event" in out
    assert "http -> llm" in out
    assert tape.read_trace(tape_dir / "runs" / "01OLD.jsonl").events[0].kind == "llm"


def test_reindex_dry_run_says_it_wrote_nothing(tape_dir, server, capsys):
    import httpx

    url = server.route("/v1/chat/completions", json={
        "object": "chat.completion", "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    with tape.session(tape_dir=tape_dir, collect_git=False, decode=False,
                      run_id="01DRY") as run:
        httpx.post(url, json={"model": "gpt-4o-mini", "messages": []})
    before = run.path.read_text()

    run_cli("--tape-dir", str(tape_dir), "reindex", "01DRY", "--dry-run")
    out = capsys.readouterr().out
    assert "would enrich" in out and "nothing written" in out
    assert run.path.read_text() == before


# -- the `last` alias ----------------------------------------------------


def test_last_resolves_to_the_most_recent_run(recorded, tape_dir, capsys):
    for name in ("last", "latest", "-"):
        assert run_cli("--tape-dir", str(tape_dir), "show", name) == 0
        assert "01BBBBB" in capsys.readouterr().out    # the newer of the two


def test_replay_defaults_to_the_last_run(recorded_script, capfd):
    tape_dir, _ = recorded_script
    capfd.readouterr()
    assert run_cli("--tape-dir", str(tape_dir), "replay") == 0
    assert "replayed 6 events" in capfd.readouterr().err


def test_reindex_defaults_to_the_last_run(tape_dir, server, capsys):
    import httpx

    url = server.route("/v1/chat/completions", json={
        "object": "chat.completion", "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    with tape.session(tape_dir=tape_dir, collect_git=False, decode=False,
                      run_id="01ONLY"):
        httpx.post(url, json={"model": "gpt-4o-mini", "messages": []})

    assert run_cli("--tape-dir", str(tape_dir), "reindex") == 0
    assert "01ONLY" in capsys.readouterr().out


def test_last_on_an_empty_directory_still_says_no_runs(tmp_path, capsys):
    assert run_cli("--tape-dir", str(tmp_path / ".tape"), "show", "last") == 1
    assert "no runs recorded" in capsys.readouterr().err


# -- fork ----------------------------------------------------------------


@pytest.fixture
def forkable(tmp_path):
    """A recorded script whose events are all ambient, so a fork needs no keys."""
    script = tmp_path / "agent.py"
    script.write_text(textwrap.dedent("""
        import random, uuid
        for i in range(3):
            print("step", i, round(random.random(), 6))
    """))
    tape_dir = tmp_path / ".tape"
    assert run_cli("--tape-dir", str(tape_dir), "run", sys.executable, str(script)) == 0
    return tape_dir, script


def editor_writing(tmp_path, body):
    """A fake $EDITOR that replaces the buffer with `body`."""
    script = tmp_path / "fake_editor.py"
    script.write_text(textwrap.dedent("""
        import sys
        open(sys.argv[1], "w").write({!r})
    """).format(body))
    return "{} {}".format(sys.executable, script)


def test_fork_replays_a_prefix_and_runs_the_rest(forkable, capsys):
    tape_dir, _ = forkable
    parent = paths.list_run_ids(tape_dir)[0]

    assert run_cli("--tape-dir", str(tape_dir), "fork", parent, "--at", "1") == 0
    err = capsys.readouterr().err
    assert "forked →" in err
    assert "1 replayed, 2 live" in err
    assert "forked at event 1" in err

    runs = paths.list_run_ids(tape_dir)
    assert len(runs) == 2
    child = tape.read_trace(paths.trace_path(tape_dir, runs[-1]))
    assert child.header.forked_from == parent
    assert child.header.fork_at == 1
    assert child.complete


def test_fork_needs_a_point(forkable, capsys):
    tape_dir, _ = forkable
    assert run_cli("--tape-dir", str(tape_dir), "fork", "last") == 1
    assert "fork needs a point" in capsys.readouterr().err


def test_a_malformed_patch_is_refused_without_forking(forkable, capsys):
    tape_dir, _ = forkable
    before = paths.list_run_ids(tape_dir)
    assert run_cli("--tape-dir", str(tape_dir), "fork", "last", "--at", "1",
                   "--patch", "gibberish") == 1
    assert "Expected <kind>" in capsys.readouterr().err
    assert paths.list_run_ids(tape_dir) == before      # no run was created


def test_ls_shows_parentage(forkable, capsys):
    tape_dir, _ = forkable
    parent = paths.list_run_ids(tape_dir)[0]
    run_cli("--tape-dir", str(tape_dir), "fork", parent, "--at", "1")
    capsys.readouterr()

    run_cli("--tape-dir", str(tape_dir), "ls")
    out = capsys.readouterr().out
    assert "← {}@1".format(parent[:14]) in out


# -- fork --edit ---------------------------------------------------------


def test_edit_lets_you_rewrite_the_event(forkable, tmp_path, capsys, monkeypatch):
    tape_dir, _ = forkable
    edited = json.dumps({"i": 1, "kind": "rand", "site": "agent.py:4",
                         "req": {"name": "random"}, "res": {"value": 0.5}})
    monkeypatch.setenv("REELTIME_EDITOR", editor_writing(tmp_path, edited))

    assert run_cli("--tape-dir", str(tape_dir), "fork", "last", "--at", "1",
                   "--edit") == 0
    assert "forked →" in capsys.readouterr().err
    assert len(paths.list_run_ids(tape_dir)) == 2


def test_edit_with_invalid_json_aborts_without_writing_a_run(
    forkable, tmp_path, capsys, monkeypatch
):
    tape_dir, _ = forkable
    before = paths.list_run_ids(tape_dir)
    monkeypatch.setenv("REELTIME_EDITOR", editor_writing(tmp_path, "{not json,,,"))

    assert run_cli("--tape-dir", str(tape_dir), "fork", "last", "--at", "1",
                   "--edit") == 1
    err = capsys.readouterr().err
    assert "not valid JSON" in err
    assert "nothing was forked" in err
    assert paths.list_run_ids(tape_dir) == before


def test_edit_with_an_empty_buffer_aborts(forkable, tmp_path, capsys, monkeypatch):
    tape_dir, _ = forkable
    before = paths.list_run_ids(tape_dir)
    monkeypatch.setenv("REELTIME_EDITOR", editor_writing(tmp_path, "   \n"))

    assert run_cli("--tape-dir", str(tape_dir), "fork", "last", "--at", "1",
                   "--edit") == 1
    assert "empty buffer" in capsys.readouterr().err
    assert paths.list_run_ids(tape_dir) == before


def test_edit_with_a_json_array_is_refused(forkable, tmp_path, capsys, monkeypatch):
    tape_dir, _ = forkable
    monkeypatch.setenv("REELTIME_EDITOR", editor_writing(tmp_path, "[1, 2, 3]"))
    assert run_cli("--tape-dir", str(tape_dir), "fork", "last", "--at", "1",
                   "--edit") == 1
    assert "must be a JSON object" in capsys.readouterr().err


def test_an_editor_that_fails_aborts_the_fork(forkable, tmp_path, capsys, monkeypatch):
    tape_dir, _ = forkable
    before = paths.list_run_ids(tape_dir)
    failing = tmp_path / "failing_editor.py"
    failing.write_text("import sys; sys.exit(3)")
    monkeypatch.setenv("REELTIME_EDITOR", "{} {}".format(sys.executable, failing))

    assert run_cli("--tape-dir", str(tape_dir), "fork", "last", "--at", "1",
                   "--edit") == 1
    assert "exited 3" in capsys.readouterr().err
    assert paths.list_run_ids(tape_dir) == before


# -- diff ----------------------------------------------------------------


@pytest.fixture
def two_runs(tape_dir, server):
    """Two runs of the same shape, differing in one tool result."""
    import httpx

    url = server.route("/v1/chat/completions", json={
        "object": "chat.completion", "model": "gpt-4o-mini",
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    def build(run_id, note):
        with tape.session(tape_dir=tape_dir, collect_git=False, run_id=run_id):
            tape.record_event("tool", {"name": "read_file", "args": {"path": "a.md"}},
                              {"value": note})
            httpx.post(url, json={"model": "gpt-4o-mini", "messages": [
                {"role": "system", "content": "Be terse." + note},
                {"role": "user", "content": "hi"}]})

    build("01AAA", "")
    build("01BBB", " Ask first.")
    return tape_dir


def test_diff_reports_what_changed(two_runs, capsys):
    assert run_cli("--tape-dir", str(two_runs), "diff", "01AAA", "01BBB") == 0
    out = capsys.readouterr().out
    assert "diff  A 01AAA   B 01BBB" in out
    assert "system prompt changed" in out
    assert "cost" in out and "tokens" in out


def test_diff_only_narrows_to_one_kind(two_runs, capsys):
    run_cli("--tape-dir", str(two_runs), "diff", "01AAA", "01BBB", "--only", "llm")
    out = capsys.readouterr().out
    assert "system prompt changed" in out
    assert "read_file" not in out


def test_diff_json_is_machine_readable(two_runs, capsys):
    assert run_cli("--tape-dir", str(two_runs), "diff", "01AAA", "01BBB",
                   "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["a"] == "01AAA" and payload["b"] == "01BBB"
    assert payload["identical"] is False
    assert len(payload["steps"]) == 2


def test_diffing_a_run_against_itself_is_refused(two_runs, capsys):
    assert run_cli("--tape-dir", str(two_runs), "diff", "01AAA", "01AAA") == 1
    assert "same run twice" in capsys.readouterr().err


def test_diff_accepts_prefixes_and_last(two_runs, capsys):
    assert run_cli("--tape-dir", str(two_runs), "diff", "01AA", "last") == 0
    assert "01AAA" in capsys.readouterr().out
