# Releasing

The sequence that has actually worked, in order. Steps 0–8 are every release;
the one-time setup that only mattered for the first publication is folded in at
the end, under [First publication](#first-publication-done-once).

**The rule this file exists for:** build from the commit you tag, and check the
tag against the published artifact before you announce it. `v0.3.0` was tagged
from a commit that landed *after* its artifact was built, so the tag carried
~2000 lines of code the release did not have. Nothing caught it, because none
of this was written down.

**The release ends at step 7, not at step 5.** The upload is the irreversible
step and so it feels like the finish line; it is not. Step 7 exits non-zero
until the published artifact, the tag, and the remote all agree — and it is the
only step whose output you are required to read. Twice now an artifact has
shipped without its tag following it.

---

## 0. Pre-flight

```bash
pytest                                   # everything green
pytest -m wheel                          # the symlinked wheel-install gate
pytest --cov --cov-report=term           # core/ must stay above 85%
```

- `src/reeltime/core/decoders/pricing.py` carries a `CHECKED` date. Re-verify
  it against the provider pricing pages and update it. A stale price is a
  confidently wrong number in someone's cost report.
- Decide the version. It is consumed forever once uploaded — on TestPyPI too,
  which is why `0.1.0` can never be reused there.
- `CHANGELOG.md` is the source of truth for release notes. Write it before
  tagging, not after.
- If the demo GIF was re-recorded, check its numbers still agree with the
  README. `examples/truncation_bug.py` runs two events against an embedded mock
  and reports ~2×; the README's ~80× comes from `examples/m3_replay_speed.py`
  at 400 ms per call. Both are true, they sit next to each other in the GIF, and
  they made each other look unreliable once already.

## 1. Prepare the tree

- `CHANGELOG.md`: move `## [Unreleased]` to `## [X.Y.Z] — <date>`.
- Bump the version in **both** places, and check they agree:

```bash
grep -n '^version' pyproject.toml
grep -n '__version__' src/reeltime/__init__.py
```

- Update anything in `README.md` that claims a number: the test count, the
  coverage figure, the roadmap table, and the CLI's `planned:` epilog in
  `cli.py` — those two are the only places that promise anything, so they must
  agree with each other.

## 2. Commit, then build from that commit

```bash
git add -A && git commit          # the release commit
git status --porcelain            # must be empty before building
rm -rf dist build && python -m build
```

**Commit first.** The artifact is built from the working tree, so anything
uncommitted ships without being in the commit you are about to tag. An empty
`git status` here is the whole guarantee.

## 3. Check the artifact

```bash
python -m twine check --strict dist/*     # both must say PASSED

# `tape run` silently records nothing without the shim.
python -c "import zipfile,glob; w=glob.glob('dist/*.whl')[0]; \
print('reeltime/_bootstrap/sitecustomize.py' in zipfile.ZipFile(w).namelist())"
```

**Rebuild after any README edit.** PyPI renders the README once, at upload, and
stores the result forever. A stale `dist/` ships a description you never read.

The README's images are referenced by absolute `raw.githubusercontent.com`
URLs. They must resolve *before* the first upload of a version, or fixing them
costs a version bump — which means **the release commit has to be pushed to
GitHub before the PyPI upload**, not after:

```bash
for asset in demo.gif ui.png; do
  printf '%s  ' "$asset"
  curl -sI "https://raw.githubusercontent.com/vedanth2406/reeltime/main/$asset" | head -1
done
```

Both are regenerable and cost nothing to re-record: `vhs demo.tape` for the
terminal GIF, `./tools/capture_ui.sh` for the viewer screenshot. They are
separate tools because vhs records a *terminal* and the viewer is a browser
page; the capture script drives headless Chrome to a URL instead, which the
viewer's location-hash deep links make possible without a browser-automation
dependency.

## 4. TestPyPI, and look at it with your eyes

```bash
python -m twine upload --repository testpypi dist/*
```

Open <https://test.pypi.org/project/reeltime/> in a browser. **This step cannot
be scripted** — TestPyPI serves a bot challenge to `curl`. Check, in order:

1. the GIF renders;
2. the description is Markdown, not raw text (raw text means `readme` lost its
   content-type);
3. the tables have borders — the comparison table and the tier table;
4. the sidebar has the licence, `Requires: Python >=3.9`, and all four links.

Then install what you just uploaded and run the product out of it:

```bash
uv venv /tmp/rt-test
VIRTUAL_ENV=/tmp/rt-test uv pip install --index-strategy unsafe-best-match \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ reeltime==X.Y.Z openai
cd $(mktemp -d)
/tmp/rt-test/bin/tape --version
/tmp/rt-test/bin/tape run python ~/newproject/reeltime/examples/truncation_bug.py
/tmp/rt-test/bin/tape show last 1 --context --diff 0
```

`--index-strategy unsafe-best-match` is required: without it `uv` resolves
`reeltime` from whichever index answers first and refuses the TestPyPI version.

**The first install attempt usually fails.** TestPyPI's index takes a few
seconds to list a version that has just been uploaded, and the error says
"unsatisfiable requirements" rather than "not there yet". Retry before believing
it — this happened on both `0.3.1` and `0.4.0`.

If the name is taken on TestPyPI, rename in `pyproject.toml` for this step only
and revert immediately. **Never leave a changed name in the tree.**

## 5. Real PyPI

```bash
python -m twine upload dist/*
```

Irreversible in the ways that matter: the version can never be reused, and the
rendered README is frozen at this moment. Deleting a release does not free the
version.

## 6. Tag the commit you built from

```bash
git push
git tag -a vX.Y.Z -m "reeltime X.Y.Z" <the-build-commit> && git push origin vX.Y.Z
gh release create vX.Y.Z --title "reeltime X.Y.Z" --notes-from-tag
```

**Name the commit explicitly.** `git tag` without one tags `HEAD`, which is the
same thing only for as long as nobody has committed since — and step 5 is the
step where you are most likely to have been distracted.

## 7. Close the release: run the check and read its verdict

**A release is not finished when the upload succeeds. It is finished when this
prints `RELEASE VERIFIED`.** Uploading is the irreversible part, so it feels
like the end; the tag is what makes the artifact traceable afterwards, and it
is the part with nothing forcing it to happen.

This has now gone wrong twice, in both directions, which makes it a checklist
gap rather than bad luck:

- **`v0.3.0`** — the tag existed and pointed at the *wrong* commit, claiming
  ~2000 lines the release did not contain.
- **`v0.5.0`** — the artifact was correct and the tag simply never followed.
  PyPI had 0.5.0; GitHub had no `v0.5.0` and the release commit was not even
  pushed. The provenance of a published artifact lived in one working copy.

Both would have been caught here, in seconds, by a step that fails instead of
printing something for you to eyeball. So this one exits non-zero:

```bash
python - <<'EOF'
import io, json, subprocess, sys, tarfile, urllib.request, hashlib

V = "X.Y.Z"                       # <- the version you just uploaded
TAG = "v" + V

meta = json.load(urllib.request.urlopen(
    "https://pypi.org/pypi/reeltime/{}/json".format(V)))
sdist = [u for u in meta["urls"] if u["packagetype"] == "sdist"][0]
raw = urllib.request.urlopen(sdist["url"]).read()

problems = []
if hashlib.sha256(raw).hexdigest() != sdist["digests"]["sha256"]:
    problems.append("the downloaded sdist does not match its own PyPI digest")

published = sorted(n.split("/", 1)[1]
                   for n in tarfile.open(fileobj=io.BytesIO(raw)).getnames()
                   if "/" in n)


def git(*args):
    out = subprocess.run(("git",) + args, capture_output=True, text=True)
    return None if out.returncode else out.stdout.strip()


if git("rev-parse", TAG + "^{}") is None:
    problems.append("{} does not exist locally".format(TAG))
elif not git("ls-remote", "--tags", "origin", "refs/tags/" + TAG):
    problems.append("{} exists locally but was never pushed".format(TAG))
else:
    tagged = sorted((git("ls-tree", "-r", "--name-only", TAG) or "").splitlines())
    only_published = set(published) - set(tagged) - {"PKG-INFO"}
    only_tagged = set(tagged) - set(published)
    if only_published:
        problems.append("in the sdist but not in {}: {}".format(
            TAG, sorted(only_published)))
    if only_tagged:
        problems.append("in {} but not in the sdist: {}".format(
            TAG, sorted(only_tagged)))
    commit = git("rev-parse", TAG + "^{}")
    if not git("branch", "-r", "--contains", commit):
        problems.append(
            "{} points at {}, which is on no remote branch — push it".format(
                TAG, (commit or "?")[:12]))

if problems:
    print("RELEASE NOT VERIFIED")
    for problem in problems:
        print("  -", problem)
    sys.exit(1)
print("RELEASE VERIFIED: {} matches {} (PKG-INFO aside, which the build "
      "generates)".format(V, TAG))
EOF
```

If it fails because the tag is on the wrong commit, move it rather than leaving
it wrong:

```bash
git tag -f -a vX.Y.Z <the-build-commit> && git push -f origin vX.Y.Z
```

Then re-run the check until it prints `RELEASE VERIFIED`. **Do not announce a
release you have not seen that line for.**

## 8. After

```bash
uv venv /tmp/rt-live
VIRTUAL_ENV=/tmp/rt-live uv pip install --refresh reeltime
/tmp/rt-live/bin/tape --version
```

**Read the version it prints, and do not trust the first answer.** `uv` caches
aggressively, so an install run shortly after an upload can resolve the
*previous* version and report success — no error, just a wrong version looking
like a pass. It is the same class of problem as TestPyPI's indexing lag in
step 4, failing in the worse direction.

Measured on the last two releases:

- **0.6.0** — a plain `uv pip install reeltime` gave `reeltime 0.5.0`. Only the
  stale `planned:` epilog from the older build gave it away.
- **0.7.0** — `--refresh` was *not enough on its own*: the first attempt still
  installed 0.6.0, in 5 ms, from cache. PyPI itself was fine — the JSON API and
  the simple index both listed 0.7.0 at that moment. A second identical
  command got 0.7.0.

So: pass `--refresh`, **and if the version is wrong, run it again** before
concluding anything is broken. Check the version against what you uploaded
every time; it is the one line of this step's output that matters.

Check <https://pypi.org/project/reeltime/> — the GIF, the screenshot, the
tables, the links.

---

## Patch releases when `main` has moved on

`main` usually carries unreleased feature work, so a fix cannot ship from it
without dragging that work along. Branch from the **release commit** — not from
the tag, which may not be where you think it is, and not from `main`:

```bash
git checkout -b hotfix/X.Y.Z+1 <the-release-commit-of-X.Y.Z>
```

Apply the fix, bump, changelog, and follow this file from step 2. Afterwards:

- **Do not merge the branch into `main`.** If the fix is already on `main` as
  part of the feature work, merging only drags the version backwards. Add the
  released section to `main`'s `CHANGELOG.md` by hand so the history stays
  continuous, and leave the branch unmerged.
- If the fix is *not* already on `main`, cherry-pick just the fix.

This is how `0.3.1` shipped: `main` held the MCP adapter, the fix belonged to
`0.3.x`, and the branch came off `6b32363` rather than off `v0.3.0`.

## If something is wrong after publishing

- **Broken README or GIF:** there is no way to re-render an existing release.
  Fix it and bump.
- **Broken code:** upload the fix as the next patch version, then
  `pip download reeltime==<bad>` to confirm what actually shipped, and yank the
  bad one from the PyPI web UI. Yanking hides it from resolvers without
  deleting it, so anyone who pinned it still resolves.
- **Wrong name entirely:** the name is claimed for good. Pick another and leave
  a final release under the old one pointing at it.

---

## First publication (done once)

Kept because the *ordering* here is not obvious and cost a version number to
learn. None of it is needed again unless the project is renamed or re-homed.

**Credentials.** `.pypirc` is configured for both indexes, so nothing needs
handling per release. PyPI and TestPyPI are separate accounts with separate
tokens. The first upload to a project that does not exist yet needs an
account-scoped token; swap it for a project-scoped one afterwards. To keep a
token out of shell history: `read -rs TWINE_PASSWORD && export TWINE_PASSWORD`
with `TWINE_USERNAME=__token__`. A **403** from TestPyPI usually means the token
was revoked — ours was, once, after being pasted into a chat.

**The GitHub repo comes before the GIF, and the GIF before any upload.** Three
steps that must happen in this order:

1. Push to GitHub with a fenced terminal transcript in the README instead of an
   image. A relative image path does not render on PyPI, and an absolute
   `raw.githubusercontent.com` URL 404s until the repo exists.

   ```bash
   gh repo create reeltime --public --source=. --remote=origin \
     --description "Deterministic record/replay debugger for LLM agents" --push
   ```

2. Record the GIF and push it. `vhs demo.tape` from the repo root drives
   `examples/truncation_bug.py`, which embeds a mock provider — so it costs
   nothing, comes out the same for everyone, and records into a temp directory
   rather than an existing `.tape/`. Watch it once before committing, then
   confirm the raw URL resolves.

3. Point the README at the absolute GIF URL and push. **Keep the transcript
   underneath it** — it is what readers get when images are blocked, and it is
   searchable, so someone googling a `TRUNCATED` line can land on the page.

   Why all of this precedes the first upload: PyPI renders the README once, at
   upload, and stores the result forever. A GIF URL that 404s at that moment
   stays broken on the project page until a new version is uploaded. GitHub
   re-renders on every view, so fixing it there is free.

**If the name is taken on TestPyPI** (it is not moderated, so squatting is
common), append a suffix in `pyproject.toml` for that step only — rebuild,
upload, revert. Never leave a changed name in the tree.

**Once the project page exists:** add the repo topics (`llm`, `agents`,
`debugging`, `record-replay`, `openai`, `anthropic`, `python`), and set the
GitHub description and website to the PyPI URL.
