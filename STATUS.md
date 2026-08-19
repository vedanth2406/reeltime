# Status

Handoff notes, current as of **2026-08-19**. Written so a session starting cold
can pick up without re-deriving anything or re-litigating decisions that were
already made for reasons.

The build spec is [`tapedeck-spec.md`](tapedeck-spec.md); the competitor
teardown that reshaped the roadmap is [`COMPETITIVE.md`](COMPETITIVE.md); the
release checklist is [`RELEASING.md`](RELEASING.md).

---

## Where things stand

| | |
|---|---|
| Milestones done | M1–M7, including M5.5. There is no M8 — see the roadmap |
| Published | `0.1.1`, `0.2.0`, `0.3.0`, `0.3.1`, `0.4.0` — all on PyPI |
| In the tree | `0.4.0`, clean. Nothing unreleased, nothing unpushed |
| Tests | 622 passing, 6 deselected (the wheel gate), 95% on `core/` |
| Repo | https://github.com/vedanth2406/reeltime |
| Tags | `v0.1.1`, `v0.2.0`, `v0.3.0`, `v0.3.1`, `v0.4.0` — the last three verified against their artifacts |
| Branches | `main`, plus `hotfix/0.3.1` — deliberately unmerged, see [`RELEASING.md`](RELEASING.md) |

**Next: M9 — the LangChain adapter and the rest of the framework coverage.**
There is no M8; the slot was emptied by the resequencing, not skipped. The
roadmap at the bottom says why.

There is no outstanding release work. `0.4.0` is published, tagged at the
commit it was built from, and verified.

### The `v0.3.0` tag drift — resolved, and why `RELEASING.md` exists

`v0.3.0` was briefly tagged at `f59d455`, the M5.5 commit, which landed *after*
the 0.3.0 artifact had been built. The published 0.3.0 sdist contains no MCP
code; the tag claimed ~2000 lines the release did not have. Nothing caught it,
because the release sequence had never been written down.

The tag now points at `6b32363`, the commit the artifact was built from. All
three releases since have been checked the same way — the published sdist file
list against `git ls-tree` of the tag:

```
v0.3.0  →  PKG-INFO
v0.3.1  →  PKG-INFO
v0.4.0  →  PKG-INFO
```

`PKG-INFO` is generated at build time, so that single line is a clean match.
The check itself is step 6 of [`RELEASING.md`](RELEASING.md). **Run it after
every release.** The rule it enforces: build from the commit you tag, and prove
the tag matches the artifact before announcing it.

---

## Milestones

| M | Shipped |
|---|---|
| **1** | Trace format (JSONL header + events + footer, flushed per line), content-addressed blob store, `Recorder`, `tape.install()`, ambient patches (`random`, `numpy.random`, `uuid`, clock), redaction, spans, call-site capture |
| **2** | httpx **and httpx2** transport shims (sync + async), `requests` fallback, streaming capture as an ordered chunk list, provider decoders (model/tokens/cost), `@tape.tool` / `wrap` / `wrap_all`, `tape run` / `ls` / `show` |
| **3** | `Player`, the three-tier matcher, `TapeMiss`, `tape replay` with `--to` / `--step` / `--strict` / `--loose` / `--realtime`, streaming re-emission |
| **4** | `tape show N --context` and `--context --diff M`, `tape reindex`, examples, LICENSE, CHANGELOG, README, **v0.1.0 → v0.1.1 released** |
| **5** | `tape fork <run> --at N`, the `--patch` grammar, `--edit`, lineage (`forked_from` / `fork_at`, shown in `tape ls`), **v0.2.0 released** |
| **6** | `tape diff <a> <b>` — signature alignment, divergence-point reporting, `--only`, `--json` |
| **5.5** | MCP adapter: `mcp` events, `tape.mcp.connect` over stdio and HTTP/SSE, `tape.mcp.wrap`, discovery recording, readable `tape show`, tool-set reporting in `tape diff`, `--patch mcp.<tool>.result=`, a mock-server example |
| **7** | `tape doctor` — run a command N times, report each boundary where the runs disagreed with its call site and a suggestion, plus the path split. `--runs`, `--json`, `--fail-on-findings` |
| **—** | The patch-grammar audit: `tool.args` and `mcp.args` implemented, `http.url` fixed, `+=`/`~=` on a JSON document refused at parse time, the fork footer written to disk, and `tests/test_patch_effects.py` |

