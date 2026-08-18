# Tapedeck — Deterministic Record/Replay for LLM Agents

> **For the implementing agent:** This is a complete build spec for a Python developer tool. Build in the milestone order in §11 — each ends in a shippable state. This is a library other people will install, so API surface, error messages, and README quality are part of the deliverable, not polish to add later.

---

## 1. What this is

Agent runs are nondeterministic, so failures can't be reproduced. Tapedeck records every source of nondeterminism during a run and replays the run exactly, offline and instantly. Then it lets you fork from any step, diff two runs, and inspect exactly what the model saw.

```
$ tape run python agent.py
  ✓ recorded 47 events → .tape/runs/01J9X2K.jsonl  (43.2s, $0.31)

$ tape replay 01J9X2K --to 14
  ✓ replayed 14 events in 0.18s  ($0.00)

$ tape fork 01J9X2K --at 13 --patch 'llm.system="Be more careful about deletions"'
  ✓ forked → 01J9X3P  (13 replayed, 9 live)

$ tape diff 01J9X2K 01J9X3P
  step 13  llm      system prompt changed
  step 14  tool     delete_file(b.txt)  →  ask_user("confirm delete b.txt?")
  step 15  ⋯ trajectories diverge
```

**The core insight:** an agent is deterministic *except* at four boundaries — LLM calls, tool/network results, randomness, and clock reads. Record what crosses those boundaries and you can replay everything between them exactly.

**Name check:** verify `tapedeck` is available on PyPI before committing. Fallbacks: `agenttape`, `reeltime`, `rewindctl`. CLI stays `tape` either way. *(Resolved 2026-08-17: `tapedeck` and `agenttape` are both taken. The package is `reeltime`; the CLI is `tape`.)*

**Prior art exists, and pretending otherwise is a liability.** `agenttape` on PyPI does deterministic record/replay of LLM and tool calls into YAML cassettes, with a pytest plugin, an alignment-based diff, and a static HTML viewer. It is a *test fixture* library: it discards the recording when the run raises, fails hard when a prompt changes by one character, and freezes the clock and RNG instead of recording them. Those are the right calls for a fixture and the wrong ones for a debugger, which is the gap this tool is built for — but every claim in this spec should be written as if someone who has used AgentTape is reading it. See `COMPETITIVE.md` for the full teardown.

---

## 2. Design principles

These are load-bearing. Violating them makes the tool useless in practice.

1. **Zero-edit adoption.** `tape run python agent.py` must work on an unmodified script. If a user has to restructure their agent, they won't use it.
2. **Replay is free and instant.** No network, no tokens, no cost. This is what makes time-travel possible (§7).
3. **Traces are portable and shareable.** A trace file goes in a GitHub issue. That means secret redaction is mandatory, not optional.
4. **Loud failure over silent divergence.** If replay can't match a call, raise a clear error naming the call site. Never quietly fall through to a live request — that's how a debugger lies to you.
5. **Framework-agnostic core.** Intercept at the HTTP layer, not the SDK layer. Adapters sit on top.

---

## 3. Architecture

```
┌─────────────────── user's agent process ────────────────────┐
│                                                             │
│   agent code (unmodified)                                   │
│        │                                                    │
│        ├── openai / anthropic SDK ──┐                       │
│        ├── httpx / requests ────────┤                       │
│        │                            ▼                       │
│        │                    httpx transport shim ──────┐    │
│        │                                               │    │
│        ├── @tape.tool decorated fns ───────────────────┤    │
│        ├── random / uuid  (patched)  ──────────────────┤    │
│        ├── time / datetime (patched) ──────────────────┤    │
│        │                                               ▼    │
│        │                                          Recorder  │
│        │                                        (or Player) │
│        │                                               │    │
└────────┼───────────────────────────────────────────────┼────┘
         │                                               ▼
         │                              .tape/runs/<run_id>.jsonl
         │                              .tape/blobs/<sha256>
         ▼
   CLI: run · ls · show · replay · fork · diff · doctor · ui
```

