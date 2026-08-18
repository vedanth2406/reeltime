"""Round-trip tests for the wire-format helpers.

These inverses are what M3's player will use to put a recorded response back
on the wire, so a body that does not survive encode/decode is a replay that
silently serves something the agent never saw.
"""

import base64
import json

import pytest

from reeltime.core.http import common


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b'{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}',
        b'{ "spaced" : "json" , "n" : 1 }',
        "unicode: ✓ ± é".encode("utf-8"),
        b"plain text, not json",
        b"[1, 2, 3]",
        bytes(range(256)),
        b'{"truncated": ',
        json.dumps({"deep": {"nest": [1, {"a": None}]}}).encode(),
    ],
)
def test_bodies_round_trip_byte_for_byte(data):
    assert common.decode_body(common.encode_body(data)) == data


def test_compact_json_needs_no_raw_copy():
    data = b'{"a":1,"b":2}'
    encoded = common.encode_body(data)
    assert encoded["json"] == {"a": 1, "b": 2}
    assert "raw" not in encoded  # rebuildable, so no second copy is stored


def test_spaced_json_keeps_the_exact_bytes_too():
    data = b'{"a": 1, "b": 2}'
    encoded = common.encode_body(data)
    assert encoded["json"] == {"a": 1, "b": 2}   # readable
    assert base64.b64decode(encoded["raw"]) == data   # and exact


def test_binary_bodies_are_base64():
    data = bytes(range(256))
    encoded = common.encode_body(data)
    assert "json" not in encoded and "text" not in encoded
    assert encoded["size"] == 256


def test_an_absent_body_is_an_empty_payload():
    assert common.encode_body(None) == {}
    assert common.decode_body(None) == b""
    assert common.decode_body({}) == b""


@pytest.mark.parametrize(
    "chunks",
    [
        [],
        [b"one"],
        [b"data: a\n\n", b"data: b\n\n"],
        [b"split ", b"across ", b"reads"],
        ["unicode ✓".encode("utf-8"), b" and more"],
        [b"\x00\x01\xff", b"binary"],
    ],
)
def test_chunk_lists_round_trip_with_their_boundaries(chunks):
    assert common.decode_chunks(common.encode_chunks(chunks)) == chunks


def test_a_text_stream_stays_readable_in_the_trace():
    encoded = common.encode_chunks([b'data: {"a":1}\n\n'])
    assert encoded["encoding"] == "utf-8"
    assert encoded["chunks"] == ['data: {"a":1}\n\n']


def test_one_binary_chunk_moves_the_whole_list_to_base64():
    # Mixing representations within a list would make the ordering ambiguous
    # to anyone reading the trace.
    encoded = common.encode_chunks([b"text", b"\xff\xfe"])
    assert encoded["encoding"] == "base64"
    assert common.decode_chunks(encoded) == [b"text", b"\xff\xfe"]


def test_stream_content_types():
    assert common.is_stream_content_type("text/event-stream")
    assert common.is_stream_content_type("text/event-stream; charset=utf-8")
    assert not common.is_stream_content_type("application/json")
    assert not common.is_stream_content_type(None)


def test_header_pairs_normalise_every_container_shape():
    assert common.header_pairs({"A": "1"}) == [("A", "1")]
    assert common.header_pairs([(b"A", b"1")]) == [("A", "1")]
    assert common.header_pairs(None) == []


def test_content_type_lookup_is_case_insensitive():
    assert common.content_type_of([("Content-Type", "application/json")]) == "application/json"
    assert common.content_type_of([("x", "y")]) is None