CLI verbs today: `run`, `replay`, `fork`, `diff`, `doctor`, `reindex`, `ls`, `show`.

## Releases

- **0.1.0** — built and rehearsed through TestPyPI, **never published**. The
  version is consumed on TestPyPI, so it can never be reused there.
- **0.1.1** — first public release. Recording, replay, context inspection.
  **Do not yank**: it carries the path-normalisation bug below, and the
  CHANGELOG entry documents it.
- **0.2.0** — fork and patch, plus the `resolve()` fix.
- **0.3.0** — `tape diff` and the wheel-install CI gate. Its tag was wrong for
  a few hours; see the tag-drift section above.
- **0.3.1** — the `tape diff --only` fix alone, released off a branch from the
  0.3.0 release commit because `main` had already moved on to M5.5. The pattern
  is written up in [`RELEASING.md`](RELEASING.md).
- **0.4.0** — M5.5 (MCP sessions), M7 (`tape doctor`), the patch-grammar audit,
  and the fork-footer fix. `tape doctor` leads the README: it is the first thing
  in the project that is useful before you have recorded anything.


---

## The patch-grammar audit (0.4.0)

`--patch tool.<name>.args` was declared in `REQUEST_FIELDS` from 0.2.0 and read
by **nothing**. It parsed, `check_patches` accepted it, the fork ran, the footer
reported it as applied, and the tool was called with its original arguments.
That is worse than an unsupported field: a patch that parses and silently does
nothing sends you looking for the bug in your own agent.

Auditing the rest of the table found two more of the same shape and one nearby:

| Field | Was | Now |
|---|---|---|
| `tool.args` | parsed, never read | rewrites the call; the event records the call that was actually made |
| `mcp.args` | did not exist | same, for an MCP tool |
| `http.url` | fell through to the body path and wrote a `url` **key into the JSON body** | rewrites the outgoing request URL |
| `http.body` / `.args` with `+=` or `~=` | accepted, then silently ignored — appending to a JSON document is undefined | refused when the expression is parsed, before the fork runs |
| fork footer | `forked_from` / `fork_at` / `patched` were added to the footer dict *after* it had been written | passed into `recorder.close(extra=…)` so they reach disk |

**The guard against a recurrence is [`tests/test_patch_effects.py`](tests/test_patch_effects.py).**
It is a table, not a list of tests: one case per `(kind, field)`, each forking a
real run and asserting an effect only observable if the patch reached its
boundary. Three meta-tests keep it honest —

- `test_every_declared_field_has_a_case` fails if a field is added to the
  grammar without a case;
- `test_every_named_case_exists` fails if a rename orphans an entry;
- `test_every_declared_field_is_documented` fails if a field is missing from the
  `patch.py` table or the README table. It failed on first run, which is how the
  README rows got written.

`patch.declared_fields()` is what those read. **If you add a patch field, add it
there, implement it, document it in both tables, and add a case.** All four, or
the suite tells you which one you skipped.

---

## The two path-normalisation bugs

Both were the same mistake — comparing a resolved path against an unresolved
one — and they are the reason the wheel gate exists.

### 1. `Path.resolve()` vs `co_filename` (shipped in 0.1.0 and 0.1.1)

`_PACKAGE_ROOT`, `_STDLIB_DIR` and the site-packages roots in
[`core/callsite.py`](src/reeltime/core/callsite.py) are computed with
`Path.resolve()`. A frame's `co_filename` is **not** resolved — it keeps
whatever spelling the module was imported by. Anywhere those differ (any
virtualenv under `/tmp` on macOS, since `/tmp` → `/private/tmp`; any symlinked
home), every `startswith` comparison failed. Consequences:

- reeltime's own frames stopped counting as internal, so call sites were
  attributed to reeltime's modules instead of to the caller, **and**
- each ambient event was then classified as coming from site-packages and
  discarded as library noise — so `random`, `uuid` and clock reads recorded
  **nothing at all**. HTTP and tool events still recorded, with a wrong site.

