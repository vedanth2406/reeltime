import pytest

from reeltime.core import ids
from reeltime.errors import TapeError


def test_run_ids_are_ulids():
    run_id = ids.new_run_id()
    assert len(run_id) == ids.RUN_ID_LEN
    assert ids.is_run_id(run_id)


def test_run_ids_sort_chronologically():
    early = ids.new_run_id(now_ms=1_700_000_000_000)
    late = ids.new_run_id(now_ms=1_800_000_000_000)
    assert early < late


def test_timestamp_survives_the_round_trip():
    assert ids.timestamp_ms(ids.new_run_id(now_ms=1_755_000_000_123)) == 1_755_000_000_123


def test_ids_are_unique():
    assert len({ids.new_run_id(now_ms=1) for _ in range(2000)}) == 2000


def test_prefix_resolves_to_one_run():
    pool = ["01AAAA", "01BBBB", "02CCCC"]
    assert ids.resolve_prefix("02", pool) == "02CCCC"
    assert ids.resolve_prefix("01aaaa", pool) == "01AAAA"


def test_ambiguous_prefix_refuses_to_guess():
    with pytest.raises(TapeError, match="ambiguous"):
        ids.resolve_prefix("01", ["01AAAA", "01BBBB"])


def test_unknown_prefix_is_an_error():
    with pytest.raises(TapeError, match="no run matches"):
        ids.resolve_prefix("ZZ", ["01AAAA"])
