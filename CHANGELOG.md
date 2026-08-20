# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

M10 — `urllib3` interception. The last uncovered HTTP stack, and the one where
the failure was worst.

### Added

- **`boto3` / `botocore` / Bedrock are recorded and replayed.** Until now an
  agent on Bedrock recorded **nothing at all** — no event, no error, and a
  `tape replay` that quietly went to the real API. botocore is built on
  `urllib3`, which sits below the httpx and requests shims, so nothing ever saw
  the call. The new seam is `HTTPConnectionPool.urlopen`: public, documented,
  and underneath every AWS SDK without knowing any of them exist.

  This is a **silent-recording fix, not a new feature**, and it is the largest
  version of the failure design principle 4 forbids. If you have replayed a
  Bedrock agent before this release, that replay was making live calls.

- **Bedrock's binary event stream is recorded frame for frame.** Bedrock streams
  `application/vnd.amazon.eventstream`, not SSE — a length prelude, typed
  headers and two CRC32s per message. Each message is recorded as its own chunk
  rather than one coalesced blob, and the test that proves it hands the recorded
  bytes back to **botocore's own parser**, which validates both checksums.

- **Amazon Nova pricing**, read from the AWS Price List Query API rather than
  the pricing page (which renders its tables client-side and cannot be read
  programmatically): `amazon.nova-micro` $0.035/$0.14, `amazon.nova-lite`
  $0.06/$0.24, `amazon.nova-pro` $0.80/$3.20 and `amazon.nova-premier`
  $2.50/$12.50 per 1M tokens, verified against us-east-1 and us-west-2.

- **A Bedrock provider decoder**, covering Anthropic-on-Bedrock, Titan, Nova,
  Meta and the Converse API, plus the Converse/`invoke-with-response-stream`
  operations. Token counts populate for all of them. It recognises Bedrock by
  the path shape as well as the hostname, so an `endpoint_url` override — a VPC
  endpoint, a gateway, LocalStack — no longer silently loses its token counts.

- **Replay works with no AWS credentials configured.** botocore signs *before*
  it sends, so a missing credential raises `NoCredentialsError` before the shim
  underneath is ever reached. Dummy credentials are injected for replay only,
  for tapes that actually touched `.amazonaws.com`, and only on machines with
  nothing configured — and the fact is reported in the replay summary, so a
  replay that works on a laptop with no AWS config is not a mystery.

- **`--patch llm.response=` works on a signed request**, and substitutes into
  the recorded response's **own model family shape** — a Titan caller still
  reads `results[0].outputText`, a Claude-on-Bedrock caller still reads
  `content[0].text`, and fields the decoder does not read survive untouched.

### Changed

- **Request-rewriting patches are refused on an AWS-signed event.**
  `--patch llm.model=…`, `llm.system`, `llm.temperature`, `http.url` and
  `http.body` now fail with an explanation instead of being accepted and
  silently doing nothing.

  botocore signs the request before it reaches reeltime's seam, and SigV4
  covers the URI path and a hash of the body — and on Bedrock the model id is a
  *path segment*, not a body field. Rewriting either would produce
  `SignatureDoesNotMatch`, an error about credentials for what is really an
  unsupported patch. The message names the reason and points at
  `llm.response=`, which works because a substituted result sends nothing.

  Previously these patches parsed, were accepted, and changed nothing — the
  same failure `tool.args` had for two releases.

- **`anthropic.claude-3-5-sonnet` is no longer priced on Bedrock**, and neither
  is Nova 2.0. Both are served through **cross-region inference profiles**,
  where the rate depends on the routing tier used — global vs geo vs in-region
  — and nothing in a recorded request says which tier answered it. A single
  rate per model id would be *wrong* rather than incomplete, so the row is gone
  and `cost_usd` reports null. Token counts are unaffected.

  The previously published `$6.00/$30.00` was one tier's rate presented as the
  model's rate. `anthropic.claude-instant` and `anthropic.claude-v2:1` are
  legacy in-region SKUs, were re-verified against the same source, and stay.

### Fixed

- **`x-amz-security-token` was reaching disk.** The STS session credential sent
  by anything using temporary AWS credentials — an assumed role, an instance
  profile, SSO, a Lambda. `Authorization` beside it was already dropped by name
  and the `AKIA`/`ASIA` id inside it was already pattern-matched, so a signed
  request *looked* covered. Also `x-amzn-authorization`, and the same credential
  in a presigned URL's query string.

- **A `requests` call is still one event, not two.** `requests` is built on
  `urllib3`, so it now passes through two shims; the M1 outermost-boundary rule
  already covered it, and there is a regression test pinning it. Same for a
  redirect retried inside `urlopen`.

### Known

- **Fork patches reach only the httpx seam.** Request-rewriting patches are
  refused on signed requests (above), but on an *unsigned* `urllib3` or
  `requests` event they are still accepted and silently do nothing. Result
  substitution works at the `urllib3` seam and not yet at the `requests` one.
  Scheduled as **M13**; see `STATUS.md`.

## [0.5.0] — 2026-08-19

M9 — framework coverage. One adapter, and one deliberate non-adapter.