**Fix:** a cached `_resolved()` helper; `_is_internal`, `_is_library` and
`_is_stdlib` all normalise before comparing. Fixed in `0.2.0`.

### 2. `abspath` vs `realpath` in the bootstrap shim

[`_bootstrap/sitecustomize.py`](src/reeltime/_bootstrap/sitecustomize.py) filters
its own directory off `sys.path` before importing the user's real
`sitecustomize`. It used `os.path.abspath`, which does not resolve symlinks. Had
that filter ever missed, the import underneath would have found the shim again
and recursed at interpreter startup. Now `realpath` on both sides, plus a
`sys._reeltime_bootstrap_active` re-entry guard so a miss degrades instead of
crashing. Unreleased.

**If you touch any path comparison, normalise both sides.** That is the whole
lesson.

---

## The wheel gate

[`tests/test_wheel_install.py`](tests/test_wheel_install.py), marker `wheel`,
deselected by default (`addopts = "-q -m 'not wheel'"`), run in CI by
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) on Linux and macOS.

It builds the wheel, installs it into a virtualenv **reached through a
symlink**, and asserts that a three-event script records three events. It also
checks call sites point at the user's code rather than into site-packages, and
that replay and fork work from the installed artifact.

**Why it must exist:** the unit suite runs from a checkout with no symlinks in
its path, so it is *structurally* incapable of catching bug 1 above. No amount of
unit testing closes that gap; only installing the built artifact somewhere
symlinked does.

**It must be able to fail.** This was verified by reverting the `resolve()` fix
and watching three of its tests go red. If you ever restructure it, re-verify
that the same revert still breaks it — a gate that cannot fail is decoration.

macOS is in the CI matrix because `/tmp` is a symlink there and that is how the
bug reached users; Linux is there because the test makes its own symlink and the
behaviour must not depend on the platform supplying one.

---

## Decisions not to re-litigate

**Transport-layer interception, never SDK patching.** The shim patches
`Client._transport_for_url`, httpx's own documented extension point. As of
2026-08-18 the OpenAI SDK (3.2.0) is built on **`httpx2` 2.10** while the
Anthropic SDK (0.122.0) is still on **`httpx` 0.28**; supporting that split cost
one constructor argument because both kept the same hook. An SDK-layer
interceptor would have needed a rewrite. Nothing in the recording path knows a
provider exists — model, tokens and cost are added afterwards by pure functions
in [`core/decoders/`](src/reeltime/core/decoders/).

**Three-tier matching** ([`core/matching.py`](src/reeltime/core/matching.py)).
Index matching breaks when you edit code; content-hash matching breaks when you
change a prompt by one character — which is the edit you make while debugging.
So identity and content are separate: tier 1 exact and silent, tier 2 the line
moved or the content drifted (matched, reported as drift), tier 3 content hash
alone (`--loose`). Tier 2 is the whole point. Tier 1 also requires the enclosing
*qualname* to agree, because two different calls can share a `file:line` after
code moves.

**`llm` folds into `http`.** `llm` is a label a decoder applies after the fact,
not a different boundary. Matching (`kind_key`) and diff alignment both fold
them, so a run recorded before a decoder existed lines up against one recorded
after. Forgetting this once made every real provider call miss its own
recording.

**`datetime` patching is opt-in.** Patching it means replacing the module
attribute with a subclass, and pydantic v2 dispatches on type *identity* — so
doing that makes the **real** datetime class unrecognisable to pydantic and
breaks any library that imported it first. The Anthropic SDK stops working
entirely. A metaclass fixes `isinstance`; nothing fixes identity dispatch.
`time.time()` is patched either way and covers most clock reads. Enable with
`patch=("random", "uuid", "time", "datetime")`.

**`REELTIME_FORK_PATCH` is not `REELTIME_PATCH`.** `REELTIME_PATCH` configures
the *ambient patch groups*. Reusing that name for fork expressions once meant a
forked child parsed `"[]"` as a group name, patched nothing, and silently
recorded nothing at all. The two must stay distinct; there is a regression test.

**The outermost boundary is the one recorded.** An HTTP call inside a
`@tape.tool` body produces no second event, and neither do ambient reads there.
On replay that body never runs, so anything recorded inside it could never be
matched.

