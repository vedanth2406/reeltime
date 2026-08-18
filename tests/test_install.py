import json
import os
import subprocess
import sys

import pytest

import reeltime as tape
from reeltime.core import paths
from reeltime.errors import TapeConfigError, TapeStateError


def test_install_writes_a_header_immediately(tape_dir):
    run = tape.install(tape_dir=tape_dir, collect_git=False)
    try:
        header = json.loads(run.path.read_text().splitlines()[0])
        assert header["run_id"] == run.run_id
        assert header["mode"] == "record"
        assert header["tool"]["name"] == "reeltime"
    finally:
        tape.uninstall()


def test_the_tape_directory_is_laid_out(tape_dir):
    tape.install(tape_dir=tape_dir, collect_git=False)
    tape.uninstall()
    assert (tape_dir / "runs").is_dir()
    assert (tape_dir / "blobs").is_dir()


def test_current_and_is_recording_track_the_lifecycle(tape_dir):
    assert tape.current() is None
    assert not tape.is_recording()

    run = tape.install(tape_dir=tape_dir, collect_git=False)
    assert tape.current() is run
    assert tape.is_recording()

    tape.uninstall()
    assert tape.current() is None
    assert not tape.is_recording()


def test_installing_twice_is_refused(tape_dir):
    tape.install(tape_dir=tape_dir, collect_git=False)
    with pytest.raises(TapeStateError, match="already installed"):
        tape.install(tape_dir=tape_dir)
    tape.uninstall()


def test_uninstall_without_a_tape_is_harmless():
    assert tape.uninstall() is None


def test_session_records_a_block(tape_dir):
    with tape.session(tape_dir=tape_dir, collect_git=False) as run:
        tape.record_event("tool", {"name": "t"})
    assert run.closed
    assert run.summary.events == 1
    assert tape.current() is None


def test_session_closes_the_trace_even_when_the_block_raises(tape_dir):
    with pytest.raises(RuntimeError):
        with tape.session(tape_dir=tape_dir, collect_git=False) as run:
            tape.record_event("tool", {"name": "t"})
            raise RuntimeError("agent crashed")

    # The crashed run is the run you want to read, so it must be complete.
    result = tape.read_trace(run.path)
    assert result.complete and len(result) == 1
    assert tape.current() is None


def test_fork_needs_a_run_and_a_point(tape_dir):
    with pytest.raises(TapeConfigError, match="needs a run to fork"):
        tape.install("fork", tape_dir=tape_dir)


def test_replay_needs_a_run_to_replay(tape_dir):
    with pytest.raises(TapeConfigError, match="needs a run to replay"):
        tape.install("replay", tape_dir=tape_dir)


def test_unknown_modes_are_rejected(tape_dir):
    with pytest.raises(TapeConfigError, match="unknown mode"):
        tape.install("rewind", tape_dir=tape_dir)


def test_unknown_config_keys_are_rejected(tape_dir):
    with pytest.raises(TapeConfigError, match="unknown configuration"):
        tape.install(tape_dir=tape_dir, blobb_threshold=1)


def test_run_ids_can_be_supplied(tape_dir):
    run = tape.install(tape_dir=tape_dir, run_id="01FIXED", collect_git=False)
    tape.uninstall()
    assert run.path == tape_dir / "runs" / "01FIXED.jsonl"
    assert paths.list_run_ids(tape_dir) == ["01FIXED"]


def test_custom_redaction_patterns_apply_to_the_running_tape(tape_dir):
    tape.install(tape_dir=tape_dir, collect_git=False, redact=[r"ACME-[A-Z0-9]{8}"])
    tape.record_event("tool", {"name": "t", "args": {"k": "ACME-ABCD1234"}})
    run_path = tape.current().path
    tape.uninstall()
    assert "ACME-ABCD1234" not in run_path.read_text()
    assert "<redacted:config>" in run_path.read_text()


def test_redact_registered_before_install_still_applies(tape_dir):
    tape.redact(r"ZZZ-\d{4}", "zzz")
    with tape.session(tape_dir=tape_dir, collect_git=False) as run:
        tape.record_event("tool", {"name": "t", "args": {"k": "ZZZ-1234"}})
    assert "ZZZ-1234" not in run.path.read_text()
    assert run.summary.redacted == {"zzz": 1}


def test_env_var_selects_the_tape_directory(tape_dir, monkeypatch):
    monkeypatch.setenv("TAPE_DIR", str(tape_dir))
    with tape.session(collect_git=False) as run:
        pass
    assert run.path.is_relative_to(tape_dir)


def test_tapeconfig_is_picked_up(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".tapeconfig").write_text(json.dumps({"blob_threshold": 10}))
    with tape.session(collect_git=False) as run:
        tape.record_event("tool", {"name": "t", "args": {"long": "x" * 50}})
    # Externalisation is per top-level field, so the whole `args` object moves.
    event = tape.read_trace(run.path).events[0]
    assert event.req["args"].startswith("blob:")
    assert run.blobs.resolve(event.req)["args"] == {"long": "x" * 50}


def test_a_broken_tapeconfig_says_so(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".tapeconfig").write_text("{not json")
    with pytest.raises(TapeConfigError, match="could not read"):
        tape.install()


def test_atexit_finishes_a_trace_the_script_never_closed(tmp_path):
    # The realistic case: someone calls install() at the top of a script and
    # never calls uninstall(). The trace must still be complete.
    script = tmp_path / "agent.py"
    script.write_text(
        "import random, reeltime as tape\n"
        "tape.install(tape_dir=%r, run_id='01ATEXIT', collect_git=False)\n"
        "random.random()\n" % str(tmp_path / ".tape")
    )
    subprocess.run([sys.executable, str(script)], check=True, cwd=str(tmp_path))

    result = tape.read_trace(tmp_path / ".tape" / "runs" / "01ATEXIT.jsonl")
    assert result.complete
    assert [e.kind for e in result.events] == ["rand"]


def test_a_killed_process_still_leaves_the_events_it_wrote(tmp_path):
    script = tmp_path / "agent.py"
    script.write_text(
        "import os, random, reeltime as tape\n"
        "tape.install(tape_dir=%r, run_id='01KILLED', collect_git=False)\n"
        "random.random()\n"
        "os._exit(9)\n" % str(tmp_path / ".tape")
    )
    proc = subprocess.run([sys.executable, str(script)], cwd=str(tmp_path))
    assert proc.returncode == 9

    result = tape.read_trace(tmp_path / ".tape" / "runs" / "01KILLED.jsonl")
    assert [e.kind for e in result.events] == ["rand"]
    assert not result.complete  # a missing footer is how you know it was killed


def test_autoinstall_env_var_records_an_unmodified_script(tmp_path):
    script = tmp_path / "agent.py"
    script.write_text("import reeltime\nimport random\nrandom.random()\n")
    env = dict(
        os.environ,
        REELTIME_AUTOINSTALL="1",
        TAPE_DIR=str(tmp_path / ".tape"),
        REELTIME_COLLECT_GIT="0",
    )
    subprocess.run([sys.executable, str(script)], check=True, cwd=str(tmp_path), env=env)

    runs = paths.list_run_ids(tmp_path / ".tape")
    assert len(runs) == 1
    result = tape.read_trace(paths.trace_path(tmp_path / ".tape", runs[0]))
    assert [e.kind for e in result.events] == ["rand"]
