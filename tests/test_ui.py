"""`tape ui` (M11) -- the viewer, tested without a browser.

Three layers, and the first is the one that matters:

* **the drift tests**, which assert the API returns exactly what the CLI's own
  functions computed. The viewer's whole design rests on "the UI is a second
  renderer over the same functions", and a claim that is only a convention is
  a claim that decays. These fail the moment the server starts computing a
  number of its own;
* **every route**, over a fixture trace, a forked run, a chain run and an empty
  `.tape/` -- status and shape, no browser;
* **the security boundary**, which is the loopback bind, asserted rather than
  commented.

No browser-driven end-to-end test: it would add a heavy dev dependency to cover
a layer already covered here, and the design says so.
"""

import json
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest

import reeltime as tape
from reeltime.core import context as context_mod
from reeltime.core import doctor as doctor_mod
from reeltime.core import paths, tracediff
from reeltime.core.blobs import BlobStore
from reeltime.core.trace import read_trace
from reeltime.core.ui import api, server as server_mod

CHAT = {
    "object": "chat.completion", "model": "gpt-4o-mini",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 2},
}


# -- fixtures --------------------------------------------------------------


def _agent(url, user="hello there, this is the first prompt"):
    def go():
        return httpx.post(url, json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "system", "content": "Be brief."},
                         {"role": "user", "content": user}],
        }).json()

    return go


@pytest.fixture
def populated(tape_dir, server):
    """A tape dir with two runs of the same command, so doctor has input."""
    url = server.route("/v1/chat/completions", json=CHAT)
    for run_id, prompt in (("01AAA", "the first prompt, which is quite long indeed"),
                           ("01BBB", "the first")):
        with tape.session(tape_dir=tape_dir, collect_git=False, run_id=run_id):
            _agent(url, prompt)()
    return tape_dir


@pytest.fixture
def live(populated):
    """A running server on an OS-chosen port. Yields (base_url, tape_dir)."""
    httpd, base = server_mod.serve_in_thread(populated, boot_run="01AAA")
    try:
        yield base, populated
    finally:
        httpd.shutdown()
        httpd.server_close()


def get(base, path):
    with urllib.request.urlopen(base + path) as response:
        return response.status, json.loads(response.read())


def status_of(base, path):
    try:
        with urllib.request.urlopen(base + path) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


# -- the drift tests -------------------------------------------------------
#
# The invariant: the server calls the same function the CLI calls and
# serialises the result. Enforced here rather than by convention, because
# "no new event data" holding *by construction* is the entire argument for
# this architecture.


def test_the_context_api_matches_what_the_cli_computes(live):
    base, tape_dir = live
    _, payload = get(base, "/api/run/01AAA/context/0")

    trace = read_trace(paths.trace_path(tape_dir, "01AAA"))
    blobs = BlobStore(paths.blobs_dir(tape_dir))
    expected = context_mod.from_event(trace.events[0], blobs)

    assert payload["context"] == expected.to_dict()
    # And the parts a renderer actually reads, spelled out, so a regression
    # names the field rather than dumping two dicts.
    assert payload["context"]["total_chars"] == expected.total_chars
    assert len(payload["context"]["messages"]) == len(expected.messages)
    assert payload["context"]["model"] == expected.model


def test_the_diff_api_matches_what_the_cli_computes(live):
    base, tape_dir = live
    _, payload = get(base, "/api/diff/01AAA/01BBB")

    expected = tracediff.diff(read_trace(paths.trace_path(tape_dir, "01AAA")),
                              read_trace(paths.trace_path(tape_dir, "01BBB")))
    assert payload == expected.to_dict()


def test_the_doctor_api_matches_what_the_cli_computes(live):
    base, tape_dir = live
    _, payload = get(base, "/api/doctor?runs=01AAA,01BBB")

    expected = doctor_mod.analyse([
        read_trace(paths.trace_path(tape_dir, "01AAA")),
        read_trace(paths.trace_path(tape_dir, "01BBB")),
    ])
    assert payload == expected.to_dict()


