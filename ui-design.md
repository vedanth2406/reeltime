# M11 — `tape ui`

Design, for review before any frontend is written. Screen inventory, what each
screen renders, where its data comes from, and what is deliberately excluded.

The build spec is [`tapedeck-spec.md`](tapedeck-spec.md) §10; this supersedes
it in two places, both noted below.

---

## The position

**A viewer for what only reeltime knows.** Not an observability dashboard.

LangSmith and Braintrust ship polished UIs backed by teams, and AgentTape ships
a static HTML viewer. Competing on breadth loses on every axis at once. But
none of them can render a fork tree, a divergence point, a context diff with
truncation called out, or a doctor finding grouped by call site — because none
of them *have* those things. That is the whole surface this milestone builds,
and nothing else.

The test for whether a feature belongs in `tape ui`: **could a general
observability tool render this from its own data?** If yes, it is out of scope
however nice it would be. Aggregate cost charts, latency percentiles, run
volume over time, and model-usage breakdowns are all out — not because they are
hard, but because they are somebody else's product and reeltime has one
developer's traces on one laptop.

---

## Two decisions that depart from spec §10

**1. No FastAPI. Standard library only.**

`pyproject.toml` says `dependencies = []`, and that is load-bearing rather than
incidental — it is why `pip install reeltime` cannot break someone's
environment, and it is quoted in the README. Spec §10 predates that property
mattering.

So the server is `http.server.ThreadingHTTPServer` and the frontend is **one
HTML file with inlined CSS and JS**: no build step, no npm, no CDN, no
framework. A debugger that needs a toolchain to look at a crash is a debugger
you will not open. It also means `tape ui` works with no network at all, which
matters because the traces it reads came from a machine that may be offline.

The cost is real and worth stating: no component library, no virtual-DOM
diffing, hand-rolled keyboard handling. For a five-screen read-only viewer with
a fixed layout, that is a good trade. If the UI ever needs more than this, that
is the signal it has drifted out of scope.

**2. No fork button in M11.**

Spec §10 lists "edit the event inline, hit fork, get a new run." That is
excluded here. Forking *executes the user's agent* — a subprocess, live
credentials, real cost, and a run that can hang or half-write. That is a
different risk surface from a read-only viewer, and it is the one part of the
UI that could damage something. `tape fork` already does it well from a shell
where the output is visible.

The UI instead makes forks *legible*: the tree screen shows lineage, and every
screen that could motivate a fork shows the exact `tape fork` command to copy.

---

## Architecture: the UI is a second renderer

The single most important structural fact, and the reason this milestone is
smaller than it looks:

**Every capability already separates computation from rendering.**

| Capability | Pure computation (reused as-is) | Terminal renderer (not reused) |
|---|---|---|
| Context view | `context.from_event()` → `Context` | `context.render()` |
| Context diff | `context.diff()` → `List[Change]` | `context.render_diff()` |
| Run diff | `tracediff.diff()` → `TraceDiff` | `tracediff.render()` |
| Doctor | `doctor.analyse()` → `Report` | `doctor.render()` |
| Chain tree | `chain` events' `path` / `depth` fields | node tree in `tape show` |
| Lineage | `trace.header.forked_from` / `fork_at` | `tape ls` |

The server calls the **same function the CLI calls** and serialises the
dataclass to JSON. It computes nothing of its own. Three consequences:

- **"No new event data, no recording changes" is satisfied by construction**,
  not by discipline. The UI imports from `core/` and never writes.
- A UI bug is a rendering bug. If the number is wrong, it is wrong in `tape
  show` too, and there is already a test for that.
- **The UI cannot drift from the CLI**, and that is testable — see Testing.

```
 browser  ──HTTP──▶  ThreadingHTTPServer (127.0.0.1:7654)
                          │
                          ├─ GET /                      → index.html (one file)
                          └─ GET /api/…                 → json.dumps(dataclass)
                                   │
                                   ▼
                          core/  (trace, context, tracediff, doctor)
                                   │
                                   ▼
                          .tape/runs/*.jsonl + .tape/blobs/
```

