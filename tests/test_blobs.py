import json

import pytest

from reeltime.core.blobs import BlobStore, canonical_bytes, is_ref
from reeltime.errors import TapeError


@pytest.fixture
def store(tmp_path):
    return BlobStore(tmp_path / "blobs", threshold=100)


def test_small_values_stay_inline(store):
    assert store.maybe_put("short") == "short"
    assert store.maybe_put({"a": 1}) == {"a": 1}
    assert store.maybe_put(None) is None
    assert store.maybe_put(3.5) == 3.5


def test_large_values_become_refs(store):
    big = "x" * 500
    ref = store.maybe_put(big)
    assert is_ref(ref)
    assert store.get(ref) == big
    assert store.path_for(ref.split(":")[1]).exists()


def test_identical_content_is_stored_once(store):
    payload = {"messages": ["y" * 400]}
    first = store.maybe_put(payload)
    second = store.maybe_put(dict(payload))
    assert first == second
    assert len(list((store.root).iterdir())) == 1
    assert store.bytes_deduped > 0


def test_key_order_does_not_change_the_hash(store):
    a = store.put({"a": "1" * 200, "b": 2})
    b = store.put({"b": 2, "a": "1" * 200})
    assert a == b


def test_externalize_only_touches_oversized_fields(store):
    event = {"model": "gpt-4o-mini", "messages": ["z" * 400]}
    out = store.externalize(event)
    assert out["model"] == "gpt-4o-mini"
    assert is_ref(out["messages"])


def test_resolve_walks_nested_refs(store):
    event = store.externalize({"messages": ["z" * 400], "n": 1})
    assert store.resolve(event) == {"messages": ["z" * 400], "n": 1}
    assert store.refs_in(event) == [event["messages"]]


def test_missing_blob_names_the_problem(store):
    ref = store.maybe_put("q" * 400)
    store.path_for(ref.split(":")[1]).unlink()
    with pytest.raises(TapeError, match="missing"):
        store.get(ref)


def test_blobs_are_valid_json_on_disk(store):
    ref = store.maybe_put({"messages": ["hello" * 100]})
    raw = store.path_for(ref.split(":")[1]).read_bytes()
    assert json.loads(raw) == {"messages": ["hello" * 100]}


def test_canonical_bytes_rejects_non_json_floats():
    with pytest.raises(ValueError):
        canonical_bytes(float("nan"))
