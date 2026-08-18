"""How much faster is replay than the run it replays?

    python examples/m3_replay_speed.py

Simulates an agent whose LLM calls take a realistic amount of time, records it,
then replays it and reports the ratio. Replay does no network I/O at all, so
the saving is essentially the whole of the original run's latency.
"""

import json
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

import reeltime as tape

LATENCY_S = 0.4      # per "LLM" call
TURNS = 8


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        time.sleep(LATENCY_S)
        body = json.dumps({
            "object": "chat.completion",
            "model": "gpt-4o-mini",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 20},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@tape.tool
def read_file(path):
    return "contents of " + path


def agent(url):
    history = []
    for turn in range(TURNS):
        read_file("notes-{}.md".format(turn))
        reply = httpx.post(url, json={
            "model": "gpt-4o-mini",
            "messages": history + [{"role": "user", "content": "turn %d" % turn}],
        }).json()
        history.append({"role": "assistant", "content": reply["choices"][0]["message"]["content"]})
    return history


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:{}/v1/chat/completions".format(server.server_address[1])

    tape_dir = tempfile.mkdtemp(prefix="reeltime-bench-")
    try:
        # Baseline first: the same agent with nothing installed. Attributing
        # every millisecond that is not simulated latency to reeltime would
        # overstate the overhead several times over.
        started = time.perf_counter()
        agent(url)
        baseline_wall = time.perf_counter() - started

        started = time.perf_counter()
        with tape.session(tape_dir=tape_dir, collect_git=False, run_id="01BENCH") as run:
            recorded = agent(url)
        record_wall = time.perf_counter() - started
        summary = run.summary

        server.shutdown()          # the network is now gone

        started = time.perf_counter()
        with tape.session("replay", tape_dir=tape_dir, replay="01BENCH") as replay_run:
            replayed = agent(url)
        replay_wall = time.perf_counter() - started

        assert replayed == recorded, "replay diverged"

        print("agent: {} turns, {} events, {:.0f}ms simulated latency per call"
              .format(TURNS, summary.events, LATENCY_S * 1000))
        print()
        print("  record   {:7.3f}s   {}".format(record_wall, summary.line()))
        print("  replay   {:7.3f}s   {}".format(replay_wall, replay_run.summary.line()))
        print()
        print("  {:.0f}× faster, $0.00, zero network calls".format(
            record_wall / replay_wall))
        print()
        print("  baseline (not recorded)  {:7.3f}s".format(baseline_wall))
        print("  recording overhead       {:+.1f}ms total, {:.2f}ms per event".format(
            (record_wall - baseline_wall) * 1000,
            (record_wall - baseline_wall) / summary.events * 1000))
    finally:
        shutil.rmtree(tape_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
