"""Old traces still read, still replay, still enrich.

`tests/fixtures/trace_v0_1_0.jsonl` was recorded by **reeltime 0.1.0**, the
first public release, and is checked in unmodified. Every release since has
added event kinds (`mcp`, `chain`), fields (`qual`), decoders and seams, and
this file is the evidence that none of it broke the format.

That matters more than it sounds. A trace goes in a GitHub issue, or sits in a
CI artifact for a month; if reading one needed the version that wrote it, the
portability claim in design principle 3 would be worth nothing. So the
guarantee is tested against a real artifact from the oldest release rather than
asserted in a README paragraph.

**If this file ever needs regenerating, the guarantee has been broken.** Do not
re-record it -- work out what stopped reading it.
"""

import json
from pathlib import Path

import pytest

import reeltime as tape
from reeltime.core import context as context_mod
from reeltime.core import tracediff
from reeltime.core.decoders import decode
from reeltime.core.trace import SCHEMA_VERSION, read_trace

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "trace_v0_1_0.jsonl"


@pytest.fixture
def old():
    return read_trace(FIXTURE)


def test_the_fixture_really_is_from_the_first_release(old):
    """Guards the premise: a re-recorded fixture would prove nothing."""
    assert old.header.tool == {"name": "reeltime", "version": "0.1.0"}
    assert old.header.v == 1


def test_the_schema_version_has_never_changed():
    """`v` is the compatibility promise. Bumping it is a breaking change.

    If this fails, the change is either wrong or needs a migration path and a
    major version -- it is not a number to update to make a test pass.
    """
    assert SCHEMA_VERSION == 1


def test_an_0_1_0_trace_reads(old):
    assert len(old.events) == 2
    assert [e.kind for e in old.events] == ["llm", "llm"]
    assert old.footer["events"] == 2


def test_an_0_1_0_trace_still_decodes(old):
    """Enrichment is a pure function over recorded bytes, so it applies
    retroactively -- a decoder written after a trace was recorded still reads
    it. That is what `tape reindex` is built on."""
    out = decode(old.events[1])
    assert out is not None
    assert out["res"]["tokens"] == {"in": 88, "out": 12}


def test_an_0_1_0_trace_still_renders_its_context(old):
    ctx = context_mod.from_event(old.events[1])
    assert ctx is not None
    assert len(ctx.messages) == 3
    assert ctx.model == "gpt-4o-mini"


def test_an_0_1_0_trace_still_diffs(old):
    """Alignment folds `llm` into `http`, which is what lets a run recorded
    before a decoder existed line up against one recorded after."""
    result = tracediff.diff(old, old)
    assert result.to_dict()["steps"]


def test_the_context_diff_still_finds_the_truncation(old):
    """The bug this project was built to show, in a two-releases-old trace."""
    before = context_mod.from_event(old.events[0])
    after = context_mod.from_event(old.events[1])
    changes = context_mod.diff(before, after)
    assert any(c.truncated for c in changes)


def test_unknown_fields_do_not_break_reading(tmp_path):
    """Forward compatibility, the other half of the promise.

    A trace written by a *newer* reeltime carries fields this version has never
    heard of. Ignoring them is what lets one person share a trace with a
    colleague who has not upgraded.
    """
    lines = FIXTURE.read_text().splitlines()
    header = json.loads(lines[0])
    header["some_future_field"] = {"nested": True}
    event = json.loads(lines[1])
    event["unheard_of"] = [1, 2, 3]
    event["req"]["also_new"] = "x"

    path = tmp_path / "future.jsonl"
    path.write_text("\n".join(
        [json.dumps(header), json.dumps(event)] + lines[2:]) + "\n")

    trace = read_trace(path)
    assert len(trace.events) == 2
    assert trace.events[0].kind == "llm"


def test_an_unknown_event_kind_is_kept_rather_than_dropped(tmp_path):
    """A kind added later must not make an older reader lose events silently.

    Losing one is worse than failing to interpret it: the count is what tells
    somebody the trace is complete.
    """
    lines = FIXTURE.read_text().splitlines()
    event = json.loads(lines[1])
    event["kind"] = "something_new"
    event["i"] = 99

    path = tmp_path / "newkind.jsonl"
    path.write_text("\n".join(lines + [json.dumps(event)]) + "\n")

    trace = read_trace(path)
    assert len(trace.events) == 3
    assert trace.events[-1].kind == "something_new"
