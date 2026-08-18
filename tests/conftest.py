"""Shared fixtures.

Every test gets a throwaway tape directory and a guaranteed-clean process
state afterwards -- these tests patch global modules, so a leaked patch would
corrupt every test that followed it.
"""

import pytest

import reeltime as tape
from reeltime.core import callsite
from reeltime.core import tape as tape_state


@pytest.fixture(autouse=True)
def clean_state():
    yield
    tape_state._reset_for_tests()
    callsite.clear_cache()


@pytest.fixture
def tape_dir(tmp_path):
    return tmp_path / ".tape"


@pytest.fixture
def recording(tape_dir):
    """An installed tape, uninstalled at the end of the test."""
    run = tape.install(tape_dir=tape_dir, collect_git=False)
    try:
        yield run
    finally:
        if not run.closed:
            tape.uninstall()


def read(run):
    """Parse the trace a (closed) run produced."""
    return tape.read_trace(run.path)
