# reeltime

**A deterministic record/replay debugger for LLM agents.** Record a run once,
then replay it offline, instantly, for free — and see the exact bytes the model
received.

[![PyPI](https://img.shields.io/pypi/v/reeltime.svg)](https://pypi.org/project/reeltime/)
[![Python](https://img.shields.io/pypi/pyversions/reeltime.svg)](https://pypi.org/project/reeltime/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

![reeltime: record an agent, replay it offline, and see what the model actually read](https://raw.githubusercontent.com/vedanth2406/reeltime/main/demo.gif)

<details open>
<summary>The same session as text</summary>

```console
$ tape run python truncation_bug.py
Q1: is report_00.pdf there?  -> Yes — report_00.pdf is in the listing.
Q2: is invoice.pdf there?    -> No, invoice.pdf is not in the listing.

Q2 is wrong: invoice.pdf IS in the listing.
Run:  tape show last 1 --context --diff 0

note: mock provider, 2 events -- little latency to skip, so replay saves ~1s here.
      examples/m3_replay_speed.py measures ~80x on an 8-turn agent at 400ms/call.

✓ recorded 2 events → .tape/runs/01M0B3V68D0THT474YMFV0R2SQ.jsonl  (1.5s, <$0.0001)

$ tape replay
Q1: is report_00.pdf there?  -> Yes — report_00.pdf is in the listing.
Q2: is invoice.pdf there?    -> No, invoice.pdf is not in the listing.

✓ replayed 2 events in 0.72s  ($0.00)  [2× faster than the recorded run]
  wall clock 0.86s including startup; the recorded run took 1.51s

$ tape show last 1 --context --diff 0
context diff · event 0 → event 1 · gpt-4o-mini
  3 messages, 866 chars  →  3 messages, 353 chars   (+0 messages, -513 chars)

  = [0] system     unchanged · 43 chars
  ~ [1] user       CHANGED · 791 → 280 chars (-511, TRUNCATED (kept the first 280 chars))
        -  report_10.pdf  (50 KB)
        -  report_11.pdf  (51 KB)
        -  report_12.pdf  (52 KB)
        -  report_13.pdf  (53 KB)
        ⋯ 16 more diff lines ⋯
        -  invoice.pdf  (70 KB)
        +  report_10

  ~ [2] user       CHANGED · 32 → 30 chars (-2)
        -Is report_00.pdf in the listing?
        +Is invoice.pdf in the listing?

2 changed · 1 unchanged
```

</details>

The model was never wrong. `invoice.pdf` had been truncated out of its context
one line before the question that asked about it.

(That demo runs against an embedded mock, so it has almost no latency to skip
and replay only saves about a second. The ~80× figure below is measured on
[a realistic agent](examples/m3_replay_speed.py) paying 400 ms per call.)

---

## The problem

Your agent failed at step 14. You re-ran it, and now it fails at step 11.
Nothing you can reproduce, so nothing you can fix — only re-roll and hope.

## What this does about it

- **Replay is instant, offline, and free.** $0.00 and zero network calls —
  ~80× faster on an 8-turn agent paying 400 ms per call ([the
  benchmark](examples/m3_replay_speed.py)), and the ratio grows with the
  latency you were paying. That is what makes stepping and scrubbing possible
  at all.
- **Streaming is recorded and replayed chunk by chunk**, boundaries byte-exact,
  with `--realtime` to reinstate the recorded gaps. Every other tool in this
  space refuses streaming outright.
- **`--context` shows the full message array the model received**, collapsed
  where it is long, and diffs it between two calls so an injection or a
  truncation is impossible to miss. Most agent bugs are context bugs, and
  nothing else surfaces the exact bytes.

## Install

```bash
pip install reeltime
```

Nothing is required at runtime: the core is standard library only. `httpx`,
`httpx2`, and `requests` are patched if you have them.

## Quickstart

Record a script you have not modified at all:

```bash
tape run python agent.py     # records; your code is untouched
tape ls                      # what you have recorded
tape replay <run>            # re-run it offline, free
tape show <run> 14 --context # what the model actually read at step 14
```

`tape run` needs no import in your code — it injects a `sitecustomize` on
`PYTHONPATH`, so recording starts before your agent imports anything.

To scope recording yourself instead:

```python
import reeltime as tape

@tape.tool                                  # local tools become boundaries
def read_file(path: str) -> str:
    return open(path).read()

with tape.session() as run:
    with tape.span("plan"):                 # groups events; replays order-free
        notes = read_file("notes.md")
        client.chat.completions.create(...) # recorded, with tokens and cost

print(run.summary.line())
```

## How it works

An agent is deterministic *except* at four boundaries. Record what crosses them
and everything in between replays exactly.

```
┌──────────────── your agent, unmodified ────────────────┐
│                                                        │
│   ① LLM calls          ─────┐                          │
│   ② tool / network     ─────┤                          │
│   ③ random / uuid      ─────┤──►  Recorder ──► trace   │
│   ④ clock reads        ─────┘                          │
│                                                        │
└────────────────────────────────────────────────────────┘
```

Nothing else in the process can differ between two runs. That is the whole
trick, and it is why replay costs nothing: there is no model to call, because
every answer is already on the tape.

## The context view

```console
$ tape show 01M0AX2W 0 --context
event 0 · llm · gpt-4o-mini · examples/truncation_bug.py:97 (main.<locals>.ask)
3 messages · 866 chars of context · 216 in / 12 out tokens · <$0.0001
temperature 0

── [0] system · 43 chars ─────────────────────────────────────────────────────
  Answer only from the listing you are given.

── [1] user · 791 chars ──────────────────────────────────────────────────────
  Directory listing:
    report_00.pdf  (40 KB)
    report_01.pdf  (41 KB)
    report_02.pdf  (42 KB)
    report_03.pdf  (43 KB)
    report_04.pdf  (44 KB)
    report_05.pdf  (45 KB)
  ⋯ elided 550 chars · lines 8-29 of 32 ⋯
    report_28.pdf  (68 KB)
    report_29.pdf  (69 KB)
    invoice.pdf  (70 KB)

── [2] user · 32 chars ───────────────────────────────────────────────────────
  Is report_00.pdf in the listing?

── completion ────────────────────────────────────────────────────────────────
  Yes — report_00.pdf is in the listing.
```

Long messages collapse from the middle, keeping head *and* tail, because the
end of a long message is where a truncation shows itself. The marker states
both how many characters were elided and which lines. `--full` prints
everything.

`--context --diff M` aligns the two message arrays with a sequence-alignment
pass, so a message injected at the front does not report everything after it as
changed, and labels each difference `INJECTED`, `DROPPED`, `CHANGED`, or
`TRUNCATED`. Anthropic's top-level `system` field is hoisted to position 0 —
it is part of what the model read, and it is the field people most often get
wrong.

## Replay

```bash
tape replay <run>              # re-run the recorded command against the tape
tape replay <run> --to 14      # stop after event 14
tape replay <run> --step       # pause before each event
tape replay <run> --strict     # only exact matches
tape replay <run> --loose      # also match on content hash alone
tape replay <run> --realtime   # re-emit stream chunks with their recorded gaps
```

A replayed `@tape.tool` never executes its body, which is what makes replaying
an agent that deletes files or charges cards safe. Recorded exceptions are
raised again — HTTP and tool alike — because a replay in which a failed call
now succeeds is a replay of a different run.

### The three-tier matcher

Index matching breaks the moment you edit your code. Content-hash matching
breaks the moment you change a prompt by one character — which is exactly the
edit you make while debugging. So identity and content are kept separate:

| Tier | Rule | Result |
|---|---|---|
| 1 | same call site, same sequence number there, same content hash | silent |
| 2 | the line moved (enclosing function still matches), or the content differs | **matched**, reported as drift |
| 3 | call site gone entirely, content hash matches an unconsumed event | matched, warned |

`--strict` accepts tier 1, the default accepts 1–2, `--loose` accepts all three.
Tier 2 is the one that matters: it is what lets you tweak a prompt, replay
anyway, and watch what changes downstream.

Nothing ever falls through to a live call. When a call cannot be matched,
replay stops and says why each nearby recording was rejected:

```console
no recorded tool event matches this call

  at        agent.py:91  (in Planner.step)
  span      root/plan
  sent      {"args":{"path":"b.txt"},"name":"delete_file"}

  nearest unconsumed events, and why each was rejected:
    #14   tool  agent.py:88                same call site, content differs — would match without --strict
    #22   tool  tools.py:12                same kind and span, different call site

  matching is 'strict'. Drop --strict to allow drifted content, or re-record.
```

Every drifted or fuzzy match is summarised at the end of the run. A match
nobody mentions is silent divergence, which is the one thing this tool must
never do.

## Why interception is at the transport layer

On **2026-08-18** the OpenAI Python SDK (3.2.0) is built on `httpx2` 2.10, while
the Anthropic SDK (0.122.0) is still on `httpx` 0.28. reeltime intercepts at
`Client._transport_for_url` — httpx's own documented extension point — so
supporting that split cost **one constructor argument**, because both libraries
kept the same hook.

An interceptor that patched the SDKs instead would have needed a rewrite for
that migration, and another one at the next. Nothing in the recording path
knows a provider exists; model, tokens, and cost are added afterwards by pure
functions over the recorded bytes ([`core/decoders/`](src/reeltime/core/decoders/)).
Adding a provider is one module and one row in a pricing table, with nothing
patched.

## Numbers

Measured on the included benchmark (`python examples/m3_replay_speed.py`) — an
8-turn agent with 400 ms of latency per call, on an M-series Mac:

| | wall clock | cost | network |
|---|---|---|---|
| record | 3.39 s | $0.0015 | 8 calls |
| replay | **0.04 s** | **$0.00** | **none** |

- **~80× faster** replay, and the ratio grows with the latency you were paying.
- **~2 ms** added per recorded HTTP event.
- **20–30 µs** added per ambient read (`random`, `uuid`, clock).
- **~184 bytes** per event on disk; payloads over 8 KB are content-addressed
  into `.tape/blobs/` and deduplicate across turns.

## What this can't replay

Being precise about the boundary is the point.

- **External state mutation.** If the agent deleted a file, replay does not
  put it back. Replay reproduces the *decisions*, not the world. Run replays in
  a scratch directory or a container.
- **The agent's own `time.sleep`.** Replay skips network latency, not code that
  deliberately waits. An agent that sleeps 30 s still sleeps 30 s.
- **`datetime.now()`, unless you opt in.** `datetime` is a C type, so seeing
  `now()` means replacing the module attribute with a subclass — and pydantic
  v2 dispatches on type *identity*, so doing that makes the **real** datetime
  class unrecognisable to it and breaks any library that imported it first. The
  Anthropic SDK stops working entirely. Enable with
  `patch=("random", "uuid", "time", "datetime")` if your stack is not pydantic
  v2; `time.time()` is patched either way and covers most clock reads.
- **True thread races.** Concurrent calls in the same span replay in recorded
  order. Put concurrent work in separate `tape.span()`s and the order stops
  mattering; a genuine data race between threads is not reproduced.
- **JSON body whitespace.** A parsed JSON body is stored as JSON, not as the
  original bytes. Keys and values survive; formatting does not. Keeping the
  exact bytes meant keeping a base64 copy that redaction could not scrub, which
  is a bad trade for whitespace no parser can see.
- **Binary bodies** are stored as base64 and cannot be scanned for secrets.
  Text and JSON bodies are scrubbed in full.
- **`random.Random()` instances, `SystemRandom`, and
  `numpy.random.default_rng()`.** Only the module-level functions are patched;
  an explicitly constructed generator is an object you can seed yourself.
- **C-extension nondeterminism.** Anything reading the clock or entropy below
  the Python layer is invisible.
- **Non-`httpx` network stacks.** `aiohttp` and raw sockets are not intercepted.

## How this compares

| | reeltime | [agenttape](https://pypi.org/project/agenttape/) | VCR.py | LangSmith / Braintrust |
|---|---|---|---|---|
| **Job** | local debugger | test fixtures | HTTP fixtures | hosted observability & eval |
| Replay offline | ✅ | ✅ | ✅ | ✕ |
| Survives an edited prompt | ✅ tier 2 + drift report | ✕ hard fail | ✕ | n/a |
| Streaming record/replay | ✅ chunk-exact | ✕ refused | partial | n/a |
| Full context inspection | ✅ `--context`, `--diff` | inspect / timeline / HTML viewer | ✕ | ✅ in the UI |
| Keeps the trace when the run crashes | ✅ flushed per event | ✕ discards it | n/a | ✅ |
| Ambient nondeterminism | recorded, per call site | *frozen* (seeded, pinned clock) | ✕ | ✕ |
| Step controls (`--to`, `--step`) | ✅ | ✕ | ✕ | ✕ |
| pytest integration | ✕ | ✅ | ✅ | partial |
| Hand-editable fixture files | JSONL + blobs | ✅ readable YAML | ✅ YAML | n/a |
| Recorded exceptions re-raised | ✅ | ✅ | partial | n/a |

**AgentTape is the closest thing to this and it is a good project** — a shipped
CLI, an HTML viewer, an alignment-based diff, a pytest plugin, and hand-editable
YAML cassettes. It is aimed at a different job: it builds *test fixtures*, so it
deliberately discards a recording when the run raises, fails hard when a prompt
changes, and freezes the clock and RNG rather than recording them. Those are the
right calls for a fixture library and the wrong ones for a debugger. If you want
offline agent tests in CI, use it. If you want to understand why one run failed,
use this. Full teardown, including what it does better: [COMPETITIVE.md](COMPETITIVE.md).

LangSmith and Braintrust are hosted observability and evaluation platforms.
Different job again: they show you aggregate behaviour across many runs; this
reproduces one run byte for byte on your laptop.

## Design notes

**Redaction is mandatory, not optional.** Traces are meant to be pasted into
issues, so every event is scrubbed before it reaches disk — sensitive headers
dropped by name, key-shaped values replaced (`sk-`, `sk-ant-`, `ghp_`, AWS,
JWT, …), blobs included. Add your own with
`tape.redact(r"ACME-[A-Z0-9]{24}")`; the end-of-run summary reports what was
caught. The header's environment snapshot is an allowlist of
configuration-shaped variables, never the whole environment.

**Traces survive the crash you are debugging.** Every event is flushed as it is
written, so a run that dies leaves everything up to the moment it died. A
missing footer line is precisely how you know it did not exit cleanly.

**The outermost boundary is the one recorded.** An HTTP call inside a
`@tape.tool` body does not produce a second event, and neither do random draws
made there. On replay that body never runs, so anything recorded inside it could
never be matched.

**Only your own code's ambient reads are recorded.** `asyncio` reads
`time.monotonic()` every loop iteration and httpx reads `perf_counter()` twice
per request. The same filter applies on replay, so those stay live in both
directions — consistent, and never a spurious miss.

## Configuration

Explicit arguments beat environment variables, which beat the nearest
`.tapeconfig`.

```python
tape.install(
    tape_dir=".tape",            # or $TAPE_DIR
    blob_threshold=8192,         # or $REELTIME_BLOB_THRESHOLD
    patch=("random", "uuid", "time", "numpy"),   # add "datetime" to opt in
    http=True,                   # or $REELTIME_HTTP
    decode=True,                 # provider decoders; $REELTIME_DECODE
    record_library_ambient=False,
    redact=[r"ACME-[A-Z0-9]{24}"],
)
```

```json
{ "blob_threshold": 16384, "redact": ["ACME-[A-Z0-9]{24}"] }
```

## Examples

Three runnable agents, all covered by the test suite — see
[examples/](examples/). The two SDK examples import nothing from reeltime,
which is the zero-edit claim made concrete.

## Roadmap

| M | Scope | Status |
|---|---|---|
| 1 | Trace format, blob store, recorder, ambient patches | ✅ |
| 2 | httpx shim, provider decoders, `@tape.tool`, streaming, `run/ls/show` | ✅ |
| 3 | Player, three-tier matcher, `TapeMiss`, `tape replay` | ✅ |
| 4 | `--context`, `tape reindex`, examples, **v0.1.0** | ✅ |
| 5 | `tape fork <run> --at N --patch …` | next |
| 5.5 | MCP adapter | |
| 6 | `tape diff`, divergence-point reporting | |
| 7 | `tape doctor` — find a run's nondeterminism sources | |
| 8 | LangChain callback adapter | |
| 9 | Overhead benchmarks, docs site | v1.0 |
| 10 | Web UI | |

`tape fork` is the one to watch: replay to step 13, change one thing, and run
live from there — so testing a prompt fix costs one step instead of a whole run.

## Development

```bash
git clone https://github.com/vedanth2406/reeltime
cd reeltime
pip install -e ".[dev]"
pytest                                  # 361 tests
pytest --cov --cov-report=term-missing  # core/ is at 93%
python examples/m3_replay_speed.py      # the benchmark above
```

Verified on Python 3.9 through 3.13.

## Prior art

`tapedeck` and `agenttape` were both taken on PyPI, so the package is
`reeltime`. The CLI is `tape`. See [How this compares](#how-this-compares) for
what already exists in this space and why this is a different tool.

## License

MIT — see [LICENSE](LICENSE).