---

## Screen inventory

Six routes. Five are the capabilities named above; the first is navigation.

### 0. Runs index — `/`

The entry point, and deliberately thin.

A table, the same columns `tape ls` prints: run id, when, events, duration,
cost, argv, and lineage. A forked run shows `← 01M0BDHF8JK3MT@1` in its own
column, and the parent is a link. Rows are filterable by a single text box that
matches argv and run id.

*Renders from:* each trace's header and footer. No event parsing, so it stays
fast with hundreds of runs.

**Not here:** totals across runs, charts, "cost this week." That is the
dashboard this is not.

### 1. Run view — `/run/<id>` — the spine

Where the arrow keys live. Three regions:

**Timeline strip** (top, full width). One horizontal track. Each event is a
block, x-position by `t_rel`, **width proportional to `dur_ms`**, colour by
`kind` (`llm`, `http`, `tool`, `mcp`, `chain`, `rand`, `time`, `uuid`). The
selected event is outlined, not animated. Ambient events (`rand`/`time`/`uuid`)
are near-zero duration, so they render as 2px ticks on a sub-track rather than
invisible slivers.

Above ~2,000 events the track buckets by time and a block becomes "n events" —
scrubbing then steps bucket to bucket until you zoom. Traces that large are
real: an agent in a loop reads the clock hundreds of times.

**Inspector** (centre). The selected event, in one of three modes:

- `raw` — the event JSON, pretty-printed, blobs resolved and marked
  `blob:sha256…` so you can see what was externalised.
- `context` — the context view (screen 2).
- `diff` — the context diff (screen 2).

**Status bar** (bottom). Event `i` of `n`, kind, call site, duration, model,
tokens, cost — and, when cost is null, *why* (`no price row: Claude-on-Bedrock
routing tier`), because a blank cost otherwise reads as a bug.

*Renders from:* `Trace.events`, plus `decoders` enrichment already on the event.

### 2. Context view and context diff — inside `/run/<id>`

**The sleeper feature, and the one to get right.** Most agent bugs are context
bugs, and nothing else shows the exact bytes the model received.

**Context mode** renders a `Context`: the full message array, one collapsible
card per message, with role, char count, and tool calls broken out as
structured `ToolCall` rows rather than buried in JSON. Header carries model,
params, tool names, tokens, cost. The completion is rendered below the input,
visually separated — it is the one thing that came *back*.

**Diff mode** picks a baseline (default: the previous LLM event; any earlier
event selectable) and renders `List[Change]`:

| `Change.kind` | Rendering |
|---|---|
| `same` | collapsed to one line: role, chars |
| `added` | full, green gutter |
| `dropped` | full, red gutter |
| `changed` | word-level inline diff |

**Truncation is the headline case, and it gets its own treatment.**
`Change.truncated` is already computed — a message that survived but lost most
of itself — and `Change.kept_prefix` says the new text is the old one cut
short. When both are true the card is flagged `TRUNCATED` in the gutter, with
the kept prefix and the dropped tail shown as separate blocks rather than as an
inline diff, because an inline diff of "the last 500 characters vanished" is
unreadable. This is the exact bug `examples/truncation_bug.py` and the demo GIF
are built around; it should be impossible to miss on screen.

*Renders from:* `context.from_event()`, `context.diff()`. Both already used by
`tape show --context --diff`.

### 3. Chain tree — `/run/<id>/chain`

Shown only when the run has `chain` events; the tab is absent otherwise rather
than empty.

An indented tree from each node's `path` and `depth`. Each row: name, type,
duration, fan-out. **Nested HTTP/LLM events appear as children of the node they
occurred inside** — which is the thing the transport layer alone cannot show,
and the reason the LangChain adapter exists. Selecting a node selects the
corresponding timeline event, so the two views stay in sync.

Because model nodes are deliberately not recorded (the transport shim already
records that crossing, with the wire bytes the callback lacks), a node's LLM
child is the `llm` event at that position. The tree makes that adjacency
visible instead of leaving a gap someone reads as a missing node.

