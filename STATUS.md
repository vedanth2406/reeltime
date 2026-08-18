# Status

Handoff notes, current as of **2026-08-18**. Written so a session starting cold
can pick up without re-deriving anything or re-litigating decisions that were
already made for reasons.

The build spec is [`tapedeck-spec.md`](tapedeck-spec.md); the competitor
teardown that reshaped the roadmap is [`COMPETITIVE.md`](COMPETITIVE.md); the
release runbook is [`RELEASE.md`](RELEASE.md).

---

## Where things stand

| | |
|---|---|
| Milestones done | M1–M6 **and M5.5** (of 10) |
| Published | `0.1.1`, `0.2.0` on PyPI. **`0.3.0` is built, checked, and on TestPyPI — the real upload has not been run** |
| In the tree | `0.3.0` + M5.5 (the MCP adapter), unreleased |
| Tests | 552 passing, 6 deselected (the wheel gate), 94% on `core/`, 100% on `core/mcp.py` |
| Repo | https://github.com/vedanth2406/reeltime |
| Git | **5 commits unpushed** (`git push` before anything else) |
| Tags | `v0.1.1`, `v0.2.0` |

Next, in order: **finish publishing 0.3.0** (see below), then **M7 (`tape
doctor`)**. M5.5 is done.

### Finishing the 0.3.0 release

Everything up to the real upload is done: version bumped in both files, the
CHANGELOG heading moved, `dist/` rebuilt from the released tree, `twine check
--strict` passed, TestPyPI uploaded and installed from, and the whole product
verified out of that install. What is left:

```bash
python -m twine upload dist/*
git push && git tag -a v0.3.0 -m "reeltime 0.3.0" && git push origin v0.3.0
gh release create v0.3.0 --title "reeltime 0.3.0" --notes-from-tag
```

Check <https://test.pypi.org/project/reeltime/0.3.0/> in a browser first — the
GIF and the tables. TestPyPI serves a bot challenge to `curl`, so that check
cannot be scripted, and PyPI renders a README exactly once.

**`dist/` is the 0.3.0 tree, not the current tree.** M5.5 landed after the
build. Do not rebuild before uploading, or the artifact stops matching what was
verified on TestPyPI; M5.5 ships in 0.4.0.

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

CLI verbs today: `run`, `replay`, `fork`, `diff`, `reindex`, `ls`, `show`.

## Releases

- **0.1.0** — built and rehearsed through TestPyPI, **never published**. The
  version is consumed on TestPyPI, so it can never be reused there.
- **0.1.1** — first public release. Recording, replay, context inspection.
  **Do not yank**: it carries the path-normalisation bug below, and the
  CHANGELOG entry documents it.
- **0.2.0** — fork and patch, plus the `resolve()` fix.
- **0.3.0 — built and on TestPyPI, not yet on PyPI.** `tape diff` and the
  wheel-install CI gate. See "Finishing the 0.3.0 release" above.
- **0.4.0 — pending.** `[Unreleased]` holds M5.5, the MCP adapter.

`.pypirc` is configured for both indexes, so no token handling is needed.
TestPyPI's token was revoked once mid-project after being pasted into a chat; if
an upload there returns **403**, that is the first thing to check.

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

pytest                                   # 489 tests; the wheel gate is deselected
pytest --cov --cov-report=term-missing   # core/ is at 94%; the bar is 85%
pytest -m wheel                          # the symlinked wheel-install gate (slow)
pytest -m wheel -v                       # what CI runs

python examples/m3_replay_speed.py       # the ~80× number the README quotes
python examples/truncation_bug.py        # the demo; no API key needed
vhs demo.tape                            # re-record demo.gif (needs `brew install vhs`)
```

The demo binds a fixed port (`REELTIME_DEMO_PORT`, default 8422) because the
request URL is part of what replay matches on — an ephemeral port would make
every event report as drifted.

### Build and publish

```bash
rm -rf dist build && python -m build
python -m twine check --strict dist/*
python -m twine upload --repository testpypi dist/*   # always first
python -m twine upload dist/*
git push && git tag -a v0.3.0 -m "reeltime 0.3.0" && git push origin v0.3.0
```

Two things that bite, both in [`RELEASE.md`](RELEASE.md) in full:

- **Rebuild after any README edit.** PyPI renders the README once at upload and
  never again, so a stale `dist/` ships a description you did not check.
- The GIF is referenced by absolute `raw.githubusercontent.com` URL. It must
  resolve *before* the first upload of a version, or fixing it costs a version
  bump.

### Try it

```bash
tape run python agent.py       # record, no code changes needed
tape ls                        # what has been recorded
tape replay last               # offline, free
tape show last 1 --context     # what the model actually read
tape fork last --at 1 --patch 'llm.system+="Ask first."'
tape diff <a> <b>              # where two runs stopped being the same run
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
| — | release 0.3.0 | built, on TestPyPI, **PyPI upload outstanding** |
| 5.5 | **MCP adapter** | ✅ |
| 7 | `tape doctor` — run twice, report actual nondeterminism sources | **next** |
| 8 | LangChain callback adapter | |
| 9 | Overhead benchmarks, docs site → v1.0 | |
| 10 | Web UI | |

MCP was deliberately early, and it shipped: no record/replay tool captures MCP
sessions, and AgentTape still publishes an `mcp` optional dependency with no
code behind it. The web UI was moved last: it is the most expensive milestone
and the least differentiating, since a competitor already ships a viewer.
