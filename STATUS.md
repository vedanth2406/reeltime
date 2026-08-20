# Status

Handoff notes, current as of **2026-08-20**. Written so a session starting cold
can pick up without re-deriving anything or re-litigating decisions that were
already made for reasons.

The build spec is [`tapedeck-spec.md`](tapedeck-spec.md); the competitor
teardown that reshaped the roadmap is [`COMPETITIVE.md`](COMPETITIVE.md); the
release checklist is [`RELEASING.md`](RELEASING.md).

---

## Where things stand

| | |
|---|---|
| Milestones done | M1–M10, including M5.5. There is no M8 — see the roadmap |
| Published | `0.1.1`, `0.2.0`, `0.3.0`, `0.3.1`, `0.4.0`, `0.5.0` — all on PyPI |
| In the tree | **M10 complete and unreleased.** `0.5.0` is the last published version |
| Tests | 833 passing, 7 deselected (the wheel gate), 95% on `core/` |
| Repo | https://github.com/vedanth2406/reeltime |
| Tags | `v0.1.1`, `v0.2.0`, `v0.3.0`, `v0.3.1`, `v0.4.0`, `v0.5.0` — every release from `v0.3.0` on is checked against its published sdist at [`RELEASING.md`](RELEASING.md) step 6 |
| Branches | `main`, plus `hotfix/0.3.1` — deliberately unmerged, see [`RELEASING.md`](RELEASING.md) |

