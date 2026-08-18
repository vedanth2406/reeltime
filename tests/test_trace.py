import json
import shutil
import subprocess

import pytest

import reeltime as tape
from reeltime.core import trace as trace_mod
from reeltime.core.trace import Event, Header, build_header, read_trace
from reeltime.errors import TapeError


def test_event_round_trips_through_json():
    event = Event(
        i=14,
        kind="llm",
        site="agent.py:88",
        qual="agent.py::plan",
        span="root/plan",
        t_rel=12.418,
        dur_ms=842.0,
        req={"model": "gpt-4o-mini"},
        res={"content": "hi", "tokens": {"in": 1204, "out": 18}},
        meta={"cost_usd": 0.0031},
    )
    restored = Event.from_dict(json.loads(json.dumps(event.to_dict())))
    assert restored == event


def test_optional_event_fields_are_omitted():
    keys = Event(i=0, kind="time", site="a.py:1").to_dict()
    assert "meta" not in keys and "res" not in keys and "qual" not in keys


def test_event_signature_is_the_alignment_key():
    event = Event(i=0, kind="tool", site="a.py:1", req={"name": "read_file"})
    assert event.signature == ("tool", "a.py:1", "read_file")


def test_header_records_what_replay_needs_to_check(tmp_path):
    header = build_header("01TEST", argv=["python", "agent.py"], tool_version="9.9")
    data = json.loads(json.dumps(header.to_dict()))
    assert data["v"] == 1
    assert data["run_id"] == "01TEST"
    assert data["argv"] == ["python", "agent.py"]
    assert data["python"] and data["started"].endswith("Z")
    assert data["tool"] == {"name": "reeltime", "version": "9.9"}
    assert Header.from_dict(data).run_id == "01TEST"


def test_git_info_is_none_outside_a_repo(tmp_path):
    assert trace_mod.collect_git(str(tmp_path)) is None


@pytest.fixture
def git_repo(tmp_path):
    """A real one-commit repository, so the git probe is exercised for real."""
    if shutil.which("git") is None:  # pragma: no cover - git is assumed present
        pytest.skip("git is not installed")
    repo = tmp_path / "project"
    repo.mkdir()

    def git(*args):
        subprocess.run(
            ("git",) + args, cwd=str(repo), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    git("-c", "init.defaultBranch=main", "init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "agent.py").write_text("print('hi')\n")
    git("add", "-A")
    git("commit", "-qm", "first")
    return repo


def test_git_sha_is_recorded_inside_a_repo(git_repo):
    # The most common cause of a failed replay is that the code changed since
    # recording, so a null SHA would cost us the ability to say so.
    info = trace_mod.collect_git(str(git_repo))
    assert info is not None
    assert info["sha"] is not None
    assert len(info["sha"]) == 40 and int(info["sha"], 16) >= 0
    assert info["branch"] == "main"
    assert info["dirty"] is False


def test_uncommitted_changes_are_flagged_dirty(git_repo):
    (git_repo / "agent.py").write_text("print('edited')\n")
    assert trace_mod.collect_git(str(git_repo))["dirty"] is True


def test_a_trace_recorded_in_a_repo_carries_the_sha(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    expected = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=str(git_repo),
        stdout=subprocess.PIPE, check=True,
    ).stdout.decode().strip()

    with tape.session(tape_dir=git_repo / ".tape") as run:
        tape.record_event("tool", {"name": "t"})

    header = json.loads(run.path.read_text().splitlines()[0])
    assert header["git"]["sha"] == expected
    assert header["git"]["dirty"] is False


def test_packages_are_recorded_for_what_is_installed():
    packages = trace_mod.collect_packages(("reeltime", "definitely-not-installed"))
    assert "reeltime" in packages
    assert "definitely-not-installed" not in packages


def test_read_trace_returns_header_events_and_footer(recording):
    tape.record_event("tool", {"name": "a"}, {"value": 1})
    tape.record_event("tool", {"name": "b"}, {"value": 2})
    tape.uninstall()

    result = read_trace(recording.path)
    assert result.run_id == recording.run_id
    assert [e.name for e in result.by_kind("tool")] == ["a", "b"]
    assert result.complete and not result.truncated
    assert result.footer["events"] == 2
    assert len(result) == 2 and result[0].i == 0


def test_a_torn_final_line_is_survivable(recording):
    tape.record_event("tool", {"name": "a"}, {"value": 1})
    tape.uninstall()
    with open(recording.path, "a") as handle:
        handle.write('{"i":1,"kind":"tool","re')  # killed mid-write

    result = read_trace(recording.path)
    assert len(result) == 1
    assert result.truncated


def test_a_run_that_never_finished_has_no_footer(recording):
    tape.record_event("tool", {"name": "a"})
    result = read_trace(recording.path)  # still recording
    assert not result.complete
    assert len(result) == 1


def test_a_non_trace_file_is_rejected(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello\n")
    with pytest.raises(TapeError, match="not a trace file"):
        read_trace(path)