*Renders from:* `chain` events' `framework`, `name`, `type`, `path`, `depth`,
`step` fields.

### 4. Fork tree — `/tree`

A forest. Each root is an unforked run; each child is a run whose header
carries `forked_from`, attached at its `fork_at` index.

Rendered as an indented tree, not a graph canvas — lineage here is shallow and
a canvas would be decoration. Each node shows run id, the patch expressions
from its footer's `patched` list, event count, and cost. The edge label is the
fork point: `@13`.

Selecting two nodes and pressing `x` opens the diff screen for that pair, which
is the common motion: *fork, then compare against the parent.*

*Renders from:* headers across all traces; `footer["patched"]` for the labels.

### 5. Divergence view — `/diff/<a>/<b>`

**Divergence first, per spec §8.2** — the differentiator, not a footer.

Top of screen, before any per-field detail: *the step where the two runs stopped
being the same run*, and what each did afterwards. Aligned pairs below it, two
columns, `Step` by `Step`. Identical runs collapse to `steps 0–12 identical
(12 events)` — one line, not twelve rows.

A `Change` renders as a field path with before/after values. Path splits are
rendered differently from value differences, because they are a different
claim: `run A called tool·path_a; run B called tool·path_b` with an explicit
"everything after this is incomparable, not divergent" note.

Totals footer: cost and tokens for each side, and the deltas.

*Renders from:* `tracediff.diff()` → `TraceDiff.steps`, `find_divergence()`,
`_totals()`.

### 6. Doctor findings — `/doctor/<id>`

Reads a doctor report produced by `tape doctor --json`. **The UI does not run
the agent** — doctor executes the user's command N times with real calls and
real cost, and a web page must not do that behind a click. The screen says so,
and shows the `tape doctor` command to run.

Findings grouped **by call site**, which is already how `analyse()` groups them
— an agent in a loop reads the clock forty times and the report is as long as
the number of distinct *places* it is nondeterministic. Each finding: site,
kind, name, the distinct values observed, occurrence count, and the suggestion.
Clicking a finding jumps to that event in the run view.

Path splits are a separate section, not mixed into sources, for the same reason
the terminal report separates them: a split points at code behaving perfectly.

*Renders from:* `doctor.Report` → `Source` and `Split`.

---

## Keyboard map

Keyboard-first. Every action reachable without the mouse; the mouse is a
convenience, never the only path.

| Key | Action |
|---|---|
| `←` `→` | previous / next event — **the primary motion** |
| `j` `k` | same, vim-style |
| `Home` `End` | first / last event |
| `g` then digits | jump to event N |
| `r` `c` `d` | inspector mode: raw / context / context-diff |
| `[` `]` | in diff mode, move the baseline earlier / later |
| `n` `p` | next / previous event *of the same kind* |
| `f` | filter by kind (multi-select) |
| `t` | fork tree |
| `x` | diff — from the tree, the two selected runs |
| `l` | chain tree, when the run has one |
| `y` | copy the `tape …` command for the current view |
| `?` | keyboard help overlay |
| `Esc` | back / close overlay |

`←`/`→` scrubbing is the interaction the whole layout serves. Selection state
changes instantly — no transition, no easing. The timeline is the scrubber; the
inspector is what it drives.

---

## Look

Dark, monospace throughout, no animation beyond instant state changes. It
should read as a profiler, not a product: dense, high information per pixel, no
empty hero space, no rounded cards with drop shadows.

Colour carries exactly one meaning — **event kind** — and is reused nowhere
else, so a colour is always answering the same question. Selection is an
outline; diff status is a gutter character plus a muted background tint
(`+` `-` `~` `=`), which keeps it legible for the ~8% of men with colour vision
deficiency and preserves the terminal's own vocabulary.

Both themes ship, defaulting to dark, because a projector or a bright room is
the one place a dark debugger fails.

---

## Server, and why it is safe by construction

```
tape ui [--port 7654] [--tape-dir .tape] [--no-open]
```

- **Binds `127.0.0.1` explicitly**, never `0.0.0.0`. Not configurable. A trace
  is redacted on a best-effort basis and redaction is pattern-matching, so the
  correct assumption is that a trace may still hold something private — which
  makes "never reachable from the network" load-bearing rather than a default.