**M10 is done and sitting unreleased in the tree** — `urllib3` interception,
closing the Bedrock/boto3 gap. Core, tests, example and docs are all in;
[What M10 shipped](#what-m10-shipped) is the summary and
[the SigV4 finding](#forking-below-a-signer-the-sigv4-asymmetry) is the one
piece of it that changed a decision rather than adding code. There is no M8;
that slot was emptied by the resequencing, not skipped. The web UI moved from
M10 to M11 to make room; the roadmap at the bottom says why, and why that one
shuffled where M8 did not.

**The next thing to do is release it.** Follow [`RELEASING.md`](RELEASING.md)
from step 0; the `CHANGELOG.md` entry is written and sits under
`## [Unreleased]`, and the version has not been chosen or bumped yet. `0.5.0`
is published, tagged at the commit it was built from, and verified.

### Two things to know before the next release

- `pytest -m wheel` matters more than usual now. M9 put `core/langchain.py`,
  `reeltime/langchain.py` and `core/http/aiohttp_guard.py` in the wheel and M10
  added `core/http/urllib3_shim.py`, `core/aws.py` and
  `core/decoders/bedrock.py`, so a packaging change can drop one without the
  unit suite noticing. **This is now checked rather than remembered:**
  `test_the_wheel_ships_every_module_in_the_source_tree` reads the module list
  off `src/reeltime/` and fails naming anything missing from the artifact.
  Verified able to fail, by excluding a module from the wheel and watching it
  go red — every *other* wheel test still passed, which is exactly the point.
- The README states a supported `langchain-core` range. If the CI
  `langchain-core floor` job is red, raise `MINIMUM` in `core/langchain.py`
  **and** the range in the README — do not work around it. That job has already
  caught one silent payload rename; it is not decoration.

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
| **9** | LangChain adapter: `chain` events, `tape.langchain.install()` / `handler()`, `tape run --langchain`, node tree in `tape show`, graph-change reporting in `tape diff`, a version gate with a CI floor job, an example, and the `aiohttp` guard |
| **10** | `urllib3` interception: `HTTPConnectionPool.urlopen` shim (record + replay, streaming chunk-exact), the Bedrock decoder across five model families with a binary event-stream parser, dummy-credential injection for replay, the `x-amz-security-token` redaction fix, a mock-Bedrock example, and the SigV4 patch decision below |
| **—** | The patch-grammar audit: `tool.args` and `mcp.args` implemented, `http.url` fixed, `+=`/`~=` on a JSON document refused at parse time, the fork footer written to disk, and `tests/test_patch_effects.py` |

CLI verbs today: `run`, `replay`, `fork`, `diff`, `doctor`, `reindex`, `ls`, `show`.
Event kinds today: `llm`, `http`, `tool`, `mcp`, `chain`, `rand`, `time`, `uuid`.

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
- **0.5.0** — M9: the LangChain adapter (`chain` events), the graph-change diff,
  `tape run --langchain`, the `langchain-core` version gate with its CI floor
  job, and the aiohttp guard. **The guard is a behaviour change** and the
  CHANGELOG entry leads with it: an aiohttp request during a replay used to
  reach the real network silently and now raises. Anyone upgrading with aiohttp
  installed and a replay that touches it will see a new failure — and that
  failure was always there, just invisible.


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

## What M10 shipped

**M10 is complete.** `urllib3` interception, closing the Bedrock/boto3 gap:
core, tests, the example, and the docs. The suite is green at 833 passing, the
wheel gate at 7, and `core/` is at 95%. What is left is the *release*, which is
[`RELEASING.md`](RELEASING.md) from step 0 and not a milestone task.

The assessment that authorised it is worth reading first if you are cold: the
seam is `HTTPConnectionPool.urlopen`, public and documented, and unlike aiohttp
the replay side was prototyped *before* any code was written and turned out to
be one public keyword-only constructor. See the M9 framework audit above for
why Bedrock was the gap worth closing.


### Landed first, deliberately separate

Three commits went in ahead of the Bedrock work, each standing on its own so
that none of them is hostage to M10 finishing:

| Commit | What it did |
|---|---|
| `4bfe2c5` | **The release checklist gap.** `RELEASING.md` step 7 is now a script that exits non-zero and prints `RELEASE VERIFIED`, rather than a diff for a human to eyeball at the end of a long release. It checks the published sdist against the tag, that the tag exists, that it was pushed, and that its commit is on a remote branch. Verified both ways: it passes on the real 0.5.0 and fails when pointed at a missing tag or at `v0.4.0`. |
| `7075ec8` | **`x-amz-security-token` was reaching disk.** The STS session credential, sent by anything using temporary AWS credentials — an assumed role, an instance profile, SSO, a Lambda. `Authorization` beside it was already dropped by name and the `AKIA`/`ASIA` id inside it was already pattern-matched, so a signed request *looked* covered. Also `x-amzn-authorization`, and a pattern for the same credential in a presigned URL's query string. Landed ahead of M10 because it applies to anyone using boto3 today whether or not this milestone ships. |
| `9618087` | **The renumber**, plus the boundary-rule design note. |

### The core — what each piece is for

| File | Role |
|---|---|
| [`core/http/urllib3_shim.py`](src/reeltime/core/http/urllib3_shim.py) | The shim. Patches `HTTPConnectionPool.urlopen`; records by wrapping the response in a file-like, replays by building a fresh `HTTPResponse` over the recorded chunks. Installed last in `HttpShim` so the install order matches the way a request travels. |
| [`core/aws.py`](src/reeltime/core/aws.py) | Dummy-credential injection for replay. Needed because **botocore signs before it sends**, so a missing credential raises `NoCredentialsError` and the shim underneath is never reached at all — measured, with the environment scrubbed `urlopen` sees zero calls. Scoped three ways: replay only, tapes that actually touched `.amazonaws.com` only, and machines with nothing configured only. Reported through `ReplaySummary.environment` so a replay that works on a laptop with no AWS config is not a mystery. |
| [`core/decoders/bedrock.py`](src/reeltime/core/decoders/bedrock.py) | The provider decoder. Handles Anthropic-on-Bedrock, Titan, Nova, Meta and the Converse API, plus a binary event-stream frame parser (`iter_frames`) because Bedrock streams `application/vnd.amazon.eventstream`, not SSE. |
| [`examples/bedrock_agent.py`](examples/bedrock_agent.py) | Mock Bedrock endpoint, both operations, no AWS account. Currently records 2 `llm` events and replays identically, with `137→6` tokens. `cost_usd` is **null** on purpose — it runs Claude-on-Bedrock, which has no price row; see the pricing section. |

Also touched: `core/http/__init__.py` (registers the shim),
`core/http/common.py` (`application/vnd.amazon.eventstream` added to
`STREAM_CONTENT_TYPES`), `core/player.py` and `core/tape.py` (the
`environment` note), `core/decoders/__init__.py` and `pricing.py`.

### Three bugs, all found by measuring rather than reasoning

Recorded because two of them are invisible to inspection and the third would be
reintroduced by anyone tidying the stream path.

**1. The shim recorded nothing at all.** Completion only fired on an empty read
or an explicit `close()`. botocore reads a non-streaming body with a single
`resp.read()` and never reads again, so the event was never written. The first
end-to-end run reported `recorded 0 events` while the agent worked perfectly.
Fixed by completing when the body is exhausted, not only when someone asks for
more.

**2. `read(amt)` collapses the frame boundaries. This is the one to be careful
with.** The obvious implementation of a recording wrapper is to pass `read(n)`
through to the inner response and keep what comes back. It is wrong, and it is
wrong *silently*: `read(n)` on a buffered response **blocks until it has n
bytes or the connection ends**, so a six-frame Bedrock stream arrives as a
single 1376-byte read and every chunk boundary is gone before anything can
record it. The recording still replays correctly — the bytes are all there —
so nothing fails, and the chunk-boundary claim quietly becomes untrue.

`_RecordingBody` therefore drives the inner response with **`stream()`**, which
yields what arrived when it arrived (one HTTP chunk at a time for a chunked
response), and hands the caller one inner chunk per `read()`. Measured: the
same stream records as **six chunks** this way and **one** the other way.
`read(None)` still returns the whole body, because that is what asking for
everything means — the chunks are recorded individually and joined on the way
out.

**If you refactor the stream path, re-check the recorded chunk count on a
multi-frame stream.** A test asserting only that the bytes round-trip will pass
against the broken version.

Two smaller consequences of the same design, both load-bearing:
`_RecordingBody.closed` reports *its own* exhaustion rather than delegating to
the inner response, because urllib3 marks a response closed as soon as its
connection is drained while chunks are still queued in the iterator; and the
mock in the example sleeps `FRAME_GAP_S` between frames, because flushing alone
does not stop the kernel coalescing the writes.

**3. The decoder only matched on hostname.** Any `endpoint_url` override — a
VPC endpoint, a gateway, LocalStack, or the example's own mock — put a
different name in front of the same API and the call silently lost its token
counts. `matches` now also recognises the path shape, `/model/<id>/<operation>`
with one of four known operations, which is specific enough to carry the
recognition on its own.

### Bedrock pricing: now from the Price List API, and still deliberately incomplete

**The source changed on 2026-08-20 and that is the important part.**
`https://aws.amazon.com/bedrock/pricing/` still renders its current-model
tables client-side, so it was never a usable source for anything not in the
served HTML — which is why M10 originally shipped four hand-read rows. Every
Bedrock row is now read from the **AWS Price List Query API** instead: public,
unauthenticated, machine-readable, and versioned per region.

```
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/region_index.json
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/<region>/index.json
```

**The `usagetype` field is what makes it trustworthy.** A model has several
SKUs and they are not the same product: `USE1-NovaLite-input-tokens` is the
base on-demand rate, while `-batch` is exactly half, `-custom-model` matches,
and `-cross-region-global` is a different routing tier. Reading "the price"
without filtering on that gives a number that is 0.5x or 1.1x the real one.

Added, verified against **us-east-1 and us-west-2, which agree exactly**:

| Row | per 1M in / out |
|---|---|
| `amazon.nova-micro` | $0.035 / $0.14 |
| `amazon.nova-lite` | $0.06 / $0.24 |
| `amazon.nova-pro` | $0.80 / $3.20 |
| `amazon.nova-premier` | $2.50 / $12.50 |

`anthropic.claude-instant` and `anthropic.claude-v2:1` were re-verified against
the same source and were already correct.

**Removed: `anthropic.claude-3-5-sonnet` ($6.00/$30.00).** It is not sold as an
in-region SKU at all — it does not appear in any region's price list, because
from 3.5 Sonnet on Claude is served through **cross-region inference
profiles**, and the rate depends on which routing tier the profile used: global
vs geo vs in-region. Nothing in a recorded request says which one answered it,
so a single rate per model id is *wrong* rather than incomplete. Tokens still
populate; `cost_usd` stays null. The `$6.00/$30.00` figure read off the page in
M10 was one tier's rate presented as the model's rate.

**Nova 2.0 is absent for exactly the same reason**, and this one is measurable:
`amazon.nova-2-lite-v1:0` is $0.30/$2.50 per 1M through a global cross-region
profile and $0.33/$2.75 in-region. A prefix collision here would have been
silent, so `test_nova_2_does_not_borrow_the_first_generation_price` pins it —
had the rows been spelled `amazon.nova-` rather than `amazon.nova-lite`,
longest-prefix matching would have priced every Nova 2.0 call at the older,
cheaper rate.

**`global.` is deliberately not in `BEDROCK_REGION_PREFIXES`.** It looks like
one more geography and is not: stripping it would price a global-profile call
at the in-region rate. Left unstripped it matches nothing and reports no cost,
which is the honest answer. `us.` / `eu.` / `apac.` are still stripped.

**Two simplifications that are stated rather than hidden.** The table is
US-based: the same Nova Lite is $0.06/$0.24 in us-east-1 and us-west-2 but
$0.078/$0.312 in eu-central-1, so a European run is under-reported rather than
guessed. And latency-optimised Nova Pro is billed at $1.00/$4.00 against the
standard $0.80/$3.20 while using the *same* model id, so a latency-optimised
call is under-reported too — same class as batch and prompt caching, which the
module docstring already declares unmodelled.

### Follow-up: make the Bedrock rows re-runnable — M14

The rows above are still **hand-transcribed** from an API response, which means
the next person to re-verify them does what this session did: fetch three
region files, filter on `usagetype`, and copy numbers across by hand. That is
the part worth automating, and it is small and self-contained.

A script — `tools/refresh_bedrock_pricing.py`, or a `pytest -m pricing` check —
that fetches the region index, filters to base on-demand SKUs, and diffs the
result against `PRICES` would turn `RELEASING.md` step 0's "re-verify
`pricing.py`" from a manual reading task into a command that exits non-zero.
It would also catch the failure mode this section exists to document: a model
quietly acquiring a `-cross-region-global` SKU, which is the signal that its
single row has become wrong rather than merely stale.

Scoped as **M14**, after v1.0, because it is a maintenance tool rather than a
user-facing capability. The OpenAI and Anthropic rows have no equivalent
machine-readable source, so this covers Bedrock only — which is also where the
transcription is hardest and the tiering most likely to change under us.

### Forking below a signer: the SigV4 asymmetry

**This is the one part of M10 that changed a decision rather than adding code,
and it was found by measuring.** The checklist asked whether `chain`-style
patch grammar applied here. The literal answer is no — no new `--patch` fields
were added, so `test_patch_effects.py` needs no case and the four-step rule is
not triggered. But testing the *existing* fields against this seam found them
silently doing nothing, which is the exact failure the patch-grammar audit
exists to prevent.

Measured, not reasoned:

| Patch | Before M10 finished |
|---|---|
| `llm.model=…` on a Bedrock fork | request went out with the **original** model; footer reported nothing |
| `llm.response="…"` on a Bedrock fork | request **still hit the network**; agent got the original answer |

The cause is that `engine.substitute`, `rewrite_url` and `rewrite_body` are
wired into `httpx_shim` and **only** into `httpx_shim`.

**Why the fix is not "mirror what httpx does".** botocore signs the request and
*then* calls `urlopen`, so this seam sits below the signer. SigV4 covers the URI
path and a hash of the payload — verified directly by re-signing with a changed
body and a changed path and watching the signature change both times. And on
Bedrock the model id is a **path segment**, not a body field, so `llm.model` is
precisely the case that would rewrite signed bytes. Mirroring httpx here would
replace a silent no-op with `SignatureDoesNotMatch`: an error about
credentials, for what is really an unsupported patch.

So the two halves were split by what the signature actually permits:

- **Request-rewriting patches are refused**, in `check_patches`, before the
  fork replays anything. `fork.is_signed_request` recognises a signed request
  from its header *names* — an `Authorization` beside any `x-amz-*` — because
  the redactor drops that header's value before it reaches disk, so the
  signature is not there to inspect and does not need to be. The message names
  the reason and points at the patch that does work.
- **Result substitution is implemented**, because a substituted result sends
  nothing and so has nothing left to invalidate. The substituted body is built
  from the parent event's own recorded response, so it comes back in that model
  family's shape — a Titan caller still reads `results[0].outputText`, a
  Claude-on-Bedrock caller still reads `content[0].text`. A fixed template
  would make a Titan agent raise `KeyError` instead of showing what it does
  with a different answer, which is the whole point of the patch.

That shape logic lives in `core/decoders/` (a new optional `substitute` slot on
`Decoder`, plus `decoders.substituted_body`), **not** in the shim — the
transport is required to know no providers exist, and Bedrock is one endpoint
in front of families that agree on nothing.

### Still open: fork patches reach only the httpx seam — M13

**This is a known bug with a number, deliberately, because a known bug without
one never gets fixed.** M10 fixed the part that is M10's own territory. The
general case is larger and predates it:

| Seam | Request rewriting (`llm.model`, `llm.system`, `http.url`, `http.body`) | Result substitution (`llm.response`) |
|---|---|---|
| `httpx` / `httpx2` | ✅ | ✅ |
| `urllib3`, signed (Bedrock) | **refused, with a reason** — M10 | ✅ — M10 |
| `urllib3`, unsigned | ✕ **accepted, silently does nothing** | ✅ — M10 |
| `requests` | ✕ **accepted, silently does nothing** | ✕ **accepted, silently does nothing** |

The `requests` row has been true since **M2** and is not an M10 regression;
`requests_shim` has never had the fork hooks. The unsigned-`urllib3` row is the
part of M10's seam the signature argument does not cover, so refusing it on
signature grounds would be a lie.

**Why it was not fixed here:** the SigV4 refusal is specific to signed
requests, and extending it to every non-httpx event needs the trace to record
*which seam* recorded each event — a trace-format addition — or the hooks
wiring into both remaining shims. Either is its own piece of work, and it is
`requests`-shaped as much as `urllib3`-shaped.

**M13, after v1.0.** Two ways to do it, and the choice has not been made:

1. Wire `substitute` / `rewrite_url` / `rewrite_body` into `requests_shim` and
   into the unsigned `urllib3` path, so the grammar means the same thing
   everywhere. More work, no new trace fields, and the honest end state.
2. Record the recording seam on each event and refuse what that seam cannot do,
   the way signed requests are refused now. Cheaper, and it leaves the grammar
   narrower than the docs imply.

Prefer (1); (2) is the fallback if the rewrite hooks turn out to be awkward at
the `requests` layer. Either way the rule from the patch-grammar audit stands:
**a patch that parses and silently does nothing is worse than one that is
refused.**

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

**A LangChain chain node is structure, not a boundary.** Everything else in the
adapter follows from this. A callback handler is an *observer*: it is told a
node started, it cannot stop the node from running. If a chain node opened a
recorder boundary, the model call inside it would be suppressed at record time
— and would then go live on replay, because a callback cannot serve a result
either. So chain events nest *around* other events rather than standing in for
them, and the adapter uses `record()`, never `capture()`.

**The adapter does not record LLM nodes.** That is what keeps the event count
honest. `on_chat_model_start` fires for exactly the crossing the transport shim
already records, and with less: the callback has no wire bytes, no token
counts, no chunk list. Recording both would be two events for one boundary.
Model nodes are still *tracked* — their children need the right depth — just
never written. Everything else becomes an event, because a rule with exceptions
is a rule people get wrong.

A LangChain *tool* node that makes an HTTP call is therefore two events on
purpose: one `chain` for the node, one `http` for the crossing inside it. They
are different things at different levels. Wrapping the function in `@tape.tool`
instead makes it one event and stops the body running on replay.

**A chain node's identity is where it sits, never what flowed through it.**
`CONTENT_FIELDS["chain"]` is `(framework, name, type, path, depth, step)` and
deliberately excludes `inputs`. A node's inputs are a *consequence* of the model
calls above it, which the tape already holds still; hashing them would report
drift on every node downstream of a prompt tweak and bury the one place the run
actually changed. Same reasoning as "doctor compares results, never requests".

**`chain` folds into `http` for alignment but not for matching.** This is a
third distinction alongside `EQUIVALENT_KINDS` and `FILTER_ALIASES`, and
`matching.align_key` exists for it. Alignment is advisory — a wrong pairing
costs a confusing line in a report. Matching is not: a request folded into the
wrong bucket can be served a chain event's payload where an HTTP response was
expected, which is a silent wrong answer rather than a clean `TapeMiss`. `llm`
and `mcp` fold in both places because they genuinely *are* the same crossing as
an `http` event; a chain node never is.

**Every node in a run tree inherits the root's call site.** LangGraph runs nodes
on a thread pool, so a child's callback frequently fires on a stack with no user
frame on it at all, and the nearest answer is then a line number inside
langchain — which moves on every upgrade and would drift every event in the run.
Only a root node walks the stack. The consequence is that all of an agent's
chain events share one site and are matched in recorded order, disambiguated by
the structural content key.

**The handler sets `run_inline = True` and `raise_error = True`, and both are
load-bearing.** This is where LangChain's version of M5.5's contextvar problem
lives, and it is worse. Without `run_inline`, `ahandle_event` dispatches a
synchronous handler to a thread-pool executor on *every* async path: the
executor thread's stack contains no user frame at all (measured — it is
`thread.py:_worker` all the way down), so call-site identity is destroyed, and
the handlers are `asyncio.gather`ed so write order stops matching event order.
Without `raise_error`, LangChain catches every exception a handler raises and
logs it at warning level — so a `TapeMiss` during a replay would be *swallowed*
and the replay would sail past a call it could not match. That is the exact
silent divergence design principle 4 forbids.

Related: on the async path even an inline handler sees only the frame where the
event loop was entered, because a suspended coroutine's frame is not on the
stack. That is pre-existing (async HTTP events already behave this way), stable
across record and replay, and therefore harmless — but it is why chain identity
lives in the content key rather than in the line number.

**LangChain's per-run message ids never reach the trace.** They are the *only*
part of a node payload that differs between two identical runs — measured by
running the same agent twice and diffing every callback argument, not assumed —
and leaving them in would make every downstream node's inputs differ, so
`tape diff` would report noise at every step. `core/langchain.stable()` drops
`id` keys whose value is a UUID or a run-derived id. A `tool_call` id does not
look like one and survives, which is right: it is part of the conversation.

**The prefix on that id is `run--` in langchain-core 0.3 and `lc_run--` in
1.x.** The CI `langchain-core floor` job is what found that, on its first run.
It is the concrete argument for both the version gate and the floor job: the
callback contract is stable, the payloads underneath it are not.

**`chain` has no `--patch` fields, and should not get any.** A callback handler
cannot alter what a chain does, so a field that parsed and reported itself as
applied would change nothing — which is exactly what `tool.args` did for two
releases. `patch.declared_fields()` therefore lists nothing for `chain`, and
`tests/test_patch_effects.py` needs no case. Patch the `llm` boundary inside the
node instead. **If that ever changes, all four steps still apply.**

**aiohttp is not intercepted, and the guard is the point.** See "The framework
audit" for the assessment. The part worth remembering: reeltime patches
`aiohttp.ClientSession._request` *to refuse it*, not to record it. A replay that
reaches an aiohttp request stops with a message naming the URL; a recording
warns once. aiohttp is **not** added to the footer's `intercepted` list, because
that list answers "why was my call not recorded?" and listing it would answer
wrongly.

**The M1 boundary rule keeps absorbing problems later milestones expected to
have to solve, and that is an argument about the architecture worth recording.**
Twice now a milestone has budgeted for a double-recording problem and found it
already handled:

- **M9** expected to have to stop a LangChain tool node and the HTTP call
  inside it from both being recorded. It did not: `record()` and `consume()`
  both return None inside `in_boundary()`, so the rule wrote the answer.
- **M10** expected `requests`-on-`urllib3` to be the hard part, since a shim at
  the connection-pool layer sits *underneath* the `requests` shim and both
  would fire on one call. Measured before writing any code: inside a
  `RequestsShim`-recorded call, `urlopen` is reached with `in_boundary()`
  already true, so a urllib3 shim respecting the same rule records nothing
  extra. One event, not two, for free.

The rule is four lines of `threading.local` written in M1, before any of the
things it now protects existed. What makes it keep paying is that it is stated
in terms of *boundaries* rather than in terms of any particular library: "the
outermost boundary is the one recorded" does not care whether the inner thing
is httpx, urllib3, a LangChain callback, or something not written yet. A rule
phrased as "do not double-record requests calls" would have needed rewriting at
both milestones.

The practical consequence for whoever adds the next interception point:
**check whether the rule already covers your case before designing around it,
and measure rather than reason.** Both times the answer was already yes, and
both times it would have been easy to build a redundant mechanism instead.

**A patch is refused when the seam cannot honour it, never accepted and
dropped.** M10's version of this is the SigV4 refusal above: botocore signs
before reeltime's seam sees the request, so rewriting the URL or body there
would be rejected by AWS rather than run. The general rule is the one the
patch-grammar audit paid for — `tool.args` parsed, reported itself applied, and
did nothing for two releases, and that sends you looking for the bug in your own
agent. **A patch that silently does nothing is worse than one that is
refused**, so when a boundary cannot honour a field, the fork stops before it
replays anything and says why. The fields that are still silently dropped are
listed under M13; they are a bug with a number, not a design.

**Result substitution is what survives below a signer.** Rewriting a request
needs to happen above whoever signs it; substituting a result needs no request
at all. That asymmetry is why `llm.response` works at the `urllib3` seam and
`llm.model` cannot, and it is worth remembering before designing any future
interception point that sits underneath an SDK's own request preparation.

**A substituted body is built from the parent's recorded response, not from a
template.** The httpx shim can fabricate an OpenAI- or Anthropic-shaped body
because its providers agree on a shape. Bedrock is one endpoint in front of
families that agree on nothing — Titan reads `results[0].outputText`, Claude
reads `content[0].text`, Meta reads `generation` — so a template would hand a
Titan agent a body it raises on. Rewriting the recorded body in place keeps the
family, the field names, and every key the decoder does not read. The logic
lives in `core/decoders/`, behind an optional `substitute` slot on `Decoder`,
because the transport layer is required not to know a provider exists.

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

## The framework audit (M9)

M9 asked what else the framework layer needs to be honestly covered. This is the
answer, including the things that were **not** built — recorded here so the next
session does not re-derive it, and so that "not covered" never means "nobody
looked".

### Built

| | |
|---|---|
| **LangChain** | `chain` events via the callback adapter. Covers `langchain`, `langgraph` and `langchain-openai` too — all of them route through `langchain-core`'s callbacks, so `create_agent` and a LangGraph graph are recorded by the same code, verified end to end. |
| **aiohttp** | Not intercepted. Guarded, so it cannot be silent. |

### Assessed and not built

**aiohttp — leave unsupported, guard the consequence.** The seam is the
problem, not the demand. httpx publishes
`BaseTransport.handle_request(Request) -> Response`, a documented extension
point with a public `Response` constructor; that is why the httpx shim is small
and survives SDK churn. aiohttp's only public hook is `TraceConfig`, which is
observe-only — it can neither substitute a response nor supply a chunk, so it
can record and can never replay, which is the worst possible half. The real seam
is the private `ClientSession._request` (33 parameters).

Replay was prototyped, and it works, which is why the recommendation is a cost
argument and not an impossibility claim. Fabricating a `ClientResponse` from
recorded bytes needs: a fake protocol implementing
`resume_reading(resume_parser=…)` — a keyword in no public interface — a fake
stream writer with `output_size` and five methods, four private attributes set
by hand, and a live or faked `ClientSession`. Every one of those would need
re-verifying on each aiohttp release. The httpx equivalent is one public
constructor.

And the demand is thin: OpenAI, Anthropic, Google GenAI and the MCP SDK are all
on httpx or httpx2. The realistic exposure is an agent's own tool code, which
`@tape.tool` already covers at a *better* boundary — replay needs the tool's
result, not the request inside it.

What was unacceptable was the consequence, and that is what got built instead:
`core/http/aiohttp_guard.py` patches the same private method to **refuse**
during a replay and to **warn once** during a recording. Reconsider the full
shim only if aiohttp grows a public transport abstraction, or if a provider
reeltime targets moves onto it.

**Everything else, and why not.**

| Stack | Position |
|---|---|
| **LlamaIndex** | The strongest candidate for M12 if a framework adapter is wanted again. It has its own `CallbackManager` / `BaseCallbackHandler` with `CBEventType`, structurally similar to LangChain's, so the `Tracker` in `core/langchain.py` would mostly transfer — the shell is what would be new. Not built because one framework adapter is enough to prove the shape, and LangChain is the one people are debugging. |
| **LiteLLM, Instructor, Ollama, Mistral, Cohere** | Already covered, with no work. All are httpx clients, so the transport shim records them today; they are unenriched (no model/token/cost) only until someone writes a decoder, which is a pure function and a pricing row. |
| **Bedrock (`boto3` / `botocore`)** | Genuinely uncovered — botocore is on `urllib3`, below every shim. This is the largest real gap after aiohttp. `urllib3` does have a seam (`HTTPConnectionPool.urlopen`), so a shim is more tractable than aiohttp's; it is a milestone of its own, not a footnote to this one. `aiobotocore` is on aiohttp and inherits that position. |
| **OpenAI Realtime / voice** | A WebSocket, not a request/response boundary at all. Recording it means recording a bidirectional message stream with timing — closer to the streaming chunk work than to the HTTP shim, and a design question in its own right. Not attempted. |
| **Vertex AI over gRPC** | Same shape of problem as WebSockets: no HTTP boundary to sit under. The REST transport is covered. |
| **CrewAI, AutoGen, Pydantic AI, OpenAI Agents SDK** | Each has its own tracing or hook surface, and each would be its own adapter. All of them make their model calls over httpx, so **the LLM boundary is already recorded** for every one — what is missing is only the structural layer, which is exactly what M9 built for LangChain. Worth doing on demand rather than speculatively. |

The pattern worth carrying forward: **the transport shim covers the boundary,
an adapter covers the shape.** Nothing on that list is unrecorded at the
boundary except Bedrock, aiohttp, WebSockets and gRPC — and three of the four
are uncovered for the same reason, which is that they are not HTTP request/
response through a client with a public transport seam.

---

## Running things

```bash
cd ~/newproject/reeltime
pip install -e ".[dev]"

pytest                                   # 738 tests; the wheel gate is deselected
pytest --cov --cov-report=term-missing   # core/ is at 95%; the bar is 85%
pytest -m wheel                          # the symlinked wheel-install gate (slow)
pytest -m wheel -v                       # what CI runs

# What the CI `langchain-core floor` job does, locally:
pip install "langchain-core==0.3.*" && pip uninstall -y langchain langchain-openai
REELTIME_LANGCHAIN_FLOOR=1 pytest tests/test_langchain.py -q

python examples/m3_replay_speed.py       # the ~80× number the README quotes
python examples/truncation_bug.py        # the demo; no API key needed
tape run python examples/mcp_agent.py    # the MCP example; no API key either
tape run python examples/langchain_agent.py   # the LangChain example; no key either
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
tape run --langchain python agent.py   # LangChain/LangGraph structure too
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
| 9 | LangChain adapter, remaining framework coverage | ✅ |
| 10 | `urllib3` interception — Bedrock/boto3, streaming included | ✅ unreleased |
| 11 | Web UI | **next** |
| 12 | Overhead benchmarks, docs site → v1.0 | |
| 13 | Fork patches at the `requests` and `urllib3` seams | after v1.0 |
| 14 | Re-runnable Bedrock pricing check against the Price List API | after v1.0 |

**There is no M8.** The original spec §11 had M8 = web UI. The resequencing
after the competitive analysis moved the web UI to M10, which emptied the slot;
nothing was deferred and nothing is missing. The number is simply vacant, and
the row is left out rather than backfilled — closing the gap by renumbering is
how it got mistaken for skipped work once already.

### The M10 renumber (2026-08-19) — and why this one shuffled

The web UI moved a second time, from M10 to M11, and benchmarks from M11 to
M12, to put `urllib3` interception at M10. **This is a real renumber, unlike
the M8 vacancy, and it was done deliberately rather than by leaving another
hole.**

The reason for the move: the M9 framework audit went looking for the largest
remaining coverage gap and found Bedrock. `botocore` is built on `urllib3`,
which sits below every shim reeltime has, so an agent on Bedrock records
nothing at all — and unlike aiohttp, `urllib3` has a public, documented seam
(`HTTPConnectionPool.urlopen`) and a response object with a public constructor.
Closing a stack where the tool silently records nothing beats shipping a viewer
for the runs it already records.

The reason it shuffled rather than taking a new number: a second vacant slot
would need its own paragraph of explanation forever, and one vacancy is already
one more than anybody wants to think about. **M8 is still vacant and still must
not be backfilled** — that gap is load-bearing history, this one would have
been clutter.

The published 0.5.0 README on PyPI says M10 is the web UI and always will:
PyPI renders a description once, at upload, and freezes it. Not worth a version
bump to correct, and noted here so nobody reads it as drift.

M9 shipped the LangChain callback adapter and closed the framework question:
see "The framework audit" above for what else was considered and why it was
not built. It was the last *adapter*-shaped milestone; M10 is transport-shaped,
which is a different job.

M14 is the pricing-refresh script under "Follow-up" above: small, self-contained,
and it turns a manual re-verification into a command that fails.

M13 is the fork-patch gap under "Still open" above — the grammar reaches only
the httpx seam, and on `requests` that has been true since M2. It sits after
v1.0 because it is a correctness gap in a feature that works everywhere people
currently use it, not a hole in coverage; the signed-request half, which is the
half M10 made reachable, is refused loudly rather than silent.

M12 is what stands between here and v1.0: measure the recording overhead per
boundary kind and publish a docs site. The README currently claims ~2 ms per
HTTP event and 20–30 µs per ambient read — **those numbers predate M5.5 and M7
and should be re-measured, not re-quoted.**

MCP was deliberately early, and it shipped: no record/replay tool captures MCP
sessions, and AgentTape still publishes an `mcp` optional dependency with no
code behind it. The web UI stays late, and got later: it is the most expensive
milestone and the least differentiating, since a competitor already ships a
viewer — and a viewer for runs the tool cannot record is worth less than being
able to record them.
