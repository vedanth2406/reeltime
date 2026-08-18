# Competitive analysis: AgentTape

**Subject:** `agenttape` 0.1.8 (PyPI, MIT, ~6,700 LOC, 32 modules)
**Repo:** https://github.com/MITHRAN-BALACHANDER/AgentTape
**Date:** 2026-08-17 · **Analyst:** written while reeltime M1 was complete and M2 unstarted
**Method:** installed into a scratch venv, read the source, and ran a live
record/replay lab against a local HTTP endpoint. No code was copied.

---

## Verdict in one paragraph

AgentTape is a real, well-built project that solves an adjacent problem: it is
**VCR for agent tests**. Its center of gravity is pytest, CI, and cassettes you
commit to git. reeltime is a **debugger**: time travel, fork-from-step-N, and
seeing the exact bytes the model received. Three of AgentTape's core design
choices are correct for testing and disqualifying for debugging — it discards
the recording when the run raises, it fails hard when a prompt changes by one
character, and it *freezes* the clock and RNG rather than recording what they
actually returned. None of those is a bug; they are the right calls for a test
fixture. They are also why it cannot become the tool the spec describes without
reversing its own design. **No milestone should be cut. Two should move, and
three claims need sharpening.**

---

## How this was tested

```
pip install agenttape==0.1.8 httpx        # core has zero dependencies
```

A four-experiment lab: a tiny agent making one HTTP "LLM" call, one `@tool`
call, and reading `random` / `uuid` / `datetime`, against a local server whose
reply changes on every request (so a replayed reply is provably from the
cassette, not the wire).

1. Record, shut the server down, replay → byte-identical output offline. ✅
2. Edit the source between record and replay (6 lines inserted above the call
   site, the tool moved below its caller) → replay still works.
3. Change the prompt by one character → hard failure.
4. Crash the agent mid-session → **no cassette is written at all.**

---

## The nine questions

| Question | AgentTape | reeltime (planned) |
|---|---|---|
| Interception point | `httpx.Client.send` / `AsyncClient.send` + `requests.adapters.HTTPAdapter.send`, ref-counted; **plus** an OpenAI SDK adapter | httpx **transport** (`_transport_for_url`) + `HTTPAdapter.send`; no SDK adapters |
| Replay matching | SHA-256 of the canonicalised request, keyed `(kind, match_key)`; ties served in recorded order. Matchers: `exact`, `ignore_volatile` (default), `ordered`, `custom` | three-tier: exact → positional-with-drift → fuzzy content |
| Survives source edits | **Yes** — no call-site identity is recorded at all | Yes — via `site` + `qual` fallback (tier 2/3) |
| Survives a one-character prompt change | **No** — `UnmatchedInteractionError` | Yes — tier 2 match + drift annotation |
| Fork from step N with a modified event | **No.** No fork, no `--to N`, no stepper, no pdb hook, no resume. Zero step-based controls anywhere in the codebase | M5 |
| Diffs two traces | **Yes, and it is good** — `difflib.SequenceMatcher` over per-step signatures, plus field-level paths (`request.json.messages[0].content: 'x' -> 'y'`) | M6 |
| Records MCP sessions | **No.** An `mcp>=1.9` extra is declared in the metadata but there is not a single occurrence of "mcp" in the source | M9 |
| Ambient nondeterminism | **Frozen, not recorded.** `random.seed(0)`, a pinned base clock, and UUIDs from a fixed namespace — during *recording* too | Recorded: real values, per call site, replayed back |
| Overhead | ~15.9 µs/call recording, ~17.6 µs/call replaying, 454 B/interaction (YAML) | ~20–28 µs/ambient call, 184 B/event (JSONL) |

### The three findings that matter

**1. A crashed run leaves nothing.** `Session.__exit__` is literally
`if exc_type is None: self._maybe_write()`. An agent that raises writes no
cassette; `os._exit()` likewise. Verified empirically — clean exit wrote 3
interactions, exception and kill wrote zero bytes. For a test fixture this is
correct: you do not want a failed run baked into your fixtures. For a debugger
it is fatal, because the run that crashed is the entire artifact. reeltime
flushes every line as it is written and has two tests asserting exactly this.

**2. Freezing is not recording.** AgentTape pins `random` to seed 0 and the
clock to a base timestamp *while recording*, so the agent under observation
never sees the real clock or real entropy. That guarantees cross-run
determinism cheaply, but it changes the run you are trying to study, and it
cannot answer "what did `datetime.now()` return at step 14 of the run that
broke?" — the value was synthetic. It also means a code path change reshuffles
the whole RNG sequence with no per-call-site anchor to detect it.

**3. Content-hash matching is a wall for iteration.** The failure mode is
precisely what the spec predicted in §6. Changing one character of a prompt
produces `UnmatchedInteractionError` and the replay stops. The workflow the
spec is built around — *tweak the prompt, replay the first 13 steps for free,
see what changes at step 14* — is not expressible in AgentTape at any setting.
Their `ordered` matcher ignores content entirely, which is the opposite failure
(it will happily serve the wrong response). There is no middle tier.

Their error message, on the other hand, is excellent, and is the bar `TapeMiss`
should be held to: it prints the incoming request, the closest recorded
request, a field-level diff of the two, and four concrete ways to fix it.

---

## What we do that it doesn't

- **Survive a changed prompt** (tier-2 positional matching + drift annotation).
  This is the single biggest functional gap and the whole debugging loop.
- **Fork from step N with a patched event.** Nothing comparable exists.
- **Step controls** — `--to N`, `--step`, `--pdb`.
- **`--context`**: the exact assembled message array the model received at a
  given step, and what changed since the previous LLM call.