**Only the user's own ambient reads are recorded.** `asyncio` reads
`time.monotonic()` every loop iteration and httpx reads `perf_counter()` twice
per request. The same filter applies on replay, so those stay live in both
directions — consistent, never a spurious miss.

**MCP: the transport is opened eagerly, or not at all.** `tape.mcp.connect`
decides at entry whether the run can go live — a recording or a fork can, a pure
replay cannot — and opens the transport then, rather than lazily at the first
call. The stdio transport runs its subprocess under an `anyio` task group,
which must be entered and exited from the *same* task; a lazily-opened one
would be entered from whichever task happened to make the first call. A fork
therefore does start the server even when every MCP call it makes turns out to
come from the replayed prefix, which is correct: a fork goes live.

**MCP over HTTP claims its endpoint instead of relying on nesting.** The
outermost-boundary rule is what normally stops the transport's own POST being
recorded a second time, but it cannot work here: the SDK issues those requests
from a task it spawned when the connection opened, and `in_boundary()` is a
contextvar, so the flag was copied before any boundary existed.
`http/common.own_endpoint()` claims the URL for the session's lifetime and the
httpx shim hands back an unwrapped transport for it. Without this, every MCP
call over HTTP is recorded twice — once as `mcp`, once as opaque `http` — and
there is a test that fails if it regresses.

**MCP results are recorded in their JSON-RPC wire form**, via
`model_dump(by_alias=True)`. The wire names (`inputSchema`, `isError`) are
fixed by the MCP specification; the Python field names are the SDK's own and
have already been renamed once between major versions (`isError` → `is_error`
in 2.0). Recording the wire form is what lets a trace stay rebuildable across
an SDK upgrade.

**A discovery event keeps its tool *names* inline** and its full definitions in
a field that may become a blob. `tape diff` compares two traces with no blob
store to hand, so a tool set that changed has to be visible in the event itself
or the report degrades to "the payload differs". Changed *schemas* under
unchanged names are detected by comparing the blob references, which content
addressing answers without either payload being read.

**`mcp` folds into `http` for alignment but not for `--only`.** Folding exists
so a session recorded before the adapter — when MCP over HTTP was opaque
`http` at the same call site — still lines up against one recorded since.
Filtering is a different question: `--only mcp` means MCP events, and `--only
http` includes `llm` because an llm event *is* an http event wearing a label.
`FILTER_ALIASES` in `core/matching.py` is where that distinction lives.

**An MCP server subprocess must not inherit the recording.** It is a child of a
recorded agent, so `REELTIME_AUTOINSTALL` and `REELTIME_RUN_ID` would have it
open the *same* trace file and append its own header and events to a run it is
not part of. `core/mcp._clean_env` strips every `REELTIME_*` variable, `TAPE_DIR`,
and the bootstrap directory off `PYTHONPATH`. The SDK's own default happens to
pass only a six-name allowlist, but a caller passing `env=os.environ` to get one
variable through would hand over all of them.

**A doctor finding is a call site, not an event.** An agent in a loop reads the
clock forty times, and forty findings bury the one that matters. Findings are
grouped by `(site, kind, name)` and counted; the report is as long as the number
of distinct *places* a run is nondeterministic, which is the number a user can
act on.

**Doctor treats an unlike pairing as a split, not a source.** `tracediff.align`
deliberately pairs events with different signatures at the same position, so a
diff can show what replaced what. Reading that pairing as "this boundary
answered differently" makes doctor report `path_a` returning `"b"` — a finding
pointing at code that is behaving perfectly. `analyse` compares signatures and
calls it a path split instead. There is a test named after this.

**Doctor compares results, never requests.** A prompt that differs between two
runs is a *consequence* of some earlier source, not a source, and reporting it
as one sends the user to the wrong line. Request differences are counted as
`propagated` — how far the real source spread — and nothing more.

**Doctor says what it will cost before it does it.** It runs the agent N times,
for real, with real calls. That warning goes to stderr before the first run
starts, not in the report afterwards.

**Traces survive a crash.** Every event is flushed as it is written; a missing
footer line is exactly how you know a run did not exit cleanly.

