# Releasing

The sequence that has actually worked, in order. [`RELEASE.md`](RELEASE.md) is
the longer first-publication runbook from v0.1.1 — one-time setup, tokens, the
GIF. This file is the one to follow for every release after that.

**The rule this file exists for:** build from the commit you tag, and check the
tag against the published artifact before you announce it. `v0.3.0` was tagged
from a commit that landed *after* its artifact was built, so the tag carried
~2000 lines of code the release did not have. Nothing caught it, because none
of this was written down.

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

The GIF is referenced by an absolute `raw.githubusercontent.com` URL. It must
resolve *before* the first upload of a version, or fixing it costs a version
bump:

```bash
curl -sI https://raw.githubusercontent.com/vedanth2406/reeltime/main/demo.gif | head -1
```

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

If the name is taken on TestPyPI, rename in `pyproject.toml` for this step only
and revert immediately. **Never leave a changed name in the tree.**

## 5. Real PyPI

```bash
python -m twine upload dist/*
```

Irreversible in the ways that matter: the version can never be reused, and the
rendered README is frozen at this moment. Deleting a release does not free the
version.

## 6. Tag the commit you built from — and prove it

```bash
git push
git tag -a vX.Y.Z -m "reeltime X.Y.Z" && git push origin vX.Y.Z
gh release create vX.Y.Z --title "reeltime X.Y.Z" --notes-from-tag
```

Then verify the tag against what PyPI is actually serving. The only difference
should be `PKG-INFO`, which the build generates:

```bash
python - <<'EOF' > /tmp/published.txt
import json, urllib.request, tarfile, io, hashlib
V = "X.Y.Z"
d = json.load(urllib.request.urlopen(
    "https://pypi.org/pypi/reeltime/{}/json".format(V)))
sd = [u for u in d["urls"] if u["packagetype"] == "sdist"][0]
raw = urllib.request.urlopen(sd["url"]).read()
assert hashlib.sha256(raw).hexdigest() == sd["digests"]["sha256"]
tf = tarfile.open(fileobj=io.BytesIO(raw))
print("\n".join(sorted(n.split("/", 1)[1] for n in tf.getnames() if "/" in n)))
EOF

git ls-tree -r --name-only vX.Y.Z | sort > /tmp/tagged.txt
comm -3 /tmp/published.txt /tmp/tagged.txt     # expect: PKG-INFO, nothing else
```

Anything else in that output means the tag and the release disagree. Move the
tag to the commit you built from rather than leaving it wrong:

```bash
git tag -f -a vX.Y.Z <the-build-commit> && git push -f origin vX.Y.Z
```

## 7. After

```bash
uv venv /tmp/rt-live && VIRTUAL_ENV=/tmp/rt-live uv pip install reeltime
/tmp/rt-live/bin/tape --version
```

Check <https://pypi.org/project/reeltime/> — the GIF, the tables, the links.

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
- **Broken code:** upload the fix as the next patch version, then yank the bad
  one from the PyPI web UI. Yanking hides it from resolvers without deleting
  it, so anyone who pinned it still resolves.
