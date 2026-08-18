"""Milestone 1 demo: ambient nondeterminism is recorded.

    python examples/m1_ambient.py

Writes a trace to .tape/runs/<run_id>.jsonl and prints it back.
"""

import datetime
import random
import time
import uuid

import reeltime as tape


def pick_strategy():
    """A "decision" that is nondeterministic for no good reason."""
    return random.choice(["cautious", "bold", "chaotic"])


def main():
    with tape.session(tape_dir=".tape") as run:
        request_id = uuid.uuid4()
        started = datetime.datetime.now()

        with tape.span("plan"):
            strategy = pick_strategy()
            temperature = round(random.uniform(0.0, 1.0), 3)

        with tape.span("act"):
            tape.record_event(
                "tool",
                {"name": "read_file", "args": {"path": "notes.md"}},
                {"value": "remember to buy milk"},
            )
            elapsed = time.perf_counter()

        print("request {} started {}".format(request_id, started.isoformat()))
        print("strategy={} temperature={}".format(strategy, temperature))

    summary = run.summary
    print("\n✓ " + summary.line())
    warning = summary.redaction_line()
    if warning:
        print("  " + warning)

    print("\n--- {} ---".format(summary.path))
    trace = tape.read_trace(summary.path)
    for event in trace.events:
        print(
            "{:>3}  {:<5} {:<22} {:<10} {}".format(
                event.i,
                event.kind,
                event.site,
                event.span,
                (event.res or {}).get("value"),
            )
        )


if __name__ == "__main__":
    main()