**Single global tape** held in a `ContextVar`, with a mode: `RECORD | REPLAY | FORK(n) | OFF`.

---

## 4. Interception

### 4.1 HTTP layer (primary)

Both the OpenAI and Anthropic SDKs are built on `httpx`. Intercept there and you catch every provider for free, plus any tool that makes HTTP calls.

Install a custom `httpx.BaseTransport` / `AsyncBaseTransport` by patching `httpx.Client._transport_for_url` and the async equivalent at import time. Also patch `requests.adapters.HTTPAdapter.send` for older stacks.

**There is more than one httpx.** As of 2026 the OpenAI SDK is built on `httpx2` while the Anthropic SDK is still on `httpx`. Both kept the same transport extension point, so the shim is parameterised by module and supporting the pair costs one argument — whereas an SDK-layer interceptor would have had to be rewritten for that migration. This is the concrete argument for principle 5, and worth stating in the README.

Record: method, URL, headers (redacted), request body, status, response headers, response body, duration.

**Streaming responses are a headline capability, not an edge case.** Most production agent code streams, and the closest competitor (AgentTape) refuses streaming outright — it warns and passes through live while recording, then raises on replay. Recording streams is therefore both the common case and the clearest thing we can do that nobody else can, so it ships in M2 with capture and M3 with replay, not late.

Record the ordered chunk list, not just the assembled body — chunk boundaries must round-trip byte-for-byte. On replay, re-emit chunks. Default to instant emission; `--realtime` re-emits with recorded inter-chunk delays for reproducing timing-sensitive bugs (this matters for anything doing barge-in or early cancellation).

Streaming also carries the token counts: providers send usage in a final chunk, so the provider decoders (§4.6) read the recorded chunk list rather than an assembled body.

### 4.2 Local tools

For functions that don't cross the network:

```python
import tapedeck as tape

@tape.tool
def read_file(path: str) -> str:
    return open(path).read()
```

Also provide `tape.wrap(fn, name=...)` for functions you don't own, and an auto-wrap helper for a dict of tools:

```python
tools = tape.wrap_all({"read_file": read_file, "search": search})
```

### 4.3 Ambient nondeterminism

Patched at `tape.install()`:

