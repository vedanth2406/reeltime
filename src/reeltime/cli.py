"""The ``tape`` command.

Three verbs so far: ``run`` records a script, ``ls`` lists what has been
recorded, and ``show`` inspects one run or one event. ``replay``, ``fork``,
``diff``, and ``doctor`` arrive in later milestones.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .core import fmt, ids, paths
from .core.blobs import BlobStore
from .core.trace import Event, Trace, read_trace
from .errors import TapeError

PLANNED = (
    ("fork", "replay to step N, then run live", "M5"),
    ("diff", "align and compare two runs", "M6"),
    ("doctor", "find a run's nondeterminism sources", "M7"),
)


# -- helpers -------------------------------------------------------------


def _tape_dir(args: argparse.Namespace) -> Path:
    from .core.paths import find_tape_dir

    return Path(args.tape_dir) if args.tape_dir else find_tape_dir()


def _resolve_run(tape_dir: Path, prefix: str) -> Path:
    available = paths.list_run_ids(tape_dir)
    if not available:
        raise TapeError("no runs recorded in {}".format(paths.display_path(tape_dir)))
    return paths.trace_path(tape_dir, ids.resolve_prefix(prefix, available))


def _short(run_id: str) -> str:
    """Enough of a ULID to identify a run at a glance and to retype."""
    return run_id[:14]


def _when(run_id: str) -> str:
    import datetime as _dt

    try:
        stamp = ids.timestamp_ms(run_id) / 1000
    except TapeError:
        return "?"
    return _dt.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")


def _truncate(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _summarise(event: Event) -> str:
    """One line describing what crossed the boundary."""
    req, res = event.req, event.res or {}
    if event.kind == "llm":
        error = event.meta.get("error")
        if error:
            return "{} raised {}".format(req.get("model", "?"), error.get("type"))
        tokens = res.get("tokens") or {}
        counts = "{}→{}".format(tokens.get("in", "?"), tokens.get("out", "?"))
        return "{} {} {}".format(
            req.get("model", "?"), counts, _truncate(res.get("preview", ""), 60)
        )
    if event.kind == "http":
        error = event.meta.get("error")
        outcome = error.get("type") if error else res.get("status", "?")
        return "{} {} → {}".format(
            req.get("method", "?"), _truncate(req.get("url", "?"), 60), outcome
        )
    if event.kind == "tool":
        error = event.meta.get("error")
        if error:
            return "{}(…) raised {}".format(req.get("name", "?"), error.get("type"))
        return "{}({}) → {}".format(
            req.get("name", "?"),
            _truncate(json.dumps(req.get("args", {}))[1:-1], 40),
            _truncate(json.dumps(res.get("value")), 40),
        )
    return "{} = {}".format(req.get("name", event.kind), _truncate(json.dumps(res)[:80], 60))


# -- tape run ------------------------------------------------------------


def _resolve_interpreter(command: List[str]) -> List[str]:
    """Point a bare ``python`` at the interpreter ``tape`` itself is running in.

    ``tape run python agent.py`` otherwise picks up whatever ``python`` happens
    to be on PATH, which frequently is not the environment reeltime is
    installed in -- and the failure is a confusing "nothing was recorded"
    rather than an obvious one. An explicit path or any other command is left
    exactly as given.
    """
    if command and command[0] in ("python", "python3", os.path.basename(sys.executable)):
        return [sys.executable] + command[1:]
    return command


def cmd_run(args: argparse.Namespace) -> int:
    if not args.command:
        sys.stderr.write("tape run: give me a command, e.g. tape run python agent.py\n")
        return 2

    tape_dir = _tape_dir(args)
    paths.ensure_tape_dir(tape_dir)
    run_id = ids.new_run_id()

    command = _resolve_interpreter(list(args.command))
    bootstrap = str(Path(__file__).resolve().parent / "_bootstrap")
    env = dict(os.environ)
    env["REELTIME_AUTOINSTALL"] = "1"
    env["REELTIME_RUN_ID"] = run_id
    env["TAPE_DIR"] = str(tape_dir)
    # Prepended, so the interpreter picks up our sitecustomize at startup --
    # before the agent imports httpx. The shim re-imports the user's own
    # sitecustomize if they have one.
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = bootstrap + (os.pathsep + existing if existing else "")

    try:
        completed = subprocess.run(command, env=env)
    except FileNotFoundError:
        sys.stderr.write("tape run: no such command: {}\n".format(command[0]))
        return 127

    trace_file = paths.trace_path(tape_dir, run_id)
    if not trace_file.exists():
        sys.stderr.write(
            "tape run: nothing was recorded. Is reeltime installed in the "
            "interpreter that ran the command?\n"
        )
        return completed.returncode or 1

    trace = read_trace(trace_file)
    footer = trace.footer or {}
    sys.stderr.write(
        "\n{} recorded {} event{} → {}  ({:.1f}s, {})\n".format(
            "✓" if completed.returncode == 0 else "✗",
            len(trace),
            "" if len(trace) == 1 else "s",
            paths.display_path(trace_file),
            footer.get("dur_s", 0.0),
            fmt.usd(footer.get("cost_usd")),
        )
    )
    redacted = footer.get("redacted") or {}
    if redacted:
        total = sum(redacted.values())
        detail = ", ".join("{} {}".format(n, k) for k, n in sorted(redacted.items()))
        sys.stderr.write(
            "  redacted {} secret{} before writing ({})\n".format(
                total, "" if total == 1 else "s", detail
            )
        )
    if not trace.complete:
        sys.stderr.write("  run did not exit cleanly; the trace ends where it died\n")
    return completed.returncode


# -- tape replay ---------------------------------------------------------


def cmd_replay(args: argparse.Namespace) -> int:
    tape_dir = _tape_dir(args)
    trace_file = _resolve_run(tape_dir, args.run)
    trace = read_trace(trace_file)

    if not trace.header.argv:
        raise TapeError(
            "run {} did not record a command, so there is nothing to re-run".format(
                _short(trace.run_id))
        )

    strictness = "strict" if args.strict else ("loose" if args.loose else "default")
    command = _resolve_interpreter([sys.executable] + list(trace.header.argv))

    env = dict(os.environ)
    env.update({
        "REELTIME_AUTOINSTALL": "1",
        "REELTIME_MODE": "replay",
        "REELTIME_REPLAY": trace.run_id,
        "REELTIME_STRICTNESS": strictness,
        "REELTIME_ANNOUNCE": "1",
        "TAPE_DIR": str(tape_dir),
    })
    if args.to is not None:
        env["REELTIME_STOP_AT"] = str(args.to)
    if args.realtime:
        env["REELTIME_REALTIME"] = "1"
    if args.step:
        env["REELTIME_STEP"] = "1"
    bootstrap = str(Path(__file__).resolve().parent / "_bootstrap")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = bootstrap + (os.pathsep + existing if existing else "")

    cwd = trace.header.cwd if os.path.isdir(trace.header.cwd or "") else None
    if cwd is None and trace.header.cwd:
        sys.stderr.write(
            "tape replay: recorded working directory {} is gone; running here "
            "instead\n".format(trace.header.cwd)
        )

    started = time.perf_counter()
    try:
        completed = subprocess.run(command, env=env, cwd=cwd)
    except FileNotFoundError:
        sys.stderr.write("tape replay: cannot re-run {}\n".format(command[0]))
        return 127
    elapsed = time.perf_counter() - started

    original = (trace.footer or {}).get("dur_s")
    if original:
        sys.stderr.write(
            "  wall clock {:.2f}s vs {:.2f}s recorded ({:.0f}× faster, $0.00)\n".format(
                elapsed, original, original / elapsed if elapsed else 0.0)
        )
    return completed.returncode


# -- tape ls -------------------------------------------------------------


def _row(tape_dir: Path, run_id: str) -> Optional[Dict[str, Any]]:
    try:
        trace = read_trace(paths.trace_path(tape_dir, run_id))
    except (TapeError, OSError):
        return None
    footer = trace.footer or {}
    return {
        "run_id": run_id,
        "when": _when(run_id),
        "events": len(trace),
        "dur_s": footer.get("dur_s"),
        "cost_usd": footer.get("cost_usd"),
        "kinds": footer.get("kinds", {}),
        "complete": trace.complete,
        "argv": " ".join(trace.header.argv),
    }


def cmd_ls(args: argparse.Namespace) -> int:
    tape_dir = _tape_dir(args)
    run_ids = paths.list_run_ids(tape_dir)[::-1][: args.limit]
    rows = [row for row in (_row(tape_dir, r) for r in run_ids) if row]

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no runs in {}".format(paths.display_path(tape_dir)))
        return 0

    print("{:<15} {:<17} {:>7} {:>8} {:>8}  {}".format(
        "RUN", "WHEN", "EVENTS", "DUR", "COST", "COMMAND"))
    for row in rows:
        print("{:<15} {:<17} {:>7} {:>8} {:>8}  {}".format(
            _short(row["run_id"]),
            row["when"],
            row["events"],
            "{:.1f}s".format(row["dur_s"]) if row["dur_s"] is not None else "–",
            fmt.usd(row["cost_usd"]),
            _truncate(row["argv"], 40) + ("" if row["complete"] else "  (incomplete)"),
        ))
    return 0


# -- tape show -----------------------------------------------------------


def _print_run(trace: Trace) -> None:
    header = trace.header
    footer = trace.footer or {}
    print("run     {}".format(header.run_id))
    print("started {}   python {}   {}".format(header.started, header.python,
                                               header.platform))
    print("command {}".format(" ".join(header.argv)))
    if header.git:
        print("git     {} on {}{}".format(
            (header.git.get("sha") or "?")[:12],
            header.git.get("branch") or "?",
            "  (dirty)" if header.git.get("dirty") else "",
        ))
    if header.packages:
        print("packages {}".format(", ".join(
            "{}={}".format(k, v) for k, v in sorted(header.packages.items()))))
    print()
    for event in trace.events:
        print("{:>4}  {:<5} {:>8}  {:<24} {}".format(
            event.i, event.kind, "{:.0f}ms".format(event.dur_ms),
            _truncate(event.site, 24), _summarise(event)))
    if not trace.complete:
        print("\n(no footer: this run did not exit cleanly)")
    else:
        print("\n{} events · {:.1f}s · ${:.4f} · {} in / {} out tokens".format(
            footer.get("events", 0), footer.get("dur_s", 0.0),
            footer.get("cost_usd", 0.0),
            (footer.get("tokens") or {}).get("in", 0),
            (footer.get("tokens") or {}).get("out", 0)))


def cmd_show(args: argparse.Namespace) -> int:
    tape_dir = _tape_dir(args)
    trace = read_trace(_resolve_run(tape_dir, args.run))

    if args.index is None:
        if args.json:
            print(json.dumps(
                {"header": trace.header.to_dict(),
                 "events": [e.to_dict() for e in trace.events],
                 "footer": trace.footer}, indent=2))
        else:
            _print_run(trace)
        return 0

    matches = [e for e in trace.events if e.i == args.index]
    if not matches:
        raise TapeError("run {} has no event {} (it has {})".format(
            _short(trace.run_id), args.index, len(trace)))

    blobs = BlobStore(paths.blobs_dir(tape_dir))
    event = matches[0]
    resolved = event.to_dict()
    if not args.raw:
        # Blob references are an encoding detail; showing an event means
        # showing what was actually sent and received.
        resolved = blobs.resolve(resolved)
    print(json.dumps(resolved, indent=2, ensure_ascii=False))
    return 0


# -- entry point ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tape",
        description="deterministic record/replay for LLM agents",
        epilog="planned: " + ", ".join(
            "{} ({})".format(name, milestone) for name, _, milestone in PLANNED),
    )
    parser.add_argument("-V", "--version", action="version",
                        version="reeltime {}".format(__version__))
    parser.add_argument("--tape-dir", help="where traces live (default: nearest .tape)")
    sub = parser.add_subparsers(dest="command_name")

    run = sub.add_parser("run", help="record an unmodified command")
    run.add_argument("command", nargs=argparse.REMAINDER,
                     help="the command to run, e.g. python agent.py")
    run.set_defaults(func=cmd_run)

    replay = sub.add_parser("replay", help="re-run a recorded command offline")
    replay.add_argument("run", help="run id, or any unambiguous prefix")
    replay.add_argument("--to", type=int, metavar="N",
                        help="stop after event N")
    replay.add_argument("--step", action="store_true",
                        help="pause before each event (interactive)")
    replay.add_argument("--realtime", action="store_true",
                        help="re-emit stream chunks with their recorded delays")
    strictness = replay.add_mutually_exclusive_group()
    strictness.add_argument("--strict", action="store_true",
                            help="only exact matches (tier 1)")
    strictness.add_argument("--loose", action="store_true",
                            help="also match on content hash alone (tier 3)")
    replay.set_defaults(func=cmd_replay)

    ls = sub.add_parser("ls", help="list recorded runs, newest first")
    ls.add_argument("-n", "--limit", type=int, default=20)
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_ls)

    show = sub.add_parser("show", help="inspect a run, or one event in it")
    show.add_argument("run", help="run id, or any unambiguous prefix")
    show.add_argument("index", nargs="?", type=int, help="event index")
    show.add_argument("--json", action="store_true")
    show.add_argument("--raw", action="store_true",
                      help="keep blob: references instead of resolving them")
    show.set_defaults(func=cmd_show)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    # `tape run python -m foo --flag` must not have its flags eaten by us.
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except TapeError as exc:
        sys.stderr.write("tape: {}\n".format(exc))
        return 1
    except BrokenPipeError:  # pragma: no cover - piping into head
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