- **Keep the trace when the process dies**, including a torn final line.
- **Record ambient nondeterminism as data**, per call site, rather than
  suppressing it.
- **Stream capture.** AgentTape refuses streaming outright: it warns and passes
  through live while recording, and raises `StreamingReplayError` on replay.
  Given how much production agent code streams, this is a large hole.
- **MCP sessions** as first-class events.
- **Call-site identity in the trace**, which is what makes drift reporting and
  `tape doctor` possible at all.

## What it does that we don't (yet)

- **A pytest plugin** (`@pytest.mark.agenttape`, `--agenttape-record`) — a
  genuinely strong adoption channel.
- **Human-readable, hand-editable YAML cassettes.** Ours are JSONL with blob
  references, which is the right call for large and streaming payloads, but
  theirs are nicer to read in a PR.
- **Framework adapters already shipped**: LangGraph, LangChain callbacks,
  OpenAI SDK.
- **A shipped CLI**: `init`, `record`, `replay`, `inspect`, `timeline`, `diff`,
  `redact`, `validate`, `export` (JSON + OpenTelemetry), `view`, `rm`.
- **A static HTML viewer** and an ASCII waterfall timeline.
- **Recorded exceptions** — a tool that raised replays the exception, with the
  real exception class resolved back when importable. We should copy this idea
  (not the code); M1 records `meta.error` but has no replay semantics for it.
- **Semantic-matcher hook point** for embedding-based matching (a documented
  stub, no implementation).
- **Docs site, CI badges, changelog, 4 months of releases.**

---

## Recommendations

Ordered by how much they change the plan.

### 1. Move MCP forward, out of M9 — do it right after M5

It is the one capability they have publicly *signalled* (an `mcp` extra in
published metadata) and not built. That is a countdown clock, not an open
field. MCP is also cheap for us: §4.4 is a client-session wrapper over
machinery M2 already builds. Being demonstrably first is worth more than the
LangChain adapter it currently shares a milestone with. **Split M9: MCP becomes
M5.5; the LangChain adapter stays late.**

### 2. Demote the web UI (M8) below everything else

It is the most expensive milestone in the plan and now the least
differentiating — they ship a static HTML viewer and an ASCII timeline today.
A better UI does not win an argument that fork and drift-tolerant replay
already win. **Move M8 after M9, and consider shipping the ASCII timeline
inside `tape show` instead, for ~2% of the effort.**

### 3. Keep M6 (diff), but resequence its claim

Their diff is already alignment-based with field-level paths, so "alignment-based
trajectory diffs" is no longer a headline. Ours still wins on one specific
thing: reporting the *divergence point* and what each branch did afterwards
(`step 15 ⋯ divergent from here (A: 6 more events, B: 9 more)`), which theirs
does not attempt. **Keep the milestone, drop it from the elevator pitch, and
make divergence reporting the part we actually build first.**

### 4. Make drift-tolerant replay an executable claim in M3

Write this test the day the matcher lands: record a run, change one character
of the system prompt, replay, assert it *succeeds* with exactly one drift
annotation. That test is the competitive difference in nine lines, it is the
demo, and it is the README's second GIF. Right now it is a design paragraph.

### 5. Hold `TapeMiss` to their error's standard

Closest unconsumed candidate, field-level diff against it, and concrete
remedies — not just a list of nearby events. Ours already carries the call site
and span, which theirs cannot; that plus a field diff is strictly better output
than anything in this space.

### 6. Streaming stays in M2 — and becomes a headline

The instinct to pull it forward is right. They cannot record streaming at all,
and it is the dominant production pattern. "Records and replays streaming
responses chunk-by-chunk" is a sentence they cannot write, available to us four
milestones early.

### 7. One M2 design implication, discovered here

They run an OpenAI SDK adapter *in addition* to HTTP interception, and the
reason is visible in their schema: token counts, cost, and streaming guards are
hard to get from raw bytes. Our §5 event schema wants `tokens` and `cost_usd`
on every `llm` event, but §2.5 forbids SDK-layer interception. Resolve it by
splitting the roles: **the transport stays provider-agnostic and records raw
request/response; a thin provider *decoder* (not interceptor) enriches the
event afterwards by recognising the response shape.** Principle 5 stays intact,
and adding a provider becomes a 30-line pure function with no patching.

### 8. Do not build a pytest plugin before v1

It is their strongest channel and their home turf, and chasing it blurs what we
are. A debugger that also happens to be a fixture library is a worse pitch than
a debugger. Revisit after v1.

### 9. Fix the README's comparison table before it is written

§14.7 names VCR.py, LangSmith, and Braintrust. AgentTape belongs in that table,
and the honest framing is the strongest one available: *AgentTape is a test
fixture library — record once, assert forever, fail loudly when anything
changes. reeltime is a debugger — replay a failed run, change one thing, and
watch what happens next.* Overclaiming here would be caught in a day by anyone
who has used it. Also drop any "no existing tool records agent runs" phrasing
from the pitch; the true and narrower claim is that no existing tool lets you
fork one.

### Not recommended

- **Do not switch to YAML cassettes.** JSONL + content-addressed blobs is right
  for streaming, large contexts, and append-during-crash. Ship `tape show
  --yaml` in M4 for readable output instead.
- **Do not cut M7 (`tape doctor`).** It is unclaimed, it is cheap once replay
  exists, and it is the one feature that sells to someone who never replays.
  Their freeze-based approach means they cannot even detect nondeterminism —
  they suppress it.
