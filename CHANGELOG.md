# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-08-18

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