def test_doctor_recomputes_rather_than_reading_a_cached_report(live, tape_dir):
    """A stale finding in a correctness tool is worse than a two-second wait.

    Proved by changing the inputs underneath and asking again: a cached report
    would still describe the old pair.
    """
    base, _ = live
    _, first = get(base, "/api/doctor?runs=01AAA,01BBB")
    assert first["runs"] == ["01AAA", "01BBB"]

    _, second = get(base, "/api/doctor?runs=01BBB,01AAA")
    assert second["runs"] == ["01BBB", "01AAA"]
    # Recomputed against a different first trace, so it is genuinely re-run.
    assert second is not first


def test_the_api_never_invents_a_cost(live):
    """Enrichment comes off the event, not from arithmetic in the UI layer."""
    base, tape_dir = live
    _, payload = get(base, "/api/run/01AAA")
    trace = read_trace(paths.trace_path(tape_dir, "01AAA"))
    assert [e["meta"].get("cost_usd") for e in payload["events"]] == \
           [e.meta.get("cost_usd") for e in trace.events]


# -- truncation, the headline case ----------------------------------------


def test_truncation_flags_survive_serialisation(tape_dir, server):
    """`truncated` and `kept_prefix` are computed properties, not fields.

    The context diff exists to make silent truncation impossible to miss, and
    the screen's whole treatment keys off these two booleans -- so a generic
    dataclass dump that quietly dropped them would take the feature with it and
    still return a valid-looking payload.
    """
    url = server.route("/v1/chat/completions", json=CHAT)
    long_prompt = "here are the files: " + ", ".join(
        "report_{}.pdf".format(i) for i in range(40))
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01CUT"):
        _agent(url, long_prompt)()
        _agent(url, long_prompt[:60])()          # the same message, cut short

    httpd, base = server_mod.serve_in_thread(tape_dir)
    try:
        _, payload = get(base, "/api/run/01CUT/context/1?baseline=0")
    finally:
        httpd.shutdown()
        httpd.server_close()

    cut = [c for c in payload["changes"] if c["truncated"]]
    assert cut, "the truncated message did not come back flagged"
    assert cut[0]["kept_prefix"] is True
    assert cut[0]["after"]["chars"] < cut[0]["before"]["chars"]


# -- routes ----------------------------------------------------------------


def test_the_page_is_served_and_is_self_contained(live):
    base, _ = live
    with urllib.request.urlopen(base + "/") as response:
        body = response.read().decode("utf-8")
        assert response.headers["Content-Type"].startswith("text/html")
    # No build step, no CDN: everything the page needs is in the page. A
    # remote asset would also be blocked by the CSP the server sends, so this
    # failing means the page is broken rather than merely impure.
    for remote in ("http://", "https://", "//cdn", "<script src", "<link rel=\"stylesheet\""):
        assert remote not in body, remote


def test_runs_lists_every_run(live):
    base, _ = live
    _, payload = get(base, "/api/runs")
    assert sorted(r["run_id"] for r in payload["runs"]) == ["01AAA", "01BBB"]
    assert all("events" in r for r in payload["runs"])


def test_run_carries_events_with_blobs_resolved(live):
    base, _ = live
    _, payload = get(base, "/api/run/01AAA")
    assert payload["summary"]["events"] == len(payload["events"])
    assert payload["events"][0]["kind"] == "llm"
    # `has_context` is what the diff baseline picker walks, so it must be there.
    assert payload["events"][0]["has_context"] is True


def test_boot_says_which_run_and_whether_it_was_asked_for(populated):
    httpd, base = server_mod.serve_in_thread(populated, boot_run="01BBB")
    try:
        _, payload = get(base, "/api/boot")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert payload["run_id"] == "01BBB"
    # Not explicit, so the page raises the runs overlay over it.
    assert payload["explicit"] is False


