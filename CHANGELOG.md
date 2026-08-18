# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`tape diff <a> <b>`** — aligns two runs by event signature (kind, call
  site, name) and reports what changed structurally, not as text. The headline
  is the divergence point: the step where the two trajectories stop being the
  same run, and how many events each went on to record alone. For LLM steps the
  report reaches into the context, so a changed system prompt shows as the two
  lines that changed. `--only <kind>` narrows the comparison; `--json` emits the
  same structure as data.
- **A wheel-install CI gate.** `pytest -m wheel` builds the wheel, installs it
  into a virtualenv reached through a symlink, and asserts a three-event script
  records three events. The unit suite runs from a checkout with no symlinks in
  its path and is structurally blind to path-normalisation bugs — which is how
  the 0.1.x defect below survived. Runs on Linux and macOS in CI.

### Fixed

- The `sitecustomize` shim filtered its own directory off `sys.path` with
  `abspath`, which does not resolve symlinks. Had that filter ever missed, the
  import underneath would have found the shim again and recursed at interpreter
  startup. Now `realpath` on both sides, with a re-entry guard.

## [0.2.0] — 2026-08-18

### Added

- **`tape fork <run> --at N`** — replay events 0..N−1 from a run, then continue
  live from event N, recording the whole thing as a new run. The first N events
  are free and identical, so a prompt fix costs one step instead of a whole run.
- **`--patch` expressions**: `<kind>[.<name>].<field>` with `=`, `+=`, and `~=`
  (regex substitution). `llm.system` finds the system prompt whichever way the
  provider carries it, so one expression works against OpenAI and Anthropic
  alike. Patches that substitute a *result* stop the boundary executing at all.
- **`--edit`** opens `$EDITOR` on the event at the fork point. An empty buffer
  or invalid JSON aborts without creating a run.
- **Lineage**: `forked_from` and `fork_at` in the header, shown in `tape ls` as
  `← <parent>@<n>`. A fork is a complete trace, so it can be replayed and forked
  again; the chain is walkable back to the root.
- Patches and missing credentials are both checked **before** anything is
  replayed, so a mistake costs an error message rather than a replayed prefix
  and then an error message.

### Fixed

- **Call sites and ambient events were broken wherever the install path
  contains a symlink** — which includes any virtualenv under `/tmp` on macOS,
  since `/tmp` is a symlink to `/private/tmp`. The package, stdlib and
  site-packages roots are computed with `Path.resolve()` while `co_filename` is
  not resolved, so every prefix comparison failed: reeltime's own frames stopped
  counting as internal, call sites were attributed to reeltime's modules rather
  than to the caller, and **every ambient event (`random`, `uuid`, clock) was
  discarded** as though it came from a library. HTTP and tool events were still
  recorded, but with a call site pointing into reeltime. Present since 0.1.0 and
  invisible to the test suite, which runs from an unsymlinked checkout; found by
  installing the wheel into a throwaway venv and watching a three-event script
  record nothing.
- `numpy` is now a dev dependency. Without it, a module-level `importorskip`
  was silently skipping all 25 tests in `test_patches.py` rather than the two
  that needed numpy — including the ones covering the opt-in `datetime` patch.
- `Tape.__repr__` assumed only a Player could be replaying, and raised on a
  fork.

## [0.1.1] — 2026-08-18

First published release. `0.1.0` was built and rehearsed through TestPyPI but
never published to PyPI.

### Fixed

- The demo GIF reported "2× faster than the recorded run" while the README
  quoted ~80×. Both were true — the demo runs two events against an embedded
  mock with almost no latency to skip — but side by side they made each other
  look unreliable. `examples/truncation_bug.py` now states its conditions, and
  the README attributes the ~80× to the benchmark that measures it.

### Added

- A run id is now optional on `tape replay` and `tape reindex`, and every
  command accepts `last` (or `latest`, or `-`) for the most recent run.

## [0.1.0] — 2026-08-18, unpublished

First release. Recording and replay both work end to end.

### Added

- **Record** every boundary an agent crosses: HTTP (`httpx`, `httpx2`,
  `requests`), `@tape.tool` functions, `random`, `numpy.random`, `uuid`, and
  clock reads. `tape run python agent.py` needs no change to your code.
- **Streaming capture and replay.** Responses are recorded as their ordered
  chunk list with boundaries intact, and replayed the same way. `--realtime`
  reinstates the recorded gaps between chunks.
- **Replay**, offline and free: `tape replay <run>`, with `--to N`, `--step`,
  `--strict`, `--loose`, and `--realtime`. Roughly 80× faster than the recorded
  run on the included benchmark, at $0.00 and zero network calls.
- **Three-tier matcher.** Exact matches are silent; a shifted line number or
  drifted content still matches and is reported; `--loose` will match on content
  hash alone. A failure raises `TapeMiss` naming the call site and why each
  nearby recording was rejected.
- **`tape show <run> N --context`** prints the full assembled message array the
  model received, collapsing long messages with a marker stating exactly what
  was elided. `--context --diff M` aligns two calls and labels what the
  framework injected, dropped, changed, or truncated between them.
- **Provider decoders** add model, token counts, and cost to LLM events as pure
  functions over recorded bytes — no SDK patching. `tape reindex <run>` applies
  newer decoders to an older trace.
- **`tape ls`** and **`tape show`** for browsing runs and events.
- **Redaction** runs before anything reaches disk, blobs included: sensitive
  headers are dropped by name and key-shaped values are replaced. Extend it with
  `tape.redact(pattern)`.
- Content-addressed blob store for payloads over 8 KB, so traces stay greppable
  and repeated context deduplicates.
- Spans (`tape.span`) so concurrent calls replay under any completion order.
- Examples for the plain OpenAI SDK, the plain Anthropic SDK, and a multi-tool
  agent, all runnable and all covered by the test suite.

### Known limitations

See [What this can't replay](README.md#what-this-cant-replay). The short version:
replay reproduces decisions, not the world; `datetime` patching is opt-in
because it breaks pydantic v2; JSON body whitespace is not byte-preserved; and
`aiohttp` and raw sockets are not intercepted.
