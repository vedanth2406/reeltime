"""Unit tests for the three-tier matcher.

Hand-built events so each tier can be provoked in isolation; the integration
tests in test_replay.py exercise the same logic through a real agent.
"""

import pytest

from reeltime.core.matching import (
    DEFAULT,
    LOOSE,
    MAX_TIER,
    STRICT,
    MatchIndex,
    Request,
    content_key,
)
from reeltime.core.trace import Event


def event(i, kind="tool", site="agent.py:10", qual="agent.py::main",
          span="root", **req):
    return Event(i=i, kind=kind, site=site, qual=qual, span=span,
                 req={"name": "read_file", "args": req or {"path": "a.md"}})


def request(kind="tool", site="agent.py:10", qual="agent.py::main",
            span="root", **req):
    return Request(kind=kind, site=site, qual=qual, span=span,
                   req={"name": "read_file", "args": req or {"path": "a.md"}})


# -- tier 1 --------------------------------------------------------------


def test_identical_site_and_content_is_a_silent_tier_one_match():
    index = MatchIndex([event(0)])
    match = index.take(request())
    assert match.tier == 1 and match.reason == "" and not match.drifted


def test_a_consumed_event_is_not_offered_twice():
    index = MatchIndex([event(0)])
    assert index.take(request()).tier == 1
    assert index.take(request()) is None


def test_repeated_calls_from_one_site_are_served_in_recorded_order():
    index = MatchIndex([event(0, path="a.md"), event(1, path="b.md")])
    assert index.take(request(path="a.md")).event.i == 0
    assert index.take(request(path="b.md")).event.i == 1


# -- tier 2 --------------------------------------------------------------


def test_changed_content_at_the_same_site_is_tier_two():
    index = MatchIndex([event(0, path="a.md")])
    match = index.take(request(path="DIFFERENT.md"))
    assert match.tier == 2
    assert match.reason == "content changed"
    assert match.drifted


def test_a_shifted_line_number_falls_back_to_the_qualname():
    # What inserting an import above the call site looks like.
    index = MatchIndex([event(0, site="agent.py:10")])
    match = index.take(request(site="agent.py:14"))
    assert match.tier == 2
    assert "line moved" in match.reason
    assert match.event.i == 0


def test_a_shifted_line_and_changed_content_is_still_tier_two():
    index = MatchIndex([event(0, site="agent.py:10", path="a.md")])
    match = index.take(request(site="agent.py:14", path="b.md"))
    assert match.tier == 2
    assert match.reason == "line moved and content changed"


# -- tier 3 --------------------------------------------------------------


def test_a_moved_call_matches_on_content_alone():
    index = MatchIndex([event(0, site="agent.py:10", qual="agent.py::main")])
    match = index.take(request(site="helpers.py:3", qual="helpers.py::fetch"))
    assert match.tier == 3
    assert "call site moved" in match.reason


def test_fuzzy_matching_prefers_the_same_span():
    index = MatchIndex([
        event(0, site="a.py:1", qual="a.py::x", span="root/other"),
        event(1, site="a.py:1", qual="a.py::x", span="root/here"),
    ])
    match = index.take(request(site="moved.py:9", qual="moved.py::y", span="root/here"))
    assert match.event.i == 1


def test_fuzzy_will_cross_spans_if_it_must():
    index = MatchIndex([event(0, site="a.py:1", qual="a.py::x", span="root/elsewhere")])
    match = index.take(request(site="moved.py:9", qual="moved.py::y", span="root/here"))
    assert match.tier == 3
    assert "span moved" in match.reason


def test_the_same_line_in_a_different_function_is_not_a_match():
    # Code moves around; two unrelated calls can land on the same line number.
    # Matching them to each other would be silent wrongness.
    index = MatchIndex([event(0, site="agent.py:10", qual="agent.py::run")])
    match = index.take(request(site="agent.py:10", qual="agent.py::helper"))
    assert match.tier == 3          # only the content vouches for it


def test_a_different_kind_is_never_matched():
    index = MatchIndex([event(0, kind="http")])
    assert index.take(request(kind="tool")) is None


# -- spans ---------------------------------------------------------------


def test_matching_is_scoped_to_the_span():
    index = MatchIndex([
        event(0, span="root/a", path="x"),
        event(1, span="root/b", path="x"),
    ])
    # Two concurrent calls in different spans, arriving in the other order.
    assert index.take(request(span="root/b", path="x")).event.i == 1
    assert index.take(request(span="root/a", path="x")).event.i == 0


# -- strictness ----------------------------------------------------------


def test_strictness_ladder():
    assert MAX_TIER[STRICT] == 1
    assert MAX_TIER[DEFAULT] == 2
    assert MAX_TIER[LOOSE] == 3


# -- content keys --------------------------------------------------------


def test_headers_and_encoding_do_not_affect_the_content_key():
    a = {"method": "POST", "url": "u", "headers": [["date", "monday"]],
         "body": {"json": {"a": 1}, "size": 9, "raw": "zzz"}}
    b = {"method": "POST", "url": "u", "headers": [["date", "tuesday"]],
         "body": {"json": {"a": 1}, "size": 11}}
    assert content_key("http", a) == content_key("http", b)


def test_decoder_added_fields_do_not_affect_the_content_key():
    # Only the recorded side has these; hashing them would miss every call.
    plain = {"method": "POST", "url": "u", "body": {"json": {"m": 1}}}
    enriched = dict(plain, provider="openai", model="gpt-4o", n_messages=3)
    assert content_key("llm", plain) == content_key("llm", enriched)


def test_a_changed_body_changes_the_content_key():
    a = {"method": "POST", "url": "u", "body": {"json": {"prompt": "hi"}}}
    b = {"method": "POST", "url": "u", "body": {"json": {"prompt": "hi!"}}}
    assert content_key("http", a) != content_key("http", b)


def test_key_order_in_a_body_does_not_matter():
    a = {"body": {"json": {"a": 1, "b": 2}}, "method": "POST", "url": "u"}
    b = {"body": {"json": {"b": 2, "a": 1}}, "method": "POST", "url": "u"}
    assert content_key("http", a) == content_key("http", b)


# -- diagnosis -----------------------------------------------------------


def test_candidates_explain_a_near_miss_at_the_same_site():
    index = MatchIndex([event(0, path="a.md")])
    rejections = index.candidates(request(path="b.md"))
    assert rejections
    assert "same call site" in rejections[0].reason


def test_candidates_rank_the_most_relevant_first():
    index = MatchIndex([
        event(5, kind="http", site="other.py:1", qual="other.py::z", span="root"),
        event(6, site="agent.py:10", qual="agent.py::main", path="different"),
    ])
    rejections = index.candidates(request(path="a.md"))
    assert rejections[0].event.i == 6      # same site and kind beats the rest


def test_candidates_say_when_a_site_ran_out_of_recordings():
    index = MatchIndex([event(0)])
    index.take(request())
    rejections = index.candidates(request())
    assert "ran more times than it was recorded" in rejections[0].reason


def test_a_decoded_llm_event_matches_an_http_request():
    # The decoder relabels an http event as llm *after* recording it. The
    # transport asks for http on both sides, so the two must fold together or
    # every enriched LLM call misses its own recording.
    from reeltime.core.matching import kind_key

    assert kind_key("llm") == kind_key("http")
    index = MatchIndex([event(0, kind="llm")])
    assert index.take(request(kind="http")).tier == 1