- **No auth, no accounts, no sessions** — correct *because* of the bind, not
  instead of it.
- **No telemetry, no external requests.** The page loads zero remote assets, so
  it works offline and cannot phone home. Enforced by everything being inlined
  in one file.
- **Read-only.** No route mutates a trace. There is no write path to audit.
- Serves one HTML file and a JSON API; no cookies, no local storage beyond view
  preferences.

---

## Testing

The claim "the UI is a second renderer over the same functions" is the thing
worth testing, and it is testable without a browser.

- **The API equals the CLI.** For a fixture trace, `/api/diff/<a>/<b>` is
  asserted to carry the same divergence index, step count and totals that
  `tracediff.diff()` produced, and `/api/context/<id>/<i>` the same message
  count and char counts as `context.from_event()`. If the UI ever starts
  computing its own numbers, these fail.
- **Every route on a fixture trace**, including a run with chain events, a
  forked run, and an empty run — asserting status and shape, no browser.
- **Truncation reaches the API.** The `truncated` / `kept_prefix` flags on a
  `Change` must survive serialisation, because the screen's headline case is
  built on them. Driven by `examples/truncation_bug.py`, which already produces
  the bug deterministically with no API key.
- **The server binds loopback.** Asserted directly: a connection to the
  non-loopback address is refused. This is the security claim, so it is a test
  rather than a comment.
- **No new runtime dependency.** `test_wheel_install.py` already fails if a
  module goes missing; a companion check asserts `dependencies == []` still
  holds, since the whole no-framework decision is invisible otherwise.

A browser-driven end-to-end test is deliberately not proposed: it would add a
heavy dev dependency to cover a layer whose logic is already covered above.

---

## Scope boundary

**In:** the six screens; keyboard navigation; read-only JSON API; one inlined
HTML file; loopback bind.

**Out, deliberately:**

| Excluded | Why |
|---|---|
| Fork button / inline editing | Executes the user's agent — cost, credentials, subprocess. Different risk surface; `tape fork` owns it |
| Running `tape doctor` from the UI | Same: real runs, real money, behind a click |
| Aggregate metrics, charts, cost-over-time | The observability product this is not |
| Auth, accounts, sharing, hosted mode | Loopback-only makes them unnecessary; adding them makes the bind negotiable |
| Live tailing of an in-progress run | Traces flush per line so it would work, but it is a second interaction model. Worth its own milestone if asked for |
| Editing or annotating traces | Read-only is what makes "no recording changes" structural |

---

## Build order

Each step ends somewhere usable, matching how every prior milestone was built.

1. **Server skeleton + runs index.** `tape ui` serves `/` and lists runs. Proves
   the stdlib server, the loopback bind, and trace discovery.
2. **Run view: timeline, inspector `raw`, arrow-key scrubbing.** The spine. At
   this point it is already useful.
3. **Context and context-diff modes**, truncation treatment included. The
   highest-value screen; it is where the demo GIF's bug becomes visible.
4. **Divergence view.** Reuses `tracediff` wholesale.
5. **Fork tree**, and `x` from tree to diff.
6. **Chain tree** and **doctor findings.** Both conditional screens, both thin
   over existing reports.
7. **Keyboard help overlay, both themes, README section, CHANGELOG.**

Steps 1–3 are the milestone's value; 4–6 are each a screen over a report that
already exists.

---

## Open questions for review

1. **Is the runs index worth a screen, or should `tape ui <run>` open straight
   into the run view** with the index as an overlay on `Esc`? The second is more
   debugger-like — you usually arrive knowing which run you want.
2. **Doctor: read a `--json` report from a path, or re-run `analyse()` over
   stored runs?** Doctor keeps its runs, so the second works with no new data
   and no execution — but a report is a *pair* of runs, and the UI would have to
   infer which. Leaning toward reading the JSON.
3. **Bucketing threshold on the timeline.** 2,000 is a guess; it should be
   measured against a real long agent run before it is fixed.
