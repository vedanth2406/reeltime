# Contributing

reeltime is a debugger, so its own tests are a credibility signal — a debugger
with weak tests is a bad joke. That is most of what this file is about.

## Running the suite

```bash
git clone https://github.com/vedanth2406/reeltime && cd reeltime
pip install -e ".[dev]"

pytest                                   # 882 tests, ~2.5 min
pytest --cov --cov-report=term-missing   # core/ must stay above 85%
```

**The wheel gate is deselected by default and is not optional.** It is marked
`wheel`, it builds a distribution and installs it into a virtualenv reached
through a symlink, and it is the only thing that can catch a whole class of
path bug:

```bash
pytest -m wheel                          # what CI runs
```

Why it exists: a path-normalisation bug shipped in `0.1.0` and silently
discarded every ambient event for anyone whose virtualenv lived under a symlink
— which is any venv under `/tmp` on macOS. The unit suite runs from a checkout
with no symlinks in its path, so it is *structurally* incapable of catching it.
Only installing the built artifact somewhere symlinked can.

Two optional tools unlock extra tests and are skipped cleanly without them:
`node` (the viewer's render check) and `vhs` (re-recording `demo.gif`).

## The bar for a change

**A test that cannot fail is decoration.** Before you claim a gate protects
something, break the thing on purpose and watch it go red — then put it back.
Every gate in this repo has been verified that way, and two of them did *not*
fail on the first attempt:

- the wheel gate was checked by reverting the `resolve()` fix;
- the viewer's render check missed a renamed field entirely, because reading an
  undefined property is falsy rather than an error, so it silently took the
  other branch. It now asserts rendered output instead of merely not throwing.

If you cannot make your test fail, you have not tested what you think.

**Measure rather than reason.** Several decisions here were made twice because
the first was reasoned and wrong: the timeline's layout constant looked like a
judgement call and was arithmetic; the published overhead figures were a
factor of ten out; a "boundaries round-trip" claim passed against a recording
that had destroyed the boundaries. Numbers in this repo come with the script
that produced them (`examples/overhead.py`, `tools/measure_timeline.py`).

**Loud failure over silent divergence.** This is design principle 4 and it
outranks convenience. If replay cannot match a call, raise `TapeMiss` naming
the call site; never fall through to a live request. If a seam cannot honour a
patch, refuse it with a reason rather than accepting it and doing nothing.

## If you add a `--patch` field, all four steps apply

`--patch tool.<name>.args` was declared for two releases and read by nothing.
It parsed, the fork ran, the footer reported it as applied, and the tool was
called with its original arguments. That is worse than an unsupported field: a
patch that silently does nothing sends you looking for the bug in your own
agent.

So a new field is four changes, not one:

1. declare it in `patch.declared_fields()`;
2. **implement it** — make it reach its boundary;
3. document it in *both* tables: the one in `core/patch.py` and the one in the
   README;
4. add a case to `tests/test_patch_effects.py` asserting an effect that is only
   observable if the patch arrived.

Three meta-tests enforce this, so skipping a step tells you which one you
skipped rather than shipping. Do not add a field you cannot implement — the
grammar is small on purpose, and `--edit` covers what it cannot express.

## Things that will fail review

- **Provider knowledge in the transport layer.** Shims record bytes; model,
  tokens and cost are added afterwards by pure functions in `core/decoders/`.
  Adding a provider should be one module and one pricing row.
- **A guessed price.** `core/decoders/pricing.py` carries a `CHECKED` date and
  a source. An unknown model reports `cost_usd: null`, never an estimate — a
  wrong number in a cost report is not obviously wrong, and a missing one is.
- **A new runtime dependency.** `dependencies = []` is why `pip install
  reeltime` cannot break an environment, and it is quoted in the README. The
  viewer is a standard-library server and one inlined HTML file for this
  reason.
- **Anything that writes during replay**, or a UI route that mutates a trace.
- **A credential fixture written out whole.** Use the vendor's published
  example and assemble it (`"ASIA" "IOSFODNN7EXAMPLE"`); a complete
  credential-shaped literal becomes a secret-scanning alert on somebody's fork.

## Where the reasoning lives

[`STATUS.md`](STATUS.md) is the long-form record: decisions not to re-litigate,
bugs found by measuring, and why several obvious-looking shortcuts are wrong.
You do not need to read it to send a patch, but if a reviewer says "that was
tried", it is where the answer is.

[`tapedeck-spec.md`](tapedeck-spec.md) is the original build spec with its
amendments; [`ui-design.md`](ui-design.md) is the viewer's design.

## Open work

reeltime is 1.0 and feature-complete, so there is no roadmap of features. Two
internal gaps are known, open, and unscheduled — both written up in `STATUS.md`
with the measurements behind them:

- **M13** — fork patches reach only the httpx seam. On an unsigned `urllib3` or
  `requests` event, request-rewriting patches are still accepted and silently
  do nothing. (On AWS-signed events they are already refused with a reason.)
- **M14** — the Bedrock pricing rows are hand-transcribed from the AWS Price
  List API. A script that diffs them against the live feed would make
  re-verification a command rather than a reading task.

Bug reports are more useful than either. A trace file in the issue is best —
they are redacted before they reach disk and are meant to travel.