- `random.Random` methods on the global instance, plus `numpy.random` if numpy is importable
- `uuid.uuid1` / `uuid.uuid4`
- `time.time`, `time.monotonic`, `time.perf_counter`
- `datetime.datetime.now` / `utcnow` (patch via a subclass assigned to the module attribute — `datetime` is a C type and can't be patched directly). **Opt-in, and off by default as of M2:** pydantic v2 dispatches on type *identity*, so replacing the module attribute makes the real `datetime` class unrecognisable to it, and any library that imported it first stops building its models — the Anthropic SDK breaks outright. A metaclass fixes `isinstance`; nothing fixes identity dispatch. `time.time` is patched either way and covers most clock reads.
- `os.environ` reads are **not** patched, but their values are snapshotted into the trace header for diffing

### 4.4 MCP adapter

Wrap an MCP client session so `tools/list` and `tools/call` are recorded as first-class events with server identity, tool name, and arguments — not as opaque HTTP. Tool discovery results get recorded too, since a server exposing a different tool set is exactly the kind of thing that changes agent behavior between runs.

This is the clearest piece of unclaimed ground, and it is on a clock. As of 2026-08-17 no record/replay tool records MCP sessions — but AgentTape publishes an `mcp>=1.9` optional dependency in its package metadata with no MCP code behind it, which reads as intent. **Resequenced from M9 to M5.5** for that reason: it is cheap once M2's interception exists, and being demonstrably first is worth more than the framework adapter it used to share a milestone with. See `COMPETITIVE.md`.

### 4.5 Redaction

Before writing any event:

- Strip `Authorization`, `x-api-key`, `api-key`, `cookie`, `set-cookie`
- Scan payloads with a configurable regex set for key-shaped strings (`sk-`, `sk-ant-`, `ghp_`, AWS keys, JWTs) and replace with `<redacted:sk>`
- User-extensible via `tape.redact(pattern)` and a `.tapeconfig` file

Emit a warning listing what was redacted, so users know it happened.

### 4.6 Provider decoders

The §5 event schema wants `tokens` and `cost_usd` on every `llm` event, but principle 5 forbids intercepting at the SDK layer to get them. (AgentTape resolves this the other way, running an OpenAI SDK adapter alongside its HTTP interception — which is why it needs a new adapter per provider per major SDK version.)

Split the two roles instead. **The transport stays provider-agnostic and records raw bytes. A decoder recognises the shape afterwards and enriches the event.** A decoder is a pure function over an already-recorded event — no I/O, no network, and no import of the provider's SDK:

```python
# core/decoders/openai.py
def decode(event: Event) -> dict | None: ...
```

- **One module per provider** in `core/decoders/`, registered with a match predicate: URL host plus path shape, plus a key check against the response body. First match wins.
- **No match returns `None`**, and the event keeps `tokens: null` and `cost_usd: null`. That is a valid, replayable event — an unrecognised provider is not an error, it is just an unenriched event.
- **A decoder that raises must never fail the recording.** Catch it, log once at debug level, write the event unenriched. There is a test with a deliberately raising decoder.
- **Decoding runs at write time** for convenience, but the function stays callable over an already-written trace so that a new decoder can enrich old runs. The programmatic path ships in M2; the `tape reindex <run>` CLI verb is **deferred to M4**, where the rest of the inspection surface gets built — there are no old traces worth reindexing before then.
- **Pricing lives in a data file**, not in decoder logic, with a source URL and the date it was checked. Prices change. A model string that is not in the table leaves `cost_usd: null` rather than guessing.
- **Streaming works the same way.** Providers put usage in a terminal chunk, so the decoder reads the recorded chunk list rather than an assembled body.

Adding a provider is then a pure function and a pricing row, with no patching and nothing to break when the vendor ships a new SDK.

---

## 5. Trace format

JSONL, append-only, one event per line, preceded by a header line. Large payloads (>8KB) are content-addressed into `.tape/blobs/<sha256>` and referenced by hash — this keeps traces greppable and dedupes repeated context.

**Header:**
```json
{"v":1,"run_id":"01J9X2K","started":"2026-08-17T14:22:01Z",
 "argv":["python","agent.py"],"cwd":"/home/v/agent",
 "python":"3.11.8","packages":{"openai":"1.51.0","httpx":"0.27.2"},
 "env_snapshot":{"MODEL":"gpt-4o-mini"},"git":{"sha":"a3f21c","dirty":false}}
```

**Event:**
```json
{"i":14,"kind":"llm","site":"agent.py:88","span":"root/plan",
 "t_rel":12.418,"dur_ms":842,
 "req":{"model":"gpt-4o-mini","messages":"blob:9f2a…","temperature":0.7},
 "res":{"content":"I'll delete b.txt","tokens":{"in":1204,"out":18}},
 "meta":{"cost_usd":0.0031}}
```

`kind` ∈ `llm | http | tool | mcp | rand | time | uuid`.

**Recording git SHA and package versions is not decoration** — the most common replay failure is that the user's code changed since recording. Detect it and say so explicitly.

---

## 6. Matching on replay

**This is the hardest part of the project and where most VCR-style tools are weak. Get it right and the tool is genuinely useful; get it wrong and it's a toy.**

Naive index matching breaks the moment the user edits their code — every subsequent event misaligns. Pure content-hash matching breaks the moment a prompt changes by one character.

**Use a three-tier match with configurable strictness:**

1. **Exact** — same call site, same sequence number at that site, same content hash. Silent match.
2. **Positional** — same call site and sequence number, content differs. Match, but record a `drift` annotation and print it at the end (`3 events matched with drifted content`). This is the common case after a prompt tweak, and it's what makes the tool survive real editing.
3. **Fuzzy** — call site missing (code moved), but content hash matches an unconsumed event of the same kind. Match with a warning.

If none hit: raise `TapeMiss` with the call site, the kind, a content preview, and the nearest unconsumed candidates. Never fall through to live.

Modes: `--strict` (only tier 1), default (tiers 1–2), `--loose` (all three).

**Call-site identity** = `<file>:<qualname>:<lineno>` from the stack frame, walking past tapedeck's own frames. Line numbers shift when code is edited, so also store the enclosing function's qualname and fall back to that when the exact line misses.

**Concurrency.** Async agents interleave calls, and wall-clock order isn't reproducible. Assign each event a `span` path via a `ContextVar` set at task boundaries, and match *within* a span rather than globally. Two concurrent tool calls in different spans can then replay in either order without breaking. Document that same-span concurrent calls are matched in recorded order.

---

## 7. Time travel

The elegant part: you don't need reverse execution. Replay is free, so **stepping backward is just replaying forward from zero to N−1**. A 40-second run replays in under a second, which makes scrubbing a timeline feel instantaneous.

```
tape replay <run> --to 14        # stop after event 14
tape replay <run> --step         # interactive stepper
tape replay <run> --to 14 --pdb  # drop into pdb at that point
```

State inspection at any step:

```
tape show <run> 14                    # the event
tape show <run> 14 --context          # the FULL message array sent to the model
tape show <run> 14 --context --diff 12  # what changed in context between steps
```

**`--context` is the sleeper feature.** Most agent bugs are context bugs — a message got truncated, a stale turn stayed in history, a framework silently injected something. Nothing else shows you the exact bytes the model received. Lead with this in the README.

---

## 8. Fork and diff

### 8.1 Fork

Replay to step N, then run live from there:

```bash
tape fork <run> --at 13 --patch 'llm.model=claude-sonnet-4-5'
tape fork <run> --at 13 --patch 'llm.system+="Ask before destructive actions."'
tape fork <run> --at 7  --patch 'tool.read_file.result="<empty file>"'
tape fork <run> --at 13 --edit          # opens $EDITOR on the event JSON
```

Patch expression grammar — keep it small and documented: `<kind>[.<name>].<field>` with `=`, `+=`, and `~=` (regex substitution). Anything more complex, use `--edit`.

Forks record their lineage in the header (`"forked_from":"01J9X2K","fork_at":13`) so the tree is reconstructable.

**Why this matters:** testing a prompt fix normally means re-running the whole agent — slow, expensive, and the bug may not recur. Forking makes the first 13 steps free and identical, so you're testing exactly one variable.

### 8.2 Diff

Not a text diff. Align two traces with a sequence-alignment pass (Needleman–Wunsch over event signatures, where signature = `kind + site + name`), then report structurally:

```
step  0–12  identical (12 events)
step 13  llm     system prompt changed
                 - "You are a file assistant."
                 + "You are a file assistant. Ask before destructive actions."
step 14  tool    delete_file(path="b.txt")
                 → ask_user(prompt="confirm delete b.txt?")
step 15  ⋯ divergent from here (A: 6 more events, B: 9 more events)

cost   A $0.31   B $0.44
tokens A 14,203  B 19,881
```

`--json` for machine output. `--only llm` / `--only tool` to filter.

**Build the divergence point first.** Alignment plus field-level change reporting is table stakes — AgentTape already ships it (`difflib.SequenceMatcher` over per-step signatures, with field paths). What no one else attempts is naming the step where the two trajectories stop being the same run and summarising what each branch did afterwards: `step 15 ⋯ divergent from here (A: 6 more events, B: 9 more events)`. That line is the differentiator and the reason to run `tape diff` at all, so implement it before the per-field diff rather than as a footer.

---

## 9. `tape doctor`

Run the agent twice in record mode and diff the traces to report actual nondeterminism sources:

```
$ tape doctor python agent.py
  running twice…
  ⚠ 3 nondeterminism sources found

  agent.py:88   llm     temperature=0.7 (2 divergent completions)
  agent.py:34   time    datetime.now() — 2 distinct values
  tools.py:12   http    GET api.weather.com — response body differs

  suggestions:
    set temperature=0 for reproducible runs
    inject a clock instead of calling datetime.now() directly
```

This is a standalone reason to install the tool even for someone who never uses replay, which is good for adoption.

---

## 10. Web UI

**Resequenced to last, after v1.0.** This is the most expensive milestone in the plan and, since AgentTape ships a static HTML viewer and an ASCII waterfall today, the least differentiating. A better UI does not win an argument that fork and drift-tolerant replay already win. Ship the ASCII timeline inside `tape show` first — a few percent of the effort for most of the value.

`tape ui` serves a local viewer at `localhost:7654`. FastAPI + a single-page frontend. Keep it plain: this is a debugger, not a product.

- **Timeline** — horizontal event track, colored by kind, width proportional to duration. Click to inspect. Keyboard `←`/`→` to scrub.
- **Inspector** — for an LLM event: full message array (collapsible per message), the completion, token counts, cost. For a tool event: args and result, pretty-printed.
- **Context view** — the assembled prompt at that step, with a toggle to highlight what changed since the previous LLM call.
- **Diff view** — two traces side by side, aligned, divergence highlighted.
- **Fork button** — edit the event inline, hit fork, get a new run.

Dark by default, monospace throughout, no animation beyond instant state changes. It should feel like a profiler.

---

## 11. Build order

Each milestone ships something usable. **Publish to PyPI at M4, not at the end** — early users are the point.

| M | Scope | Deliverable |
|---|---|---|
| **1** | Trace format, blob store, `Recorder`, `tape.install()`, ambient patches (rand/time/uuid) | Traces get written ✅ |
| **2** | httpx transport shim (sync + async), `requests` fallback, provider decoders (§4.6), redaction on the HTTP path, `@tape.tool`, **streaming chunk capture**, `tape run`, `tape ls`, `tape show` | Records real OpenAI/Anthropic agents, streaming included ✅ |
| **3** | `Player`, three-tier matcher, `TapeMiss` errors, `tape replay --to/--step`, streaming re-emission (`--realtime`) | **Replay works — this is the core** ✅ |
| **4** | `--context` inspection, `tape reindex`, README, PyPI publish | v0.1.0 released |
| **5** | Fork + patch grammar, lineage tracking | `tape fork` |
| **5.5** | MCP adapter | Unclaimed ground — take it before someone else does |
| **6** | Alignment-based diff, divergence-point reporting first | `tape diff` |
| **7** | `tape doctor` | Nondeterminism detection |
| **8** | LangChain callback adapter | Framework coverage |
| **9** | Overhead benchmarks, docs site, examples dir | v1.0 |
| **10** | Web UI | `tape ui` |

**Resequenced 2026-08-17 after the AgentTape analysis** (`COMPETITIVE.md`). Streaming capture moved from M9 into M2 and streaming replay into M3, because the closest competitor cannot record streams at all and most production agents use them. MCP moved from M9 to M5.5, because AgentTape ships an `mcp` extra with no code behind it and that lead will not last. The web UI moved from M8 to last, because a viewer is the most expensive thing in the plan and the least differentiating now that a competitor ships one — v1.0 no longer waits on it.

M1–M4 is the minimum viable tool and is achievable in about a week of focused work. Everything after is what makes it worth starring.

---

## 12. Testing

The tool's own test suite is a credibility signal — a debugger with weak tests is a bad joke. Target 85%+ coverage on `core/`.

Essential cases:

- **Round-trip fidelity** — record an agent, replay it, assert the final state and every intermediate LLM input are byte-identical.
- **Replay makes zero network calls** — assert via a transport that raises on any real request.
- **Code-edit resilience** — record, insert a line above the call site, replay, assert tier-2 positional match with a drift warning.
- **TapeMiss is raised, not swallowed** — delete an event from the trace, assert the error names the right call site.
- **Concurrency** — an async agent with 3 parallel tool calls in separate spans replays correctly under shuffled completion order.
- **Streaming** — chunk boundaries preserved.
- **Redaction** — plant an `sk-` key in a payload, assert it never reaches disk.
- **Fork isolation** — forking at step N leaves the parent trace unmodified.

Ship a `examples/` directory with three runnable agents (plain OpenAI SDK, LangChain, MCP) that double as integration tests.

---

## 13. Limitations — state these plainly

A section titled **"What this can't replay"** in the README. Being precise about the boundary is what makes engineers trust the tool.

- **External state mutation.** If the agent deletes a file, replay doesn't undo it. Replay reproduces the *decisions*, not the world. Recommend running replays in a scratch directory or container.
- **True thread races.** Same-span concurrent calls replay in recorded order; a genuine data race between threads isn't reproduced.
- **C-extension nondeterminism.** Anything reading the clock or entropy below the Python layer is invisible.
- **Code drift.** If the agent's logic changed materially since recording, replay will `TapeMiss` — by design.
- **Non-`httpx` network stacks.** `aiohttp` and raw sockets aren't intercepted in v1.

---

## 14. README requirements

The README *is* the marketing. Structure it in this order:

1. **A 20-second terminal GIF**: run → fail → `tape replay --to 14` → `tape show 14 --context` → the bug is visible. No narration needed.
2. **The problem, in three sentences.** "Your agent failed at step 14. You re-ran it. Now it fails at step 11. This fixes that."
3. **The three things nothing else does**, as three bullets, above the fold:
   - **Fork from step N.** Change one thing at step 13 and watch what happens at 14. The first 13 steps are free and identical.
   - **Replay survives an edited prompt.** Change a character and replay still runs, reporting drift instead of failing. Content-hash tools cannot do this by construction.
   - **Streaming is recorded chunk by chunk**, and replays with the boundaries intact. The nearest competitor refuses streaming outright.
4. **Install and 5-line quickstart.** `pip install reeltime` then `tape run python agent.py`.
5. **The four boundaries diagram** from §1 — it makes the whole approach click instantly.
6. **Overhead numbers.** Record mode adds X ms/call and Y MB/1000 events. Replay is Zx faster than live and costs $0. Measure these honestly.
7. **What this can't replay** (§13).
8. **Comparison table** — VCR.py, LangSmith, Braintrust, **and `agenttape`**. The first three are observability/eval platforms doing a different job, and saying so accurately builds more trust than overclaiming. AgentTape is the one real head-to-head, so give it the most honest row on the page: name what it does better (pytest integration, hand-editable YAML cassettes, recorded exceptions that re-raise with the real class, a shipped CLI and viewer) before naming what it cannot do (fork, drift-tolerant replay, streaming, MCP, and keeping the trace when the run crashes). Anyone who has used it will check, and a table that reads as fair is worth more than one that reads as marketing.

---

## 15. Resume line

> **Tapedeck** — Open-source deterministic record/replay debugger for LLM agents (`pip install tapedeck`). Intercepts nondeterminism at four boundaries via an httpx transport shim and ambient patching, enabling byte-exact offline replay, fork-from-step-N counterfactuals, and alignment-based trajectory diffs. Three-tier call matching survives source edits; MCP and LangChain adapters included.

Add stars, downloads, and any external contributor once they exist. Those numbers are the whole point of shipping this publicly — a library strangers use is a stronger signal than any self-reported metric.