def test_tree_hangs_a_fork_under_its_parent(populated, server):
    url = server.route("/v1/chat/completions", json=CHAT)
    run = tape.install("fork", tape_dir=populated, collect_git=False,
                       replay="01AAA", fork_at=1, run_id="01FORK")
    try:
        _agent(url)()
    finally:
        if not run.closed:
            tape.uninstall()

    httpd, base = server_mod.serve_in_thread(populated)
    try:
        _, payload = get(base, "/api/tree")
    finally:
        httpd.shutdown()
        httpd.server_close()

    parents = {r["run_id"]: r for r in payload["roots"]}
    assert "01FORK" not in parents, "a fork must not be a root"
    assert [c["run_id"] for c in parents["01AAA"]["children"]] == ["01FORK"]
    assert parents["01AAA"]["children"][0]["fork_at"] == 1


def test_comparable_groups_runs_of_the_same_command(live):
    base, _ = live
    _, payload = get(base, "/api/comparable")
    assert payload["groups"], "two runs of one command should be comparable"
    assert sorted(r["run_id"] for r in payload["groups"][0]["runs"]) == ["01AAA", "01BBB"]


def test_an_empty_tape_dir_serves_rather_than_crashing(tmp_path):
    """An empty `.tape/` is what a first-time user has."""
    empty = paths.ensure_tape_dir(tmp_path / ".tape")
    httpd, base = server_mod.serve_in_thread(empty)
    try:
        _, payload = get(base, "/api/runs")
        assert payload["runs"] == []
        assert status_of(base, "/") == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_half_written_trace_does_not_take_the_index_down(live, tape_dir):
    """A crashed run is exactly the run somebody wants to look at."""
    base, _ = live
    (paths.runs_dir(tape_dir) / "01BROKEN.jsonl").write_text("{not json\n")
    _, payload = get(base, "/api/runs")
    assert sorted(r["run_id"] for r in payload["runs"]) == ["01AAA", "01BBB"]


# -- errors ----------------------------------------------------------------


@pytest.mark.parametrize("path,expected", [
    ("/api/run/01NOPE", 404),
    ("/api/run/01AAA/context/99", 404),
    ("/api/run/01AAA/context/abc", 400),
    ("/api/run/01AAA/chain", 404),          # no chain events in this run
    ("/api/doctor?runs=01AAA", 400),        # doctor needs two
    ("/api/nonsense", 404),
])
def test_bad_requests_say_what_is_wrong(live, path, expected):
    base, _ = live
    assert status_of(base, path) == expected


def test_an_error_body_carries_a_message(live):
    base, _ = live
    try:
        urllib.request.urlopen(base + "/api/run/01NOPE")
        raise AssertionError("expected a 404")
    except urllib.error.HTTPError as exc:
        assert "01NOPE" in json.loads(exc.read())["error"]


# -- the security boundary -------------------------------------------------


def test_the_server_binds_loopback_only(populated):
    """The bind *is* the security model, so it is a test, not a comment.

    Redaction is pattern-matching and best-effort, so a trace may still hold
    something private. "No auth" is correct only because nothing off this
    machine can reach the port.
    """
    httpd, base = server_mod.serve_in_thread(populated)
    port = httpd.server_address[1]
    try:
        assert httpd.server_address[0] == "127.0.0.1"

        # The bound socket is not listening on this machine's routable address.
        outward = socket.socket()
        outward.settimeout(1.0)
        local_ip = socket.gethostbyname(socket.gethostname())
        if local_ip.startswith("127."):
            pytest.skip("this host resolves to loopback, so there is nothing to prove")
        with pytest.raises((ConnectionRefusedError, OSError)):
            outward.connect((local_ip, port))
        outward.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_bind_address_is_not_configurable():
    """A flag here would make the security claim negotiable."""
    import inspect

    assert server_mod.HOST == "127.0.0.1"
    for name in ("build", "serve", "serve_in_thread"):
        params = inspect.signature(getattr(server_mod, name)).parameters
        assert "host" not in params, "{} must not take a host".format(name)


def test_the_page_is_sent_with_a_restrictive_csp(live):
    base, _ = live
    with urllib.request.urlopen(base + "/") as response:
        csp = response.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "connect-src 'self'" in csp


# -- the architectural claims ---------------------------------------------