### Behaviour change: aiohttp on replay

**If you have `aiohttp` installed, read this before upgrading.** reeltime has
never intercepted aiohttp, and until now that meant an aiohttp request during a
`tape replay` reached the **real network**, with no event and no error — a
replay that quietly did the real thing while reporting itself as offline and
free.

As of 0.5.0 that raises. A replay that reaches an aiohttp request stops with a
`TapeError` naming the method and URL; a *recording* that reaches one warns
once and is otherwise unchanged. Nothing about httpx, httpx2 or requests
changes.

If a run of yours starts failing on upgrade, the replay was already wrong — it
was calling out to the network. Two ways forward:

- wrap the call in a `@tape.tool` function, which is the supported fix. reeltime
  then records its *result*, which is the boundary replay actually needs, and
  the guard steps aside because the request is no longer the outermost
  boundary; or
- `tape.install(http=False)` to turn off HTTP interception entirely, if you
  were relying on live calls during replay on purpose.

aiohttp itself is still not intercepted, and that is a decision rather than a
gap — the reasoning is under "Not added, on purpose" below.

### Added

- **LangChain runs are recorded as `chain` events**, not as opaque HTTP.
  A LangChain agent is a tree, and the transport layer sees only its leaves —
  two POSTs with a growing message array, and none of the shape that decided
  them. A node now records its identity, the path it sits on, its depth, its
  fan-out, and its inputs and outputs. `tape.langchain.install()` arms it for a
  process; `tape run --langchain` needs no edit to the script; and
  `tape.langchain.handler()` returns a callback handler to scope it to one
  chain. LangGraph and `langchain.agents.create_agent` route through the same
  callbacks and are covered by the same adapter.
- **A chain node is structure, not a boundary.** A callback handler is an
  observer: it cannot stop a node from running, so it must not open a recording
  boundary either — the model call inside would be suppressed at record time
  and would then go live on replay. Chain events nest *around* other events
  rather than standing in for them.
- **The adapter does not record LLM nodes.** `on_chat_model_start` fires for
  the same crossing the transport shim already records with the wire bytes, the
  token counts and the streaming chunks, so recording it again would be two
  events for one boundary. Every other node — chains, tools, retrievers,
  prompts, parsers, agent steps — becomes an event.
- **`tape show N` renders a node as its place in the run tree**, and the run
  listing indents by depth so an agent's shape is readable at a glance.
  `--raw` still prints the JSON.
- **`tape diff` reports a changed graph on its own line** — a node that moved
  or changed depth, and a node whose fan-out changed — rather than diffing two
  payloads that differ everywhere downstream of the first difference.
- **`chain` folds into `http` for diff alignment**, the way `llm` and `mcp` do,
  so a run recorded before this adapter existed still lines up against one
  recorded since. It is deliberately *not* folded for replay matching: a wrong
  pairing in a diff costs a confusing line, while a wrong bucket in the matcher
  would serve an HTTP request a chain node's payload. `matching.align_key` and
  `matching.kind_key` are now separate functions for that reason.
- **`tape replay` and `tape fork` arm the adapter by themselves** when the tape
  has chain events in it. The tape knows which adapters were on; the user
  should not have to remember.
- **An unsupported `langchain-core` is refused with the range it needs.** The
  callback contract is not promised across a major version, and a trace that
  looks right and replays wrong is worse than no trace.
  `install(allow_unsupported=True)` overrides it. Tested against 0.3 and 1.5,
  both in CI — the floor job is what caught langchain-core renaming a message
  id prefix from `run--` to `lc_run--`.
- **`examples/langchain_agent.py`**: a LangGraph agent with tools against an
  embedded mock provider — no API key, no network — driven by the test suite.
  Recording it twice with `LANGCHAIN_EXAMPLE_TOOLS=extended` and diffing the
  two is the example's point.
- **An `aiohttp` guard** — `ClientSession._request` is patched **to refuse a
  request, never to intercept one**. See "Behaviour change" at the top of this
  entry, which is where the details are.

### Changed

- `KINDS` gains `chain`; `--only chain` selects it.
- `tape doctor` never reports a chain node as a nondeterminism source. Its
  outputs are what the boundaries underneath it produced, so blaming the node
  that carried a difference points at the wrong line — the same correction
  doctor already makes for an unlike pairing. A node that *moved* still shows
  up as a path split.

### Not added, on purpose

- **No `--patch` fields for `chain`.** A callback handler cannot change what a
  chain does, so a field that parsed and reported itself as applied would
  change nothing — the exact failure `tool.args` shipped with for two releases.
  Patch the `llm` boundary inside the node instead.
- **`aiohttp` interception.** httpx publishes
  `BaseTransport.handle_request(Request) -> Response` and promises it; aiohttp's
  equivalent seam is the private `ClientSession._request` (33 parameters), and
  replay would mean fabricating a `ClientResponse` over aiohttp's private
  connection contract — its `StreamReader` calls
  `protocol.resume_reading(resume_parser=…)`, a keyword in no public interface.
  Prototyped, and it works; it is two fake objects and eight private attributes
  that would need re-verifying on every aiohttp release, for a stack no LLM SDK
  reeltime targets is built on. The guard above closes the part that actually
  mattered.

