# Releasing reeltime

Runbook for v0.1.0. **The order matters** — step 4 explains why.

Everything up to step 2 has already been done for v0.1.0: tests pass, the
artifacts are built in `dist/`, and `twine check` passes. Pick up at step 3.

---

## 0. Pre-flight

```bash
cd ~/newproject/reeltime
pytest                                    # 361 passed, 1 skipped
python -m pytest --cov --cov-report=term  # core/ at 93%
python examples/m3_replay_speed.py        # the numbers the README quotes
```

Confirm the version is in exactly two places and they agree:

```bash
grep -n '^version' pyproject.toml         # version = "0.1.0"
grep -n '__version__' src/reeltime/__init__.py
```

## 1. Build and check

```bash
rm -rf dist build
python -m build
python -m twine check dist/*              # both must say PASSED
```

Verify the wheel carries the sitecustomize shim — `tape run` silently records
nothing without it:

```bash
python -c "import zipfile; print('reeltime/_bootstrap/sitecustomize.py' in \
zipfile.ZipFile('dist/reeltime-0.1.0-py3-none-any.whl').namelist())"
```

And that a clean install works from the wheel alone:

```bash
uv venv /tmp/rt && VIRTUAL_ENV=/tmp/rt uv pip install dist/*.whl openai
cd $(mktemp -d) && /tmp/rt/bin/tape run python ~/newproject/reeltime/examples/truncation_bug.py
```

## 2. Get tokens

- **PyPI**: https://pypi.org/manage/account/token/ — scope it to "Entire
  account" for the first upload (a project-scoped token cannot create a project
  that does not exist yet). After v0.1.0 is up, replace it with a
  project-scoped token.
- **TestPyPI**: https://test.pypi.org/manage/account/token/ — a separate
  account and a separate token.

Keep them out of shell history:

```bash
read -rs TWINE_PASSWORD && export TWINE_PASSWORD
export TWINE_USERNAME=__token__
```

## 3. (a) Push to GitHub, with the text transcript

The README currently shows a fenced terminal transcript rather than a GIF. That
is deliberate: a relative image path does not render on PyPI, and an absolute
`raw.githubusercontent.com` URL 404s until the repo exists. So GitHub goes
first.

```bash
gh repo create reeltime --public --source=. --remote=origin \
  --description "Deterministic record/replay debugger for LLM agents" \
  --push
```

Then check https://github.com/vedanth2406/reeltime renders the README, the
transcript is legible, and the box-drawing characters line up.

## 4. (b) Generate the GIF and push it

```bash
brew install vhs            # or: go install github.com/charmbracelet/vhs@latest
vhs demo.tape               # writes demo.gif at the repo root
```

Run it from the repo root. The tape drives `examples/truncation_bug.py`, which
embeds a mock provider, so recording costs nothing and comes out the same for
everyone; it records into a temp directory, so an existing `.tape/` is left
alone.

Watch the result once before committing. Then:

```bash
git add demo.gif && git commit -m "docs: add demo gif" && git push
```

Confirm the raw URL resolves — this is the thing step 5 depends on:

```bash
curl -sI https://raw.githubusercontent.com/vedanth2406/reeltime/main/demo.gif \
  | head -1        # expect: HTTP/2 200
```

## 5. (c) Point the README at the absolute GIF URL and push