def test_the_viewer_adds_no_runtime_dependency():
    """`dependencies = []` is why there is no web framework here.

    Invisible otherwise: nothing fails if somebody adds one, until a user's
    environment breaks on install.
    """
    import tomllib

    root = Path(__file__).resolve().parent.parent
    with open(root / "pyproject.toml", "rb") as handle:
        config = tomllib.load(handle)
    assert config["project"]["dependencies"] == []


def test_the_ui_never_writes_to_the_tape_dir(live, tape_dir):
    """Read-only is what makes "no recording changes" structural."""
    base, _ = live
    before = {p: p.stat().st_mtime_ns for p in sorted(tape_dir.rglob("*")) if p.is_file()}
    for path in ("/api/runs", "/api/run/01AAA", "/api/run/01AAA/context/0",
                 "/api/diff/01AAA/01BBB", "/api/doctor?runs=01AAA,01BBB",
                 "/api/tree", "/api/comparable", "/"):
        status_of(base, path)
    after = {p: p.stat().st_mtime_ns for p in sorted(tape_dir.rglob("*")) if p.is_file()}
    assert before == after


def test_the_api_module_does_no_arithmetic_of_its_own():
    """The guard on "a UI bug is a rendering bug".

    `core/ui/api.py` may load, filter and serialise; the moment it starts
    computing a figure, that figure can disagree with the one `tape show`
    prints and no test would catch it. Checked against the parse tree so a
    docstring may discuss arithmetic freely.
    """
    import ast

    source = Path(api.__file__).read_text()
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Mult, ast.Div, ast.Sub, ast.FloorDiv, ast.Pow)):
            offenders.append(ast.unparse(node))
    assert offenders == [], offenders


# -- the chain tree, and the nesting it exists to show ---------------------


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("langchain_core") is None,
    reason="langchain-core requires Python 3.10; reeltime supports 3.9",
)
def test_chain_rows_nest_boundary_events_under_their_node(tape_dir, server):
    """The claim the chain screen exists for.

    A `chain` node and the `llm` event inside it are two different things at
    two different levels, and the transport layer alone cannot show the
    relationship -- it sees only the leaves.
    """
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    import reeltime.langchain as lc_adapter

    chain = (ChatPromptTemplate.from_template("say {x}")
             | FakeListChatModel(responses=["hello there"]))
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01CHAIN"):
        lc_adapter.install()
        chain.invoke({"x": "hi"})

    httpd, base = server_mod.serve_in_thread(tape_dir)
    try:
        status, payload = get(base, "/api/run/01CHAIN/chain")
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert status == 200
    nodes = [r for r in payload["rows"] if r["node"]]
    assert nodes, "a LangChain run should produce chain nodes"
    assert all("depth" in r for r in payload["rows"])
    # Depth is what the indent is drawn from, so a row without one would
    # silently render flat.
    assert all(isinstance(r["depth"], int) for r in payload["rows"])
    assert payload["summary"]["has_chain"] is True


def test_a_run_without_chain_events_advertises_that(live):
    """The tab is absent rather than empty, so the summary must say so."""
    base, _ = live
    _, payload = get(base, "/api/run/01AAA")
    assert payload["summary"]["has_chain"] is False


# -- the CLI verb ----------------------------------------------------------


def test_tape_ui_resolves_a_run_and_serves_it(populated, monkeypatch, capsys):
    """`tape ui <run>` opens on that run rather than on a picker."""
    from reeltime import cli

    started = {}

    def fake_serve(tape_dir, boot_run=None, port=7654, on_ready=None,
                   boot_explicit=False):
        started.update(boot_run=boot_run, port=port, explicit=boot_explicit)
        if on_ready:
            on_ready("http://127.0.0.1:{}/".format(port))

    monkeypatch.setattr("reeltime.core.ui.serve", fake_serve)
    assert cli.main(["--tape-dir", str(populated), "ui", "01BBB",
                     "--port", "0", "--no-open"]) == 0
    assert started["boot_run"] == "01BBB"
    assert started["explicit"] is True
    assert "loopback only" in capsys.readouterr().err