## [0.4.0] — 2026-08-18

Two milestones and a grammar audit. `tape doctor` is the headline: it is the
first thing in this project that is useful before you have recorded anything.

### Added

- **MCP sessions are recorded as `mcp` events**, not as opaque HTTP. Server
  identity, tool name, and arguments are named fields, and `tools/list` is
  recorded too: a server that exposes a different tool set between two runs
  changes what the agent can attempt at all, and that belongs in the trace
  rather than showing up later as an unattributable divergence.
  `tape.mcp.connect(...)` opens the session, over stdio or over HTTP (SSE and
  streamable HTTP both). `tape.mcp.wrap(session, server=...)` records a session
  you opened yourself.
- **Replay does not start the server.** A pure replay opens no subprocess and
  contacts no URL; every call is served from the tape, and one that is not
  recorded raises `TapeMiss` rather than quietly going live. A fork does start
  it, because a fork continues for real past its fork point.
- **`tape show N` renders an MCP event as prose** — server, tool, arguments,
  result, and for a discovery event the tool list with its schemas. `--raw`
  still prints the JSON.
- **`tape diff` reports a changed tool set on its own line**, naming what
  appeared and what went away, rather than diffing an opaque payload. Same tool
  names with different schemas are reported separately, which content
  addressing answers without either payload being read.
- **`mcp` folds into `http` for alignment**, the way `llm` does, so a session
  recorded before this adapter existed still lines up against one recorded
  since. `--only` is deliberately *not* folded: `--only mcp` means MCP events.
  (Splitting filtering from folding also fixed `--only llm`, which had been
  handing back every plain HTTP call; that half shipped separately as 0.3.1.)
- `--patch mcp.<tool>.result=…` substitutes an MCP result in a fork, exactly as
  `tool.<name>.result=` does for a local tool.
- `examples/mcp_agent.py` and `examples/mcp_server.py`: a mock MCP server over
  stdio, no credentials and no network, driven by the test suite. Recording it
  twice with `MCP_EXAMPLE_TOOLS=extended` and diffing the two runs is the
  changed-tool-set report end to end.
- **`--patch tool.<name>.args` and `mcp.<tool>.args`** now call the boundary
  with different arguments, and the event records the call that was actually
  made. `tool.args` had been in the grammar since 0.2.0 with nothing reading
  it: it parsed, the fork ran, the footer reported it as applied, and the tool
  was called with the original arguments.
- **Every field the patch grammar accepts now has a test proving it reaches
  its boundary**, plus a test that fails if a field is added to the grammar
  without one, and a test that fails if a field is missing from either
  documentation table. `tests/test_patch_effects.py`.

- **`tape doctor <command>`** (M7) runs a command more than once and reports
  what is actually nondeterministic about it: each boundary where two runs got
  different answers, the line of the user's code that crossed it, evidence,
  and what to do about it. A finding is a call site rather than an event, so an
  agent that reads the clock forty times produces one line instead of forty.
  A *path split* — where two runs stop making the same calls at all — is
  reported separately, because everything after it is incomparable rather than
  divergent. `--runs N` looks harder, `--json` emits the report as data, and
  `--fail-on-findings` exits 1 so it can be a CI gate. It needs no replay and
  no prior traces, which makes it the first thing a new user can run.

### Fixed

- **`--patch http.url` rewrote nothing.** It fell through to the generic
  body-field path and wrote a `url` key *into* the JSON request body, leaving
  the request pointed where it already was. It now rewrites the outgoing URL.
- **`http.body` and `.args` accepted `+=` and `~=` and then ignored them.** A
  whole JSON document has no meaningful append or regex substitution, so those
  are now refused when the expression is parsed — before a fork runs, which is
  where every other patch mistake is already caught.
- **A fork's footer never reached disk.** `forked_from`, `fork_at`, and the
  list of applied patches were added to the footer dict *after* it had been
  written, so a fork's trace did not record what was patched to produce it.
  (The header carried the lineage, so only the patch list was lost outright.)

- An MCP server started over stdio no longer inherits the recording
  environment. It is a subprocess of a recorded agent, so `REELTIME_RUN_ID`
  would have had it open the *same* trace file and append its own header and
  events to a run it is not part of.

## [0.3.1] — 2026-08-18

Released from a branch off the 0.3.0 release commit, so it carries this fix
and nothing else. The same fix is in `[Unreleased]`'s tree as part of M5.5.

### Fixed

- **`tape diff --only <kind>` widened the comparison instead of narrowing it.**
  Kinds are folded for *alignment* — `llm` is a label a decoder puts on an
  `http` event, and folding is what lets a run recorded before that decoder
  existed line up against one recorded after. Filtering was folding too, so
  `--only llm` asked for LLM calls and got every plain HTTP request in the run
  as well. Filtering is now literal, with one deliberate alias: `--only http`
  still includes `llm`, because an llm event *is* an http event wearing a
  label.

## [0.3.0] — 2026-08-18

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