**A parsed JSON body is stored as JSON only**, never alongside a base64 copy of
the original bytes. Keeping both was a redaction hole: the scrubber rewrites the
parsed view, and a secret in the base64 copy sailed past every pattern. The cost
is that whitespace is not byte-preserved, which no JSON parser can observe.

**Pricing is data, with a date.** [`core/decoders/pricing.py`](src/reeltime/core/decoders/pricing.py)
carries `CHECKED = "2026-08-18"` and source URLs. Re-verify it each release; a
stale price is a confidently wrong number in someone's cost report. An unknown
model yields `null`, never a guess.

---

## Running things

```bash
cd ~/newproject/reeltime
pip install -e ".[dev]"

pytest                                   # 622 tests; the wheel gate is deselected
pytest --cov --cov-report=term-missing   # core/ is at 95%; the bar is 85%
pytest -m wheel                          # the symlinked wheel-install gate (slow)
pytest -m wheel -v                       # what CI runs

python examples/m3_replay_speed.py       # the ~80× number the README quotes
python examples/truncation_bug.py        # the demo; no API key needed
tape run python examples/mcp_agent.py    # the MCP example; no API key either
vhs demo.tape                            # re-record demo.gif (needs `brew install vhs`)
```

The demo binds a fixed port (`REELTIME_DEMO_PORT`, default 8422) because the
request URL is part of what replay matches on — an ephemeral port would make
every event report as drifted.

### Build and publish

**Follow [`RELEASING.md`](RELEASING.md).** It is short, it is the sequence that
has actually worked, and the two things that bite most are in it in full:

- **Commit before you build, and tag the commit you built from.** The artifact
  comes from the working tree, so anything uncommitted ships without being in
  the tag. This is what went wrong with `v0.3.0`.
- **Rebuild after any README edit.** PyPI renders the README once at upload and
  never again, so a stale `dist/` ships a description nobody checked.

`.pypirc` is configured for both indexes, so no token handling is needed.
TestPyPI's token was revoked once mid-project after being pasted into a chat; if
an upload there returns **403**, that is the first thing to check. TestPyPI also
takes a few seconds to index a fresh upload, and reports the gap as
"unsatisfiable requirements" — retry before believing it.

### Try it

```bash
tape run python agent.py       # record, no code changes needed
tape ls                        # what has been recorded
tape replay last               # offline, free
tape show last 1 --context     # what the model actually read
tape fork last --at 1 --patch 'llm.system+="Ask first."'
tape diff <a> <b>              # where two runs stopped being the same run
tape doctor python agent.py    # what is actually nondeterministic here
```

```python
async with tape.mcp.connect("python", ["server.py"], server="files") as session:
    tools = await session.list_tools()      # discovery is recorded too
    await session.call_tool("read_file", {"path": "a.txt"})
```

---

## Roadmap from here

| M | Scope | Status |
|---|---|---|
| 1–6 | see above | ✅ |
| 5.5 | **MCP adapter** | ✅ |
| 7 | `tape doctor` — run twice, report actual nondeterminism sources | ✅ |
| 9 | LangChain adapter, remaining framework coverage | **next** |
| 10 | Web UI | |
| 11 | Overhead benchmarks, docs site → v1.0 | |

**There is no M8.** The original spec §11 had M8 = web UI. The resequencing
after the competitive analysis moved the web UI to M10, which emptied the slot;
nothing was deferred and nothing is missing. The number is simply vacant, and
the row is left out rather than backfilled — closing the gap by renumbering is
how it got mistaken for skipped work once already.

M9, concretely: a LangChain callback adapter, and whatever else the framework
layer needs to be honestly covered. It is the last adapter-shaped milestone.

M11 is what stands between here and v1.0: measure the recording overhead per
boundary kind and publish a docs site. The README currently claims ~2 ms per
HTTP event and 20–30 µs per ambient read — **those numbers predate M5.5 and M7
and should be re-measured, not re-quoted.**

MCP was deliberately early, and it shipped: no record/replay tool captures MCP
sessions, and AgentTape still publishes an `mcp` optional dependency with no
code behind it. The web UI stays late: it is the most expensive milestone and
the least differentiating, since a competitor already ships a viewer.
