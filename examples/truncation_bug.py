"""A context bug you can reproduce in ten seconds, with no API key.

    tape run python examples/truncation_bug.py
    tape replay <run> --to 1
    tape show <run> 1 --context --diff 0

The agent answers the first question correctly and the second one wrongly. The
reason is not in the code you would look at first: between the two calls the
"framework" (the `trim_history` function below, standing in for a real one)
truncates the directory listing, so the second call is answered from a listing
that no longer contains the file being asked about.

Nothing in the output shows this. `tape show --context --diff` shows it in one
line: TRUNCATED, with what was cut.

A mock provider is embedded so the run costs nothing and is byte-identical for
everyone. It binds a fixed port, because the request URL is part of what replay
matches on -- an ephemeral port would differ between record and replay and every
event would be reported as drifted.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openai import OpenAI

FILES = ["report_{:02d}.pdf".format(i) for i in range(30)] + ["invoice.pdf"]
LISTING = "\n".join("  {}  ({} KB)".format(name, 40 + i)
                    for i, name in enumerate(FILES))

PORT = int(os.environ.get("REELTIME_DEMO_PORT", "8422"))

#: Stands in for real API latency, so the replay actually has something to save.
#: A local mock answers instantly, which would make replay look no faster.
LATENCY_S = float(os.environ.get("REELTIME_DEMO_LATENCY", "0.4"))


class Provider(BaseHTTPRequestHandler):
    """Answers strictly from the context it is given.

    That is the whole point of the demo: the wrong answer is caused by the
    input, not by the model. Ask about a file that is still in the listing and
    it says yes; ask about one that was trimmed away and it says no.
    """

    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        time.sleep(LATENCY_S)
        context = "\n".join(m["content"] for m in request["messages"])
        question = request["messages"][-1]["content"]

        asked_about = next((name for name in FILES if name in question), None)
        listing = context.rsplit(question, 1)[0]
        if asked_about and asked_about in listing:
            answer = "Yes \u2014 {} is in the listing.".format(asked_about)
        else:
            answer = "No, {} is not in the listing.".format(asked_about or "that file")

        body = json.dumps({
            "object": "chat.completion",
            "model": "gpt-4o-mini",
            "choices": [{"message": {"content": answer}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(context) // 4, "completion_tokens": 12},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_provider():
    """Bind a fixed port so record and replay see the same URL."""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Provider)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        print("port {} was busy; using {} instead (replay will report the URL "
              "as drifted)".format(PORT, server.server_address[1]))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:{}/v1".format(server.server_address[1])


def trim_history(messages, budget=280):
    """Stands in for a framework's context manager. This is the bug.

    It trims the *oldest user message* to fit a budget, which quietly removes
    the end of the directory listing -- including the file the next question is
    about.
    """
    trimmed = []
    for message in messages:
        if message["role"] == "user" and len(message["content"]) > budget:
            message = dict(message, content=message["content"][:budget])
        trimmed.append(message)
    return trimmed


def main():
    server, base_url = start_provider()
    client = OpenAI(api_key="sk-not-a-real-key-000000000000", base_url=base_url,
                    max_retries=0)

    def ask(messages):
        reply = client.chat.completions.create(
            model="gpt-4o-mini", messages=messages, temperature=0)
        return reply.choices[0].message.content

    history = [
        {"role": "system", "content": "Answer only from the listing you are given."},
        {"role": "user", "content": "Directory listing:\n" + LISTING},
    ]

    first = ask(history + [{"role": "user", "content":
                            "Is report_00.pdf in the listing?"}])
    print("Q1: is report_00.pdf there?  ->", first)

    # ... and now the framework trims the history before the next turn.
    second = ask(trim_history(history) + [{"role": "user", "content":
                                          "Is invoice.pdf in the listing?"}])
    print("Q2: is invoice.pdf there?    ->", second)

    print()
    print("Q2 is wrong: invoice.pdf IS in the listing.")
    print("Run:  tape show <run> 1 --context --diff 0")
    server.shutdown()


if __name__ == "__main__":
    main()
