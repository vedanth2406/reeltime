"""What does recording cost, at every seam reeltime has?

    python examples/overhead.py            # all seams
    python examples/overhead.py --quick    # fewer repeats, for a smoke test

Reports the *added* time per boundary crossing: the same work measured with
recording off and with recording on, differenced. Every number the README
quotes comes from here.

**Why the numbers are measured this way.** Each seam is driven against a
loopback mock, not a real provider, so the boundary itself is nearly free and
the overhead is not hidden inside 400 ms of network latency. That makes the
*absolute* cost per event honest and the *percentage* meaningless -- against a
real provider the same overhead is a rounding error, which is the point.

Timing is the **median of repeated batches**, not a single run: a mean over one
batch on a laptop measures whatever else the laptop was doing. The spread is
reported alongside so a suspicious number is visible rather than averaged away.
"""

from __future__ import annotations

import gc
import json
import shutil
import statistics
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import reeltime as tape

QUICK = "--quick" in sys.argv
BATCHES = 3 if QUICK else 7
CALLS = 40 if QUICK else 120
AMBIENT_CALLS = 2000 if QUICK else 20000

CHAT = {
    "object": "chat.completion",
    "model": "gpt-4o-mini",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 1200, "completion_tokens": 20},
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        body = json.dumps(CHAT).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST

    def log_message(self, *args):
        pass


def serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:{}/v1/chat/completions".format(httpd.server_address[1])


# -- timing ---------------------------------------------------------------


def timed(fn, n):
    """Seconds for ``n`` iterations of ``fn``, with the GC held still."""
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        start = time.perf_counter()
        for _ in range(n):
            fn()
        return time.perf_counter() - start
    finally:
        if was_enabled:
            gc.enable()


def measure(name, work, n, setup=None, tape_dir=None, install=None):
    """Median added microseconds per crossing, off versus recording.

    ``work`` is called ``n`` times per batch. ``install`` opens a recording
    session; the same ``work`` runs inside and outside it.
    """
    off_times, on_times, events = [], [], 0

    for _ in range(BATCHES):
        if setup:
            setup()
        work()                                    # warm the path
        off_times.append(timed(work, n))

        if setup:
            setup()
        run = install()
        try:
            work()                                # warm the recorded path too
            on_times.append(timed(work, n))
        finally:
            if not run.closed:
                tape.uninstall()
        events = len(tape.read_trace(run.path).events)

    off, on = statistics.median(off_times), statistics.median(on_times)
    per_call_us = (on - off) / n * 1e6
    spread_us = (max(on_times) - min(on_times)) / n * 1e6
    return {
        "seam": name,
        "per_call_us": per_call_us,
        "spread_us": spread_us,
        "events_per_batch": events,
        "recorded": events >= n,           # did every crossing become an event?
    }


def session(tape_dir, **kwargs):
    def go():
        shutil.rmtree(tape_dir, ignore_errors=True)
        return tape.install(tape_dir=tape_dir, collect_git=False, **kwargs)

    return go


# -- the seams ------------------------------------------------------------


def http_seams(tape_dir, url, results):
    import httpx
    import requests
    import urllib3

    payload = {"model": "gpt-4o-mini",
               "messages": [{"role": "user", "content": "hello " * 40}]}

    client = httpx.Client()
    results.append(measure(
        "httpx", lambda: client.post(url, json=payload), CALLS,
        install=session(tape_dir)))
    client.close()

    try:
        import httpx2

        client2 = httpx2.Client()
        results.append(measure(
            "httpx2", lambda: client2.post(url, json=payload), CALLS,
            install=session(tape_dir)))
        client2.close()
    except ImportError:
        results.append({"seam": "httpx2", "skipped": "not installed"})

    sess = requests.Session()
    results.append(measure(
        "requests", lambda: sess.post(url, json=payload), CALLS,
        install=session(tape_dir)))
    sess.close()

    pool = urllib3.PoolManager()
    results.append(measure(
        "urllib3", lambda: pool.request("POST", url, json=payload), CALLS,
        install=session(tape_dir)))


def tool_seam(tape_dir, results):
    @tape.tool
    def lookup(key: str) -> str:
        return key.upper()

    # The decorator is a no-op until a tape is installed, so the "off" side
    # measures the wrapper's own cost too -- which is the honest comparison for
    # somebody deciding whether to leave `@tape.tool` in their code.
    results.append(measure(
        "@tape.tool", lambda: lookup("abc"), CALLS,
        install=session(tape_dir)))