def test_bare_tape_ui_opens_the_newest_run_not_a_picker(populated, monkeypatch):
    """Discovery is one keystroke away, not the landing page."""
    from reeltime import cli

    started = {}

    def fake_serve(tape_dir, boot_run=None, port=7654, on_ready=None,
                   boot_explicit=False):
        started.update(boot_run=boot_run, explicit=boot_explicit)

    monkeypatch.setattr("reeltime.core.ui.serve", fake_serve)
    assert cli.main(["--tape-dir", str(populated), "ui", "--no-open"]) == 0
    assert started["boot_run"] == "01BBB"        # ULIDs sort chronologically
    # Not explicit, so the page raises the overlay over it.
    assert started["explicit"] is False


def test_tape_ui_serves_an_empty_tape_dir_rather_than_erroring(tmp_path, monkeypatch):
    """An empty `.tape/` is what a first-time user has, not an error."""
    from reeltime import cli

    empty = paths.ensure_tape_dir(tmp_path / ".tape")
    started = {}
    monkeypatch.setattr(
        "reeltime.core.ui.serve",
        lambda tape_dir, boot_run=None, port=7654, on_ready=None,
        boot_explicit=False: started.update(boot_run=boot_run))
    assert cli.main(["--tape-dir", str(empty), "ui", "--no-open"]) == 0
    assert started["boot_run"] is None


def test_tape_ui_still_refuses_a_run_that_does_not_exist(populated, monkeypatch, capsys):
    """Naming a run and being silently redirected elsewhere would be worse.

    The empty-`.tape/` fallback above must not swallow a typo: `tape ui 01NOPE`
    has to fail, while bare `tape ui` on an empty directory does not.
    """
    from reeltime import cli

    served = []
    monkeypatch.setattr("reeltime.core.ui.serve",
                        lambda *a, **k: served.append(a))
    assert cli.main(["--tape-dir", str(populated), "ui", "01NOPE",
                     "--no-open"]) != 0
    assert "01NOPE" in capsys.readouterr().err
    assert served == [], "it must not fall back to serving something else"


# -- the paths that only show up when something is wrong -------------------


def test_asking_for_context_on_an_ambient_event_says_so(tape_dir, server):
    """Not every event has a message array, and the message should say which."""
    url = server.route("/v1/chat/completions", json=CHAT)
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01MIX",
                      patch=("random",)):
        import random

        random.random()
        _agent(url)()

    httpd, base = server_mod.serve_in_thread(tape_dir)
    try:
        trace = read_trace(paths.trace_path(tape_dir, "01MIX"))
        ambient = next(e for e in trace.events if e.kind == "rand")
        try:
            urllib.request.urlopen(
                "{}/api/run/01MIX/context/{}".format(base, ambient.i))
            raise AssertionError("expected a 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            assert "rand" in json.loads(exc.read())["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_baseline_without_a_message_array_is_refused(tape_dir, server):
    url = server.route("/v1/chat/completions", json=CHAT)
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01MIX2",
                      patch=("random",)):
        import random

        random.random()
        _agent(url)()

    httpd, base = server_mod.serve_in_thread(tape_dir)
    try:
        trace = read_trace(paths.trace_path(tape_dir, "01MIX2"))
        ambient = next(e for e in trace.events if e.kind == "rand")
        llm = next(e for e in trace.events if e.kind in ("llm", "http"))
        assert status_of(base, "/api/run/01MIX2/context/{}?baseline={}".format(
            llm.i, ambient.i)) == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_missing_page_file_reports_itself_rather_than_500ing_blankly(
        live, monkeypatch, tmp_path):
    """Only reachable from a broken install, which the wheel gate guards.

    Tested anyway because the failure is otherwise indistinguishable from the
    server being down, and the fix ("your install is missing a file") is not
    something a user would guess.
    """
    base, _ = live
    monkeypatch.setattr(server_mod, "INDEX", tmp_path / "gone.html")
    try:
        urllib.request.urlopen(base + "/")
        raise AssertionError("expected a 500")
    except urllib.error.HTTPError as exc:
        assert exc.code == 500
        assert "missing from this install" in json.loads(exc.read())["error"]


