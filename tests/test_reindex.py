"""Re-decoding a trace that is already on disk.

The property under test is the one that makes decoders worth being pure: a
decoder written today has to be able to enrich a run recorded before it existed.
"""

import json

import httpx
import pytest

import reeltime as tape
from reeltime.core import decoders
from reeltime.core.reindex import reindex

CHAT = {
    "object": "chat.completion",
    "model": "gpt-4o-mini",
    "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1200, "completion_tokens": 20},
}


@pytest.fixture
def undecoded(tape_dir, server):
    """A run recorded with the decoders switched off, as an old trace would be."""
    url = server.route("/v1/chat/completions", json=CHAT)
    with tape.session(tape_dir=tape_dir, collect_git=False, decode=False,
                      run_id="01OLD") as run:
        for turn in range(3):
            httpx.post(url, json={"model": "gpt-4o-mini",
                                  "messages": [{"role": "user", "content": str(turn)}]})
    return run


def test_an_old_trace_gains_model_tokens_and_cost(undecoded, tape_dir):
    before = tape.read_trace(undecoded.path)
    assert [e.kind for e in before.events] == ["http"] * 3
    assert before.footer["cost_usd"] == 0.0

    result = reindex(undecoded.path, undecoded.blobs)
    assert result.enriched == 3
    assert result.cost_after > 0
    assert "#0 http -> llm" in result.relabelled

    after = tape.read_trace(undecoded.path)
    assert [e.kind for e in after.events] == ["llm"] * 3
    assert after.events[0].req["model"] == "gpt-4o-mini"
    assert after.events[0].res["tokens"] == {"in": 1200, "out": 20}
    assert after.events[0].meta["cost_usd"] > 0


def test_the_footer_totals_are_recomputed(undecoded):
    reindex(undecoded.path, undecoded.blobs)
    footer = tape.read_trace(undecoded.path).footer
    assert footer["tokens"] == {"in": 3600, "out": 60}
    assert footer["cost_usd"] == pytest.approx(3 * (1200 / 1e6 * 0.15 + 20 / 1e6 * 0.60))
    assert footer["kinds"] == {"llm": 3}
    assert footer["reindexed"] is True


def test_reindexing_is_idempotent(undecoded):
    first = reindex(undecoded.path, undecoded.blobs)
    second = reindex(undecoded.path, undecoded.blobs)
    assert first.enriched == 3
    assert second.enriched == 0
    assert "nothing to add" in " ".join(second.notes())


def test_dry_run_changes_nothing_on_disk(undecoded):
    original = undecoded.path.read_text()
    result = reindex(undecoded.path, undecoded.blobs, dry_run=True)

    assert result.enriched == 3
    assert result.dry_run and "would enrich" in result.line()
    assert undecoded.path.read_text() == original


def test_a_decoder_that_raises_leaves_the_trace_intact(undecoded, monkeypatch):
    original = undecoded.path.read_text()
    bad = decoders.Decoder("explodes", lambda event: True, lambda event: 1 / 0)
    monkeypatch.setattr(decoders, "REGISTRY", [bad])

    result = reindex(undecoded.path, undecoded.blobs)
    assert result.enriched == 0
    assert undecoded.path.read_text() == original


def test_a_run_that_needs_nothing_is_left_alone(tape_dir, server):
    url = server.route("/v1/chat/completions", json=CHAT)
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01NEW") as run:
        httpx.post(url, json={"model": "gpt-4o-mini", "messages": []})

    original = run.path.read_text()
    result = reindex(run.path, run.blobs)
    assert result.enriched == 0
    assert run.path.read_text() == original


def test_blob_backed_bodies_are_decoded(tape_dir, server):
    # The bodies of a real agent's turns are externalised; reindex has to
    # resolve them or it would see a blob reference and decode nothing.
    url = server.route("/v1/chat/completions", json=CHAT)
    with tape.session(tape_dir=tape_dir, collect_git=False, decode=False,
                      run_id="01BIG") as run:
        httpx.post(url, json={"model": "gpt-4o-mini",
                              "messages": [{"role": "user", "content": "x" * 20_000}]})

    assert tape.read_trace(run.path).events[0].req["body"].startswith("blob:")
    assert reindex(run.path, run.blobs).enriched == 1
    assert tape.read_trace(run.path).events[0].kind == "llm"


def test_a_reindexed_event_matches_one_enriched_while_recording(tape_dir, server):
    """The two paths share their merge, and this is what that is for."""
    url = server.route("/v1/chat/completions", json=CHAT)
    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}

    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01LIVE"):
        httpx.post(url, json=body)
    with tape.session(tape_dir=tape_dir, collect_git=False, decode=False,
                      run_id="01LATER") as later:
        httpx.post(url, json=body)
    reindex(later.path, later.blobs)

    live = tape.read_trace(tape_dir / "runs" / "01LIVE.jsonl").events[0]
    after = tape.read_trace(tape_dir / "runs" / "01LATER.jsonl").events[0]
    assert live.kind == after.kind
    assert live.req["model"] == after.req["model"]
    assert live.res["tokens"] == after.res["tokens"]
    assert live.meta["cost_usd"] == after.meta["cost_usd"]


def test_an_incomplete_trace_can_still_be_reindexed(tape_dir, server):
    # A crashed run has no footer. It is also the run you most want to read.
    url = server.route("/v1/chat/completions", json=CHAT)
    run = tape.install(tape_dir=tape_dir, collect_git=False, decode=False,
                       run_id="01CRASH")
    httpx.post(url, json={"model": "gpt-4o-mini", "messages": []})
    path, blobs = run.path, run.blobs
    run.engine.writer.close()          # died before the footer
    tape.uninstall()

    result = reindex(path, blobs)
    assert result.enriched == 1
    assert tape.read_trace(path).events[0].kind == "llm"
