"""Install the built wheel and record with it, through a symlinked path.

This is a separate gate from the unit suite, and it has to be: the unit suite
runs from a checkout whose path contains no symlinks, so it is structurally
incapable of catching a path-normalisation bug. One such bug -- resolved
package roots compared against unresolved ``co_filename`` -- shipped in 0.1.0
and 0.1.1, silently discarding every ambient event for anyone whose virtualenv
lived under a symlink (which is any venv under ``/tmp`` on macOS).

So: build the wheel, install it into a venv reached *through a symlink*, run a
script that produces three events, and insist on three events. Nothing here
imports reeltime into the test process; everything is a subprocess, exactly as
a user would experience it.

Marked ``wheel`` and deselected by default -- it builds a distribution and
creates a virtualenv, which is too slow for the inner loop. CI runs::

    pytest -m wheel
"""

import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

pytestmark = pytest.mark.wheel

REPO = Path(__file__).resolve().parent.parent

# Three ambient boundaries that are all patched by default. Deliberately not
# datetime.now(): that group is opt-in, so a script using it records two events
# and this gate would fail for a reason that has nothing to do with paths.
AGENT = """
import random, time, uuid
print("id", uuid.uuid4())
print("n", random.random())
print("t", time.time())
"""


@pytest.fixture(scope="session")
def wheel(tmp_path_factory):
    """Build the wheel once for the whole session."""
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out), str(REPO)],
        check=True, capture_output=True,
    )
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


@pytest.fixture
def symlinked_env(tmp_path, wheel):
    """A venv with reeltime installed, reachable only through a symlink.

    ``real/`` holds everything; ``link -> real`` is how we address it. Every
    path handed to the interpreter is therefore a spelling that
    ``Path.resolve()`` would rewrite, which is precisely the condition that
    broke.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert str(link.resolve()) != str(link), "the symlink must actually redirect"

    env_dir = link / "venv"
    venv.EnvBuilder(with_pip=True, symlinks=True).create(str(env_dir))
    python = env_dir / ("Scripts" if os.name == "nt" else "bin") / "python"

    subprocess.run([str(python), "-m", "pip", "install", "--quiet", str(wheel)],
                   check=True, capture_output=True)

    work = link / "work"
    work.mkdir()
    (work / "agent.py").write_text(AGENT)
    return {"python": python, "work": work, "link": link, "real": real}


def run(env, *args, **kwargs):
    return subprocess.run(
        [str(env["python"]), "-m", "reeltime.cli", *args],
        cwd=str(env["work"]), capture_output=True, text=True, check=False,
        timeout=180, **kwargs,
    )


def events_of(env, run_id):
    path = env["work"] / ".tape" / "runs" / "{}.jsonl".format(run_id)
    return [json.loads(line) for line in path.read_text().splitlines()
            if '"i":' in line]


def only_run(env):
    runs = sorted((env["work"] / ".tape" / "runs").glob("*.jsonl"))
    assert len(runs) == 1, runs
    return runs[0].stem


# -- the gate ------------------------------------------------------------


def test_a_three_event_script_records_three_events(symlinked_env):
    """The assertion the unit suite cannot make.

    Before the fix this recorded zero: reeltime's own frames were not
    recognised as internal, so each ambient event was attributed to a file
    inside site-packages and dropped as library noise.
    """
    result = run(symlinked_env, "run", str(symlinked_env["python"]), "agent.py")
    assert result.returncode == 0, result.stderr
    assert "recorded 3 events" in result.stderr, result.stderr

    events = events_of(symlinked_env, only_run(symlinked_env))
    assert sorted(e["kind"] for e in events) == ["rand", "time", "uuid"]


def test_call_sites_point_at_the_users_code_not_at_reeltime(symlinked_env):
    run(symlinked_env, "run", str(symlinked_env["python"]), "agent.py")
    events = events_of(symlinked_env, only_run(symlinked_env))

    for event in events:
        assert "site-packages" not in event["site"], event["site"]
        assert "reeltime" not in event["site"], event["site"]
        assert event["site"].startswith("agent.py:"), event["site"]


def test_the_run_replays_from_the_installed_wheel(symlinked_env):
    recorded = run(symlinked_env, "run", str(symlinked_env["python"]), "agent.py")
    replayed = run(symlinked_env, "replay", only_run(symlinked_env))

    assert replayed.returncode == 0, replayed.stderr
    assert "replayed 3 events" in replayed.stderr, replayed.stderr
    # Same uuid, same random draw, same year: served from the tape.
    assert replayed.stdout == recorded.stdout


def test_a_fork_works_from_the_installed_wheel(symlinked_env):
    recorded = run(symlinked_env, "run", str(symlinked_env["python"]), "agent.py")
    parent = only_run(symlinked_env)

    forked = run(symlinked_env, "fork", parent, "--at", "1")
    assert forked.returncode == 0, forked.stderr
    assert "1 replayed, 2 live" in forked.stderr, forked.stderr
    # The replayed first line is identical; the rest is new.
    assert forked.stdout.splitlines()[0] == recorded.stdout.splitlines()[0]


def test_the_console_script_is_installed_and_runnable(symlinked_env):
    tape = symlinked_env["python"].parent / "tape"
    result = subprocess.run([str(tape), "--version"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "reeltime" in result.stdout


def test_the_wheel_ships_the_bootstrap_shim(wheel):
    import zipfile

    names = zipfile.ZipFile(wheel).namelist()
    # Without this file `tape run` installs nothing and records nothing, and
    # says so only by producing an empty trace.
    assert "reeltime/_bootstrap/sitecustomize.py" in names
    assert "reeltime/py.typed" in names
