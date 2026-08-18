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
| Milestones done | M1–M6 (of 10) |
| Published | `0.1.1`, `0.2.0` on PyPI |
| In the tree | `0.2.0` + unreleased work → **`0.3.0` pending** |
| Tests | 489 passing, 6 deselected (the wheel gate), 94% on `core/` |
| Repo | https://github.com/vedanth2406/reeltime |
| Git | **2 commits unpushed** (`git push` before anything else) |
| Tags | `v0.1.1`, `v0.2.0` |

Next, in order: **release 0.3.0**, then **M5.5 (MCP adapter)**. Do not start M7.

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

CLI verbs today: `run`, `replay`, `fork`, `diff`, `reindex`, `ls`, `show`.

## Releases

- **0.1.0** — built and rehearsed through TestPyPI, **never published**. The
  version is consumed on TestPyPI, so it can never be reused there.
- **0.1.1** — first public release. Recording, replay, context inspection.
  **Do not yank**: it carries the path-normalisation bug below, and the
  CHANGELOG entry documents it.
- **0.2.0** — fork and patch, plus the `resolve()` fix.
- **0.3.0 — pending.** `[Unreleased]` in the CHANGELOG currently holds
  `tape diff` and the wheel-install CI gate. Bump `pyproject.toml` *and*
  `src/reeltime/__init__.py` together, move the `[Unreleased]` heading to
  `## [0.3.0] — <date>`, then follow [`RELEASE.md`](RELEASE.md).

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

---

## Roadmap from here

| M | Scope | Status |
|---|---|---|
| 1–6 | see above | ✅ |
| — | **release 0.3.0** | **next** |
| 5.5 | **MCP adapter** | then this |
| 7 | `tape doctor` — run twice, report actual nondeterminism sources | |
| 8 | LangChain callback adapter | |
| 9 | Overhead benchmarks, docs site → v1.0 | |
| 10 | Web UI | |

MCP is deliberately early. No record/replay tool captures MCP sessions today,
and AgentTape publishes an `mcp` optional dependency with no code behind it —
which reads as intent. The web UI was moved last: it is the most expensive
milestone and the least differentiating, since a competitor already ships a
viewer.