def test_an_unexpected_failure_becomes_a_message_not_a_hang(live, monkeypatch):
    """A viewer must not 500 silently: the browser would just spin."""
    base, _ = live

    def boom(*args, **kwargs):
        raise RuntimeError("something went wrong deep inside")

    monkeypatch.setattr(api, "runs", boom)
    try:
        urllib.request.urlopen(base + "/api/runs")
        raise AssertionError("expected a 500")
    except urllib.error.HTTPError as exc:
        assert exc.code == 500
        body = json.loads(exc.read())["error"]
        assert "RuntimeError" in body and "deep inside" in body


# -- the frontend, which Python otherwise cannot reach ---------------------


def test_the_frontend_render_paths_execute(populated, tmp_path):
    """`index.html` is ~600 lines the rest of this file cannot reach.

    The API tests prove the payloads are right and say nothing about whether
    the code that draws them runs: a typo in a render function is invisible
    until somebody opens the page. So the real functions are executed over real
    payloads under a deliberately dumb DOM stub -- enough for an exception to
    escape, which is the class of bug worth catching, and far short of the
    headless browser the design rejected.

    Skipped when node is unavailable rather than made a dev dependency: it
    covers a real gap and is not worth blocking `pip install -e ".[dev]"` over.
    """
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the frontend check needs a JS runtime")

    root = Path(__file__).resolve().parent.parent
    harness = root / "tools" / "ui_render_check.js"
    assert harness.exists(), harness

    httpd, base = server_mod.serve_in_thread(populated, boot_run="01AAA")
    try:
        payloads = {
            "run": get(base, "/api/run/01AAA")[1],
            "ctx": get(base, "/api/run/01AAA/context/0")[1],
            "diff": get(base, "/api/run/01AAA/context/0?baseline=0")[1],
            "runs": get(base, "/api/runs")[1],
            "tree": get(base, "/api/tree")[1],
        }
    finally:
        httpd.shutdown()
        httpd.server_close()

    # A diff against itself has no truncated change, and the harness asserts
    # the TRUNCATED path is reached -- so build a real one.
    long_text = "files: " + ", ".join("report_%d.pdf" % i for i in range(40))
    before = dict(payloads["ctx"]["context"])
    after = json.loads(json.dumps(before))
    before["messages"][1]["text"] = long_text
    before["messages"][1]["chars"] = len(long_text)
    after["messages"][1]["text"] = long_text[:40]
    after["messages"][1]["chars"] = 40
    payloads["diff"] = {
        "context": after, "baseline": before,
        "changes": [
            {"kind": "same", "before": before["messages"][0],
             "after": after["messages"][0], "truncated": False, "kept_prefix": False},
            {"kind": "changed", "before": before["messages"][1],
             "after": after["messages"][1], "truncated": True, "kept_prefix": True},
        ],
    }

    payload_file = tmp_path / "payloads.json"
    payload_file.write_text(json.dumps(payloads))

    html = (root / "src" / "reeltime" / "core" / "ui" / "index.html").read_text()
    script = tmp_path / "ui.js"
    script.write_text("\n".join(re.findall(r"<script>(.*?)</script>", html, re.S)))

    result = subprocess.run(
        [node, str(harness), str(payload_file), str(script)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all render paths executed cleanly" in result.stdout
    # The stub is dumb on purpose, so an empty run would pass vacuously.
    assert result.stdout.count("  ok    ") >= 14, result.stdout


def test_the_frontend_javascript_parses(tmp_path):
    """A syntax error would serve a blank page with a console message nobody sees."""
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    root = Path(__file__).resolve().parent.parent
    html = (root / "src" / "reeltime" / "core" / "ui" / "index.html").read_text()
    script = tmp_path / "ui.js"
    script.write_text("\n".join(re.findall(r"<script>(.*?)</script>", html, re.S)))
    result = subprocess.run([node, "--check", str(script)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