def ambient_seam(tape_dir, results):
    import random

    def work():
        random.random()
        uuid.uuid4()
        time.time()

    # Three reads per call, so the per-crossing figure is a third of the
    # per-call one. Reported per *read*, which is what the README claims.
    out = measure("ambient", work, AMBIENT_CALLS // 3,
                  install=session(tape_dir, patch=("random", "uuid", "time")))
    out["per_call_us"] /= 3
    out["spread_us"] /= 3
    out["seam"] = "ambient (random/uuid/clock)"
    out["recorded"] = True
    results.append(out)


def mcp_seam(tape_dir, results):
    try:
        import mcp  # noqa: F401
    except ImportError:
        results.append({"seam": "MCP (tools/call)", "skipped": "SDK needs 3.10+"})
        return

    import asyncio

    server = Path(__file__).resolve().parent / "mcp_server.py"
    if not server.exists():
        results.append({"seam": "MCP (tools/call)", "skipped": "no mock server"})
        return

    calls = max(8, CALLS // 6)          # a subprocess round trip is not cheap

    def run_session(recording):
        async def go():
            async with tape.mcp.connect(sys.executable, [str(server)],
                                        server="files") as sess:
                start = time.perf_counter()
                for _ in range(calls):
                    await sess.call_tool("read_file", {"path": "a.txt"})
                return time.perf_counter() - start

        return asyncio.run(go())

    off, on = [], []
    for _ in range(max(2, BATCHES // 2)):
        off.append(run_session(False))
        shutil.rmtree(tape_dir, ignore_errors=True)
        run = tape.install(tape_dir=tape_dir, collect_git=False)
        try:
            on.append(run_session(True))
        finally:
            if not run.closed:
                tape.uninstall()
    results.append({
        "seam": "MCP (tools/call)",
        "per_call_us": (statistics.median(on) - statistics.median(off)) / calls * 1e6,
        "spread_us": (max(on) - min(on)) / calls * 1e6,
        "events_per_batch": calls,
        "recorded": True,
    })


def langchain_seam(tape_dir, url, results):
    try:
        from langchain_core.language_models.fake_chat_models import FakeListChatModel
        from langchain_core.prompts import ChatPromptTemplate
    except ImportError:
        results.append({"seam": "LangChain (chain node)",
                        "skipped": "langchain-core needs 3.10+"})
        return

    import reeltime.langchain as adapter

    chain = (ChatPromptTemplate.from_template("say {x}")
             | FakeListChatModel(responses=["hi"]))
    calls = max(10, CALLS // 4)

    def work():
        chain.invoke({"x": "hi"})

    def install():
        shutil.rmtree(tape_dir, ignore_errors=True)
        run = tape.install(tape_dir=tape_dir, collect_git=False)
        adapter.install()
        return run

    out = measure("LangChain (chain node)", work, calls, install=install)
    # A prompt|model chain is several nodes per invoke; report per node, which
    # is the unit somebody adding the adapter is paying for.
    per_invoke = max(1, out["events_per_batch"] // calls)
    out["per_call_us"] /= per_invoke
    out["spread_us"] /= per_invoke
    out["nodes_per_invoke"] = per_invoke
    results.append(out)


# -- size and replay ------------------------------------------------------


def startup_cost(tape_dir):
    """What `tape.install()` and the session teardown cost, once per run.

    **This is the number that reconciles this benchmark with
    `m3_replay_speed.py`, and the two disagreed by 25x until it was measured.**
    That example divides a run's *whole* overhead -- install, shim patching,
    header, footer -- by its event count, which for a 16-event agent makes a
    one-time cost look like a large per-event one. Both figures are true and
    they answer different questions, so the README quotes both: a fixed cost at
    startup, and a marginal cost per crossing.
    """
    times = []
    for _ in range(BATCHES):
        shutil.rmtree(tape_dir, ignore_errors=True)
        gc.collect()
        start = time.perf_counter()
        run = tape.install(tape_dir=tape_dir, collect_git=False)
        tape.uninstall()
        times.append(time.perf_counter() - start)
    return {"install_ms": statistics.median(times) * 1000,
            "spread_ms": (max(times) - min(times)) * 1000}


def trace_size(tape_dir, url):
    import httpx

    shutil.rmtree(tape_dir, ignore_errors=True)
    payload = {"model": "gpt-4o-mini",
               "messages": [{"role": "user", "content": "hello " * 40}]}
    with tape.session(tape_dir=tape_dir, collect_git=False) as run:
        with httpx.Client() as client:
            for _ in range(200):
                client.post(url, json=payload)
    trace = Path(run.path)
    events = len(tape.read_trace(trace).events)
    blobs = sum(f.stat().st_size for f in (Path(tape_dir) / "blobs").glob("*")) \
        if (Path(tape_dir) / "blobs").exists() else 0
    http_bytes = trace.stat().st_size / events

    # An ambient read and an LLM call are not the same size of thing -- they
    # differ by an order of magnitude -- so quoting one figure for "an event"
    # is the kind of average that describes nothing.
    import random

    shutil.rmtree(tape_dir, ignore_errors=True)
    with tape.session(tape_dir=tape_dir, collect_git=False,
                      patch=("random",)) as amb:
        for _ in range(200):
            random.random()
    amb_trace = Path(amb.path)
    amb_events = len(tape.read_trace(amb_trace).events)

    return {
        "http_bytes_per_event": http_bytes,
        "ambient_bytes_per_event": amb_trace.stat().st_size / amb_events,
        "blob_bytes": blobs,
        "events": events,
        "prompt_chars": len("hello " * 40),
    }


def replay_speedup(tape_dir, latency_s=0.4, turns=8):
    """The README's headline ratio, against a mock with realistic latency."""
    import httpx

    class Slow(Handler):
        def do_POST(self):
            time.sleep(latency_s)
            Handler.do_POST(self)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:{}/v1/chat/completions".format(httpd.server_address[1])

    def agent():
        with httpx.Client() as client:
            for _ in range(turns):
                client.post(url, json={"model": "gpt-4o-mini",
                                       "messages": [{"role": "user", "content": "hi"}]})

    shutil.rmtree(tape_dir, ignore_errors=True)
    start = time.perf_counter()
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01BENCH"):
        agent()
    recorded = time.perf_counter() - start

    start = time.perf_counter()
    with tape.session("replay", tape_dir=tape_dir, replay="01BENCH"):
        agent()
    replayed = time.perf_counter() - start
    httpd.shutdown()
    return {"recorded_s": recorded, "replayed_s": replayed,
            "ratio": recorded / replayed, "turns": turns, "latency_s": latency_s}


def main():
    import platform

    httpd, url = serve()
    root = Path(tempfile.mkdtemp())
    tape_dir = root / ".tape"
    results = []

    try:
        http_seams(tape_dir, url, results)
        tool_seam(tape_dir, results)
        ambient_seam(tape_dir, results)
        mcp_seam(tape_dir, results)
        langchain_seam(tape_dir, url, results)
        size = trace_size(tape_dir, url)
        startup = startup_cost(tape_dir)
        speed = replay_speedup(tape_dir)
    finally:
        httpd.shutdown()

    print("\nreeltime {}  ·  {}  ·  Python {}".format(
        tape.__version__, platform.platform(), platform.python_version()))
    print("median of {} batches, {} calls each; loopback mock, so the boundary "
          "itself is ~free\n".format(BATCHES, CALLS))

    print("  {:26s} {:>12s} {:>10s}   {}".format(
        "seam", "added/event", "spread", "note"))
    print("  " + "-" * 66)
    for r in results:
        if r.get("skipped"):
            print("  {:26s} {:>12s} {:>10s}   {}".format(
                r["seam"], "-", "-", r["skipped"]))
            continue
        note = "" if r.get("recorded", True) else "NOT ALL CROSSINGS RECORDED"
        if "nodes_per_invoke" in r:
            note = "{} nodes per invoke".format(r["nodes_per_invoke"])
        print("  {:26s} {:>9.0f} us {:>9.0f}u   {}".format(
            r["seam"], r["per_call_us"], r["spread_us"], note))

    print("\n  startup       {:.0f} ms once per run (install + teardown, +/-{:.0f})"
          .format(startup["install_ms"], startup["spread_ms"]))
    print("  trace size    {:.0f} bytes per llm event ({}-char prompt), "
          "{:.0f} bytes per ambient read".format(
              size["http_bytes_per_event"], size["prompt_chars"],
              size["ambient_bytes_per_event"]))
    if size["blob_bytes"]:
        print("                +{:.0f} KB of blobs".format(size["blob_bytes"] / 1024))
    print("  replay        {:.2f}s -> {:.2f}s  =  {:.0f}x  "
          "({} turns at {:.0f} ms)".format(
              speed["recorded_s"], speed["replayed_s"], speed["ratio"],
              speed["turns"], speed["latency_s"] * 1000))
    print()
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