Replace the HTML comment and the ```` ```console ```` transcript block at the top of
`README.md` with:

```markdown
![reeltime: record an agent, replay it offline, and see what the model actually read](https://raw.githubusercontent.com/vedanth2406/reeltime/main/demo.gif)
```

Keep the transcript underneath the GIF rather than deleting it. It is what
readers get when images are blocked, and it is searchable — someone googling a
`TRUNCATED` line should be able to land on this page.

```bash
git commit -am "README: use the hosted GIF" && git push
```

**Why this must happen before any real upload:** PyPI renders the README once,
at upload, and stores the result. It never re-renders it. A GIF URL that 404s at
that moment stays broken on the project page forever — the only fix is uploading
a new version. GitHub, by contrast, re-renders on every view, so fixing it there
is free.

## 6. TestPyPI, to check metadata and rendering cheaply

```bash
python -m twine upload --repository testpypi dist/*
```

If the name is already taken on TestPyPI (it is not moderated, so squatting is
common), append a suffix in `pyproject.toml` for this step only — for example
`name = "reeltime-vd-test"` — rebuild, upload, then revert. Do **not** leave a
changed name in the tree.

Then open https://test.pypi.org/project/reeltime/ and check, in this order:

1. **The GIF renders.** If it does not, the URL from step 4 is wrong, and the
   real upload would bake that in.
2. **The description renders as Markdown**, not as raw text. Raw text means
   `readme = "README.md"` lost its content-type — stop and fix.
3. **The three badges resolve.** The PyPI version badge will read "not found"
   until the real upload; that one is expected.
4. **The tables have borders** — the comparison table and the tier table are the
   two that matter most.
5. **The sidebar** shows the MIT license, `Requires: Python >=3.9`, and all four
   project links (Homepage, Repository, Issues, Changelog).
6. **The classifiers** list Development Status 4, Debuggers, Testing, Typed.

Install from TestPyPI to be sure the artifact is sound. Dependencies come from
real PyPI, hence the extra index:

```bash
uv venv /tmp/rt-test
VIRTUAL_ENV=/tmp/rt-test uv pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  reeltime openai
cd $(mktemp -d)
/tmp/rt-test/bin/tape --version
/tmp/rt-test/bin/tape run python ~/newproject/reeltime/examples/truncation_bug.py
/tmp/rt-test/bin/tape show $(ls .tape/runs | head -1 | cut -c1-8) 1 --context --diff 0
```

That last command is the whole product in one line. If it prints the
`TRUNCATED` diff, ship it.

## 7. (d) Publish to real PyPI

```bash
python -m twine upload dist/*
```

Irreversible in the ways that matter: the version number `0.1.0` can never be
reused, and the rendered README is frozen at this moment. Deleting a release
does not free the version.

```bash
git tag -a v0.1.0 -m "reeltime 0.1.0" && git push origin v0.1.0
gh release create v0.1.0 --title "reeltime 0.1.0" --notes-from-tag
```

## 8. After

```bash
uv venv /tmp/rt-live && VIRTUAL_ENV=/tmp/rt-live uv pip install reeltime openai
/tmp/rt-live/bin/tape --version
```

- Check https://pypi.org/project/reeltime/ — the GIF, the tables, the links.
- Add the repo topics: `llm`, `agents`, `debugging`, `record-replay`,
  `openai`, `anthropic`, `python`.
- Set the GitHub repo description and website to the PyPI URL.
- Bump `src/reeltime/__init__.py` and `pyproject.toml` to `0.2.0.dev0` so `main`
  is never mistaken for the released version.

## If something is wrong after publishing

- **Broken README or GIF:** fix it, bump to `0.1.1`, upload again. There is no
  way to re-render an existing release.
- **Broken code:** `twine upload` the fix as `0.1.1`, then
  `pip download reeltime==0.1.0` to confirm what shipped, and yank the bad
  release from the PyPI web UI. Yanking hides it from resolvers without deleting
  it, so anyone who pinned it still resolves.
- **Wrong name entirely:** the name is claimed for good. Pick another, and leave
  a final release under the old one pointing at it.

## Notes for the next release

- `CHANGELOG.md` is the source of truth for release notes; write it before
  tagging, not after.
- `src/reeltime/core/decoders/pricing.py` carries a `CHECKED` date. Re-verify it
  against the provider pricing pages each release and update the date — a stale
  price is a confidently wrong number in someone's cost report.
- The version lives in two files. Keep them in step.
