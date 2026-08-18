# reeltime

**Deterministic record/replay for LLM agents.** Record every source of
nondeterminism during a run, then replay the run exactly — offline, instantly,
for free.

> **Status: milestone 2 of 10.** Recording works end to end — HTTP (streaming
> included), local tools, and ambient nondeterminism — with `tape run`,
> `tape ls`, and `tape show`. Replay (`tape replay`) is milestone 3. See
> [Roadmap](#roadmap). Not on PyPI yet.

---

## The problem

Your agent failed at step 14. You re-ran it. Now it fails at step 11. You
cannot reproduce the bug, so you cannot fix it — you can only re-roll and hope.

## The idea

An agent is deterministic *except* at four boundaries. Record what crosses
them and everything in between replays exactly.

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

## Install

Not published yet. From a checkout:

```bash
pip install -e ".[dev]"
```

## Quickstart

Record a script you have not modified at all:

```bash
$ tape run python agent.py
✓ recorded 47 events → .tape/runs/01M09GEPFGRH2RQ8KG1BKWDAPE.jsonl  (43.2s, $0.31)

$ tape ls
RUN             WHEN               EVENTS      DUR     COST  COMMAND
01M09GEPFGRH2R  2026-08-17 23:00       47    43.2s    $0.31  agent.py

$ tape show 01M09G
   0  tool       2ms  agent.py:31    read_file({"path": "notes.md"}) → "remember…"
   1  llm      842ms  agent.py:88    gpt-4o-mini 1204→18 I'll delete b.txt
   2  http      31ms  tools.py:12    GET https://api.weather.com/v1 → 200

$ tape show 01M09G 1        # the whole event, blobs resolved
```

`tape run` needs no import in your code: it injects a `sitecustomize` on
`PYTHONPATH` so reeltime installs at interpreter startup, before your agent
imports anything.

To scope recording yourself instead:

```python
import random, uuid
import reeltime as tape

@tape.tool                                 # local tools become boundaries
def read_file(path: str) -> str:
    return open(path).read()

with tape.session() as run:
    seed = random.random()                 # recorded
    request_id = uuid.uuid4()              # recorded

    with tape.span("plan"):                # groups the events below
        notes = read_file("notes.md")      # recorded
        client.chat.completions.create(…)  # recorded, with tokens and cost

print(run.summary.line())
```

The trace lands in `.tape/runs/<run_id>.jsonl`, one JSON object per line:

```json
{"v":1,"run_id":"01M09J…","started":"2026-08-17T14:22:01Z","argv":["python","agent.py"],"python":"3.13.1","packages":{"openai":"1.51.0"},"git":{"sha":"a3f21c…","dirty":false}}
{"i":0,"kind":"rand","site":"agent.py:5","qual":"agent.py::<module>","span":"root","t_rel":0.0002,"dur_ms":0.0,"req":{"name":"random","args":[],"kwargs":{}},"res":{"value":0.639}}
{"i":3,"kind":"tool","site":"agent.py:12","span":"root/plan","t_rel":0.0009,"dur_ms":0.0,"req":{"name":"read_file","args":{"path":"notes.md"}},"res":{"value":"…"}}
{"end":true,"events":4,"kinds":{"rand":1,"time":1,"tool":1,"uuid":1},"dur_s":0.002,"exit":null}
```

Every line is flushed as it is written, so a run that crashes still leaves a
readable trace of everything up to the crash — which is the run you actually
wanted to look at. A missing footer line is precisely the signal that the
process did not exit cleanly.

## What gets recorded today

| Boundary | How | Milestone |
|---|---|---|
| any `httpx` / `httpx2` / `requests` call | transport shim | **M2 ✅** |
| streaming responses, chunk by chunk | transport shim | **M2 ✅** |
| model, tokens, and cost on LLM calls | provider decoders | **M2 ✅** |
| `@tape.tool` / `wrap` / `wrap_all` | decorator | **M2 ✅** |
| `random.*`, `numpy.random.*` | module-level patch | **M1 ✅** |
| `uuid.uuid1/uuid4` | module-level patch | **M1 ✅** |
| `time.time/monotonic/perf_counter` (+ `_ns`) | module-level patch | **M1 ✅** |
| anything you pass to `record_event()` | explicit | **M1 ✅** |
| `datetime.now/utcnow/today` | subclass swap | **opt-in** — see below |
| MCP `tools/list` and `tools/call` | client wrapper | M5.5 |

## Design notes

**Interception is at the transport, not the SDK.** The shim patches
`Client._transport_for_url` on `httpx` *and* `httpx2` — as of 2026 the OpenAI
SDK has moved to httpx2 while the Anthropic SDK is still on httpx — plus
`requests.adapters.HTTPAdapter.send`. Supporting that split cost one argument,
because both keep the same transport extension point. An SDK-layer interceptor
would have needed rewriting for the migration. Nothing in the recording path
knows a provider exists.

**Model, tokens, and cost come from a decoder, not an interceptor.** A decoder
is a pure function over an already-recorded event: no network, no filesystem,
no import of the provider's SDK. If none matches, the event stays a plain
`http` event with null tokens — an unrecognised provider is not an error. A
decoder that raises is caught and the event is written unenriched; a recording
never fails because of one. Adding a provider is one module in
[`core/decoders/`](src/reeltime/core/decoders/) and one row in
[`pricing.py`](src/reeltime/core/decoders/pricing.py).

**The outermost boundary is the one recorded.** An HTTP call inside a
`@tape.tool` body does not produce a second event, and neither do random draws
or clock reads made there. That is not an optimisation: on replay the tool's
result is served from the tape and its body never runs, so anything recorded
inside it could never be matched.

**Redaction is mandatory, not optional.** Traces are meant to be pasted into
GitHub issues, so every event is scrubbed before it reaches disk —
`Authorization` headers dropped, and payloads scanned for key-shaped strings
(`sk-`, `sk-ant-`, `ghp_`, AWS, JWT, …) which become `<redacted:sk>`. Add your
own with `tape.redact(r"ACME-[A-Z0-9]{24}")`. The end-of-run summary tells you
what was caught. The environment snapshot in the header is an allowlist of
configuration-shaped variables, never the whole environment.

**Large payloads are content-addressed.** Any field over 8KB is written to
`.tape/blobs/<sha256>` and referenced as `"messages":"blob:9f2a…"`. Traces stay
greppable in a terminal, and since agents resend the same growing message array
every turn, deduplication shrinks a real run dramatically.

**Only your own code's clock reads are recorded.** `asyncio` reads
`time.monotonic()` every loop iteration, `logging` timestamps every record, and
`httpx` reads `perf_counter()` twice per request. Recording those would bury the
trace and hand a replayed clock to the event loop's own timeouts. The same
filter applies on replay, so those reads stay live in both directions — which is
consistent, and never a spurious miss. Set `record_library_ambient=True` to see
them.

**Spans, not global order.** Concurrent agents interleave, and wall-clock order
across tasks is not reproducible. Every event carries a span path from a
`ContextVar`, and replay matching (M3) happens *within* a span — so two tool
calls in different spans may replay in either order without breaking.

**Call sites are recorded two ways.** `site` is `agent.py:88`; `qual` is
`agent.py::Planner.step`. Line numbers shift the moment someone adds an import,
so the matcher falls back to the qualified name. This is what lets replay
survive ordinary editing.

## Configuration

Explicit arguments beat environment variables, which beat the nearest
`.tapeconfig`.

```python
tape.install(
    tape_dir=".tape",              # or $TAPE_DIR
    blob_threshold=8192,           # or $REELTIME_BLOB_THRESHOLD
    patch=("random", "uuid", "time", "numpy"),   # add "datetime" to opt in
    http=True,                     # or $REELTIME_HTTP
    decode=True,                   # provider decoders; $REELTIME_DECODE
    record_library_ambient=False,
    redact=[r"ACME-[A-Z0-9]{24}"],
)
```

```json
{ "blob_threshold": 16384, "redact": ["ACME-[A-Z0-9]{24}"] }
```

## What this can't replay

Being precise about the boundary is the point.

- **External state mutation.** If the agent deletes a file, replay does not
  undo it. Replay reproduces the *decisions*, not the world. Run replays in a
  scratch directory or a container.
- **`datetime.now()`, unless you opt in.** `datetime` is a C type, so the only
  way to see `now()` is to swap the module attribute for a subclass — and
  pydantic v2 dispatches on type *identity*, so doing that makes the **real**
  datetime class unrecognisable to it and breaks any library that imported it
  first. The Anthropic SDK stops working entirely. Enable with
  `patch=("random", "uuid", "time", "datetime")` if your stack is not pydantic
  v2; `time.time()` is patched either way and covers most clock reads.
- **`random.Random()` instances and `SystemRandom`.** Only the module-level
  `random.*` functions are patched. An explicitly constructed generator is an
  object you can seed yourself.
- **Streamed `requests` responses.** Reading one to record it would consume the
  caller's stream, so the event is marked `stream_not_captured` rather than
  recording a body nobody saw. Streamed *httpx* responses are captured in full.
- **`numpy.random.default_rng()` generators.** Same reasoning; only the legacy
  global functions are patched, and only if numpy was imported before
  `install()`.
- **`from datetime import datetime` executed before `install()`.** That name
  already points at the real class. Import the module, or install earlier.
- **True thread races.** Same-span concurrent calls replay in recorded order; a
  genuine data race between threads is not reproduced.
- **C-extension nondeterminism.** Anything reading the clock or entropy below
  the Python layer is invisible.
- **Non-`httpx` network stacks.** `aiohttp` and raw sockets are not intercepted
  in v1.

## Roadmap

| M | Scope | Status |
|---|---|---|
| 1 | Trace format, blob store, recorder, ambient patches | ✅ |
| 2 | httpx shim, provider decoders, `@tape.tool`, streaming capture, `tape run/ls/show` | ✅ |
| 3 | Player, three-tier matcher, `TapeMiss`, `tape replay`, streaming re-emission | next |
| 4 | `--context` inspection, `tape reindex`, README, PyPI publish | |
| 5 | `tape fork` with patch grammar | |
| 5.5 | MCP adapter | |
| 6 | `tape diff`, divergence-point reporting | |
| 7 | `tape doctor` | |
| 8 | LangChain callback adapter | |
| 9 | Overhead benchmarks, docs, examples | v1.0 |
| 10 | Web UI | |

Resequenced after a teardown of the nearest competitor — see
[COMPETITIVE.md](COMPETITIVE.md). Streaming moved forward into M2/M3, MCP to
M5.5, and the web UI to last.

## Development

```bash
pip install -e ".[dev]"
pytest
pytest --cov --cov-report=term-missing     # core/ targets 85%+
python examples/m1_ambient.py
```

## Prior art

`agenttape` on PyPI does deterministic record/replay of LLM and tool calls into
YAML cassettes, with a pytest plugin, an alignment-based diff, and an HTML
viewer. It is a *test fixture* library — it discards the recording when the run
raises, fails when a prompt changes by one character, and freezes the clock and
RNG rather than recording them. Those are the right calls for a fixture and the
wrong ones for a debugger. [COMPETITIVE.md](COMPETITIVE.md) is the full
teardown, including what it does better than us.

## Name

`tapedeck` and `agenttape` were both taken on PyPI, so the package is
`reeltime`. The CLI stays `tape`.

## License

MIT
