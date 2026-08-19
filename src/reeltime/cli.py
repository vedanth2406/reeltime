"""The ``tape`` command.

Three verbs so far: ``run`` records a script, ``ls`` lists what has been
recorded, and ``show`` inspects one run or one event. ``replay``, ``fork``,
``diff``, and ``doctor`` arrive in later milestones.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .core import context, doctor, fmt, ids, paths, tracediff
from .core import mcp as mcp_mod
from .core.blobs import BlobStore
from .core.fork import check_patches, missing_credentials
from .core.patch import parse_all as parse_patches
from .core.reindex import reindex
from .core.trace import Event, Trace, read_trace
from .errors import TapeError

#: Not yet built. Kept in step with the roadmap table in the README -- the two
#: are the only places that promise anything, so they must not drift apart.
#: Entries that are not subcommands say so in their name ("mcp adapter").
#: Not yet built. Kept in step with the roadmap table in the README -- the two
#: are the only places that promise anything, so they must not drift apart.
PLANNED = (
    ("langchain adapter", "record a LangChain agent's callbacks", "M9"),
)


# -- helpers -------------------------------------------------------------


def _tape_dir(args: argparse.Namespace) -> Path:
    from .core.paths import find_tape_dir

    return Path(args.tape_dir) if args.tape_dir else find_tape_dir()


#: Stands for the most recent run wherever a run id is accepted. No ULID can
#: collide with it, so it is unambiguous as a positional argument.
LATEST = ("last", "latest", "-")


def _resolve_run(tape_dir: Path, prefix: Optional[str]) -> Path:
    """Locate a run by id, by unambiguous prefix, or by being the latest one."""
    available = paths.list_run_ids(tape_dir)
    if not available:
        raise TapeError("no runs recorded in {}".format(paths.display_path(tape_dir)))
    if not prefix or prefix.lower() in LATEST:
        # list_run_ids sorts oldest first, and ULIDs sort chronologically.
        return paths.trace_path(tape_dir, available[-1])
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
    if event.kind == "mcp":
        return _summarise_mcp(event)
    return "{} = {}".format(req.get("name", event.kind), _truncate(json.dumps(res)[:80], 60))


def _summarise_mcp(event: Event) -> str:
    """One line for an MCP event, naming the server and what was asked of it."""
    req, res = event.req, event.res or {}
    server, op = req.get("server", "?"), req.get("op")
    error = event.meta.get("error")
    if op == mcp_mod.OP_INIT:
        outcome = error.get("type") if error else "{} {}".format(
            res.get("server_name") or "?", res.get("server_version") or "")
        return "{} initialize → {}".format(server, outcome.strip())
    if op == mcp_mod.OP_LIST:
        if error:
            return "{} tools/list raised {}".format(server, error.get("type"))
        names = res.get("tools") or []
        return "{} tools/list → {} tools: {}".format(
            server, res.get("count", len(names)), _truncate(", ".join(names), 44))
    if error:
        return "{}·{}(…) raised {}".format(server, req.get("name", "?"), error.get("type"))
    marker = " [tool error]" if res.get("is_error") else ""
    return "{}·{}({}) → {}{}".format(
        server, req.get("name", "?"),
        _truncate(json.dumps(req.get("args", {}), default=str)[1:-1], 30),
        _truncate(json.dumps(res.get("value"), default=str), 34), marker)


def _render_mcp(event: Event, resolved: Dict[str, Any]) -> str:
    """An MCP event as prose rather than as JSON-RPC.

    ``tape show N`` prints raw JSON for every other kind, and for HTTP that is
    the right answer -- the payload *is* the thing. An MCP event is not: server,
    tool, arguments and result are four named fields, and burying them in a
    nested wire envelope would give back exactly the opacity this milestone
    exists to remove. ``--raw`` still prints the JSON.
    """
    req = resolved.get("req") or {}
    res = resolved.get("res") or {}
    op = req.get("op")
    out = ["mcp · event {} · {} · {}".format(
        event.i, req.get("server", "?"), event.site)]
    if event.dur_ms:
        out[0] += "  ({:.0f}ms)".format(event.dur_ms)

    error = event.meta.get("error")
    if op == mcp_mod.OP_INIT:
        out.append("")
        out.append("  initialize")
        out.append("  server       {} {}".format(
            res.get("server_name") or "?", res.get("server_version") or ""))
        out.append("  protocol     {}".format(res.get("protocol") or "?"))
        out.append("  capabilities {}".format(
            ", ".join(res.get("capabilities") or []) or "none"))
    elif op == mcp_mod.OP_LIST:
        out.append("")
        out.append("  tools/list → {} tools".format(res.get("count", 0)))
        for tool_def in ((res.get("result") or {}).get("tools") or []):
            out.append("    {:<24} {}".format(
                tool_def.get("name", "?"),
                _truncate(tool_def.get("description") or "", 50)))
            schema = tool_def.get("inputSchema") or {}
            params = schema.get("properties") or {}
            if params:
                required = set(schema.get("required") or [])
                out.append("      ({})".format(", ".join(
                    "{}{}: {}".format(k, "" if k in required else "?",
                                      (v or {}).get("type", "any"))
                    for k, v in params.items())))
    else:
        out.append("")
        out.append("  tool   {}".format(req.get("name", "?")))
        out.append("  args   {}".format(
            json.dumps(req.get("args", {}), ensure_ascii=False, default=str)))
        if res.get("is_error"):
            out.append("  result the server reported a tool error")
        value = res.get("value")
        rendered = value if isinstance(value, str) else json.dumps(
            value, indent=2, ensure_ascii=False, default=str)
        out.append("  result {}".format(_indent_after_first(rendered, 9)))

    if error:
        out.append("  raised {}: {}".format(error.get("type"), error.get("message")))
    out.append("")
    out.append("(--raw prints the recorded JSON, wire envelope included)")
    return "\n".join(out) + "\n"


def _indent_after_first(text: str, width: int) -> str:
    lines = text.splitlines() or [""]
    pad = " " * width
    return "\n".join([lines[0]] + [pad + line for line in lines[1:]])


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


def _record_once(command: Sequence[str], tape_dir: Path, **env_extra: str):
    """Record one run of ``command``. Returns ``(run_id, CompletedProcess)``.

    Shared by ``tape run`` and ``tape doctor`` so that a doctored run is
    recorded through exactly the path a normal one is -- a doctor that recorded
    differently would be diagnosing its own harness.
    """
    run_id = ids.new_run_id()
    resolved = _resolve_interpreter(list(command))
    bootstrap = str(Path(__file__).resolve().parent / "_bootstrap")
    env = dict(os.environ, **env_extra)
    env["REELTIME_AUTOINSTALL"] = "1"
    env["REELTIME_RUN_ID"] = run_id
    env["TAPE_DIR"] = str(tape_dir)
    # Prepended, so the interpreter picks up our sitecustomize at startup --
    # before the agent imports httpx. The shim re-imports the user's own
    # sitecustomize if they have one.
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = bootstrap + (os.pathsep + existing if existing else "")
    return run_id, subprocess.run(resolved, env=env)


def cmd_run(args: argparse.Namespace) -> int:
    if not args.command:
        sys.stderr.write("tape run: give me a command, e.g. tape run python agent.py\n")
        return 2

    tape_dir = _tape_dir(args)
    paths.ensure_tape_dir(tape_dir)

    try:
        run_id, completed = _record_once(list(args.command), tape_dir)
    except FileNotFoundError as exc:
        sys.stderr.write("tape run: no such command: {}\n".format(exc.filename))
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
        # Deliberately not a ratio: this number includes interpreter startup,
        # so dividing by it would understate replay on a short run. The
        # boundary-to-boundary speedup is on the line above, from the player.
        sys.stderr.write(
            "  wall clock {:.2f}s including startup; the recorded run took "
            "{:.2f}s\n".format(elapsed, original)
        )
    return completed.returncode


# -- tape fork -----------------------------------------------------------


def _edit_event(trace: Trace, index: int, blobs: BlobStore) -> Optional[Dict[str, Any]]:
    """Open $EDITOR on one event. Returns None when the edit is abandoned.

    An empty buffer means "never mind", the way `git commit` reads one. Invalid
    JSON is a mistake rather than a decision, so it is reported and nothing is
    forked either way -- no run is created for an edit that did not land.
    """
    event = _event_at(trace, index)
    original = blobs.resolve(event.to_dict())

    editor = os.environ.get("REELTIME_EDITOR") or os.environ.get("EDITOR") or "vi"
    handle, path = tempfile.mkstemp(prefix="reeltime-fork-", suffix=".json")
    try:
        with os.fdopen(handle, "w") as out:
            json.dump(original, out, indent=2, ensure_ascii=False)
            out.write("\n")

        completed = subprocess.run(shlex.split(editor) + [path])
        if completed.returncode != 0:
            raise TapeError("{} exited {}; nothing was forked".format(
                editor, completed.returncode))

        with open(path) as source:
            text = source.read()
    finally:
        try:
            os.unlink(path)
        except OSError:  # pragma: no cover
            pass

    if not text.strip():
        sys.stderr.write("empty buffer; nothing was forked\n")
        return None
    try:
        edited = json.loads(text)
    except ValueError as exc:
        raise TapeError(
            "the edited event is not valid JSON ({}), so nothing was forked. "
            "Your edit was not saved anywhere -- run the command again.".format(exc)
        )
    if not isinstance(edited, dict):
        raise TapeError("the edited event must be a JSON object; nothing was forked")
    if edited == original:
        sys.stderr.write("no changes; forking anyway\n")
    return edited


def cmd_fork(args: argparse.Namespace) -> int:
    tape_dir = _tape_dir(args)
    trace = read_trace(_resolve_run(tape_dir, args.run))

    if args.at is None:
        raise TapeError(
            "fork needs a point: --at N replays events 0..N-1 and runs event N "
            "live. `tape show {}` lists them.".format(_short(trace.run_id))
        )
    if not trace.header.argv:
        raise TapeError(
            "run {} did not record a command, so there is nothing to re-run".format(
                _short(trace.run_id))
        )

    # Everything that can be known before spending a replay is checked here.
    patches = parse_patches(args.patch or [])
    check_patches(patches, trace, args.at)
    missing = missing_credentials(trace, args.at)
    if missing:
        raise TapeError(
            "this fork runs live from event {}, and those calls need:\n{}".format(
                args.at,
                "\n".join("  {}  (for {})".format(var, host) for host, var in missing))
        )

    override_path = None
    if args.edit:
        edited = _edit_event(trace, args.at, BlobStore(paths.blobs_dir(tape_dir)))
        if edited is None:
            return 1
        handle, override_path = tempfile.mkstemp(prefix="reeltime-override-",
                                                 suffix=".json")
        with os.fdopen(handle, "w") as out:
            json.dump(edited, out)

    child = ids.new_run_id()
    env = dict(os.environ)
    env.update({
        "REELTIME_AUTOINSTALL": "1",
        "REELTIME_MODE": "fork",
        "REELTIME_REPLAY": trace.run_id,
        "REELTIME_RUN_ID": child,
        "REELTIME_FORK_AT": str(args.at),
        "REELTIME_FORK_PATCH": json.dumps([p.source for p in patches]),
        "REELTIME_STRICTNESS": "loose" if args.loose else "default",
        "TAPE_DIR": str(tape_dir),
    })
    if override_path:
        env["REELTIME_FORK_OVERRIDE"] = override_path
    bootstrap = str(Path(__file__).resolve().parent / "_bootstrap")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = bootstrap + (os.pathsep + existing if existing else "")

    command = _resolve_interpreter([sys.executable] + list(trace.header.argv))
    cwd = trace.header.cwd if os.path.isdir(trace.header.cwd or "") else None

    try:
        completed = subprocess.run(command, env=env, cwd=cwd)
    except FileNotFoundError:
        sys.stderr.write("tape fork: cannot re-run {}\n".format(command[0]))
        return 127
    finally:
        if override_path:
            try:
                os.unlink(override_path)
            except OSError:  # pragma: no cover
                pass

    forked = paths.trace_path(tape_dir, child)
    if not forked.exists():
        sys.stderr.write("tape fork: nothing was recorded\n")
        return completed.returncode or 1

    result = read_trace(forked)
    footer = result.footer or {}
    replayed = sum(1 for e in result.events if e.meta.get("replayed_from"))
    sys.stderr.write(
        "\n{} forked → {}  ({} replayed, {} live, {})\n".format(
            "✓" if completed.returncode == 0 else "✗", _short(child), replayed,
            len(result.events) - replayed, fmt.usd(footer.get("cost_usd")))
    )
    sys.stderr.write("  parent {} · forked at event {}\n".format(
        _short(trace.run_id), args.at))
    for expression in (p.source for p in patches):
        sys.stderr.write("  patched {}\n".format(expression))
    return completed.returncode


# -- tape diff -----------------------------------------------------------


def cmd_diff(args: argparse.Namespace) -> int:
    tape_dir = _tape_dir(args)
    a = read_trace(_resolve_run(tape_dir, args.a))
    b = read_trace(_resolve_run(tape_dir, args.b))
    if a.run_id == b.run_id:
        raise TapeError("{} is the same run twice; give two different runs".format(
            _short(a.run_id)))

    result = tracediff.diff(a, b, only=args.only)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(tracediff.render(result))
    # A trajectory that diverged is the interesting answer, not an error.
    return 0


# -- tape doctor ---------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    if not args.command:
        sys.stderr.write(
            "tape doctor: give me a command, e.g. tape doctor python agent.py\n")
        return 2
    if args.runs < 2:
        raise TapeError(
            "--runs must be at least 2; there is nothing to compare below that")

    tape_dir = _tape_dir(args)
    paths.ensure_tape_dir(tape_dir)

    # Said before anything happens rather than after: this runs the agent for
    # real, N times, and the second one costs what the first one did.
    sys.stderr.write(
        "running `{}` {} times — real runs, real calls, real cost\n".format(
            " ".join(args.command), args.runs))

    run_ids = []
    for attempt in range(args.runs):
        sys.stderr.write("  run {} of {}…\n".format(attempt + 1, args.runs))
        try:
            run_id, completed = _record_once(list(args.command), tape_dir)
        except FileNotFoundError as exc:
            sys.stderr.write("tape doctor: no such command: {}\n".format(exc.filename))
            return 127
        if not paths.trace_path(tape_dir, run_id).exists():
            sys.stderr.write(
                "tape doctor: run {} recorded nothing. Is reeltime installed in "
                "the interpreter that ran the command?\n".format(attempt + 1))
            return completed.returncode or 1
        if completed.returncode != 0:
            # Still worth analysing. A command that fails the same way twice is
            # a different report from one that fails only sometimes, and the
            # second is exactly what this command is for.
            sys.stderr.write("  (exited {})\n".format(completed.returncode))
        run_ids.append(run_id)

    traces = [read_trace(paths.trace_path(tape_dir, r)) for r in run_ids]
    report = doctor.analyse(traces)

    sys.stderr.write("\n")
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(doctor.render(report))
        print()
        print("the runs are kept: tape diff {} {}".format(
            _short(run_ids[0]), _short(run_ids[1])))

    if args.fail_on_findings and not report.clean:
        return 1
    return 0


# -- tape reindex --------------------------------------------------------


def cmd_reindex(args: argparse.Namespace) -> int:
    tape_dir = _tape_dir(args)
    path = _resolve_run(tape_dir, args.run)
    result = reindex(path, BlobStore(paths.blobs_dir(tape_dir)), dry_run=args.dry_run)

    print("{}  {}".format(_short(result.run_id), result.line()))
    for note in result.notes():
        print("  {}".format(note))
    if args.dry_run and result.enriched:
        print("  (nothing written; drop --dry-run to apply)")
    return 0


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
        "forked_from": trace.header.forked_from,
        "fork_at": trace.header.fork_at,
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
        note = "" if row["complete"] else "  (incomplete)"
        if row["forked_from"]:
            # Parentage on the row itself: a directory of forks is unreadable
            # without it, and the tree is the point of forking.
            note += "  ← {}@{}".format(_short(row["forked_from"]), row["fork_at"])
        print("{:<15} {:<17} {:>7} {:>8} {:>8}  {}".format(
            _short(row["run_id"]),
            row["when"],
            row["events"],
            "{:.1f}s".format(row["dur_s"]) if row["dur_s"] is not None else "–",
            fmt.usd(row["cost_usd"]),
            _truncate(row["argv"], 34) + note,
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


def _event_at(trace: Trace, index: int) -> Event:
    for event in trace.events:
        if event.i == index:
            return event
    raise TapeError("run {} has no event {} (it has {})".format(
        _short(trace.run_id), index, len(trace)))


def _context_at(trace: Trace, index: int, blobs: BlobStore):
    event = _event_at(trace, index)
    assembled = context.from_event(event, blobs)
    if assembled is None:
        raise TapeError(
            "event {} is a {} event, not a recognised LLM call, so it has no "
            "message array to show. `tape show {} {}` prints it in full.".format(
                index, event.kind, _short(trace.run_id), index)
        )
    return assembled


def cmd_show(args: argparse.Namespace) -> int:
    tape_dir = _tape_dir(args)
    trace = read_trace(_resolve_run(tape_dir, args.run))

    if args.context:
        if args.index is None:
            raise TapeError(
                "--context needs an event: try `tape show {} <N> --context`, or "
                "`tape show {}` to see which events are LLM calls".format(
                    _short(trace.run_id), _short(trace.run_id))
            )
        blobs = BlobStore(paths.blobs_dir(tape_dir))
        assembled = _context_at(trace, args.index, blobs)
        if args.diff is None:
            sys.stdout.write(context.render(assembled, collapse=not args.full))
        else:
            earlier = _context_at(trace, args.diff, blobs)
            sys.stdout.write(context.render_diff(earlier, assembled))
        return 0

    if args.index is None:
        if args.json:
            print(json.dumps(
                {"header": trace.header.to_dict(),
                 "events": [e.to_dict() for e in trace.events],
                 "footer": trace.footer}, indent=2))
        else:
            _print_run(trace)
        return 0

    blobs = BlobStore(paths.blobs_dir(tape_dir))
    event = _event_at(trace, args.index)
    resolved = event.to_dict()
    if not args.raw:
        # Blob references are an encoding detail; showing an event means
        # showing what was actually sent and received.
        resolved = blobs.resolve(resolved)
        if event.kind == "mcp":
            sys.stdout.write(_render_mcp(event, resolved))
            return 0
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
    replay.add_argument("run", nargs="?", default="last",
                        help="run id, prefix, or 'last' (the default)")
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

    diff_cmd = sub.add_parser(
        "diff", help="align two runs and report what changed",
        description="Aligns two runs by call site and reports where they stop "
                    "being the same run.")
    diff_cmd.add_argument("a", help="the earlier run: id, prefix, or 'last'")
    diff_cmd.add_argument("b", help="the later run: id, prefix, or 'last'")
    diff_cmd.add_argument("--only", action="append", metavar="KIND",
                          help="compare only these kinds (llm, tool, http, mcp, "
                               "rand, time, uuid; repeatable)")
    diff_cmd.add_argument("--json", action="store_true",
                          help="machine-readable output")
    diff_cmd.set_defaults(func=cmd_diff)

    doctor_cmd = sub.add_parser(
        "doctor", help="run a command twice and report what is nondeterministic",
        description="Records the same command more than once and compares the "
                    "traces, so the report names actual sources of "
                    "nondeterminism rather than possible ones.")
    doctor_cmd.add_argument("command", nargs=argparse.REMAINDER,
                            help="the command to run, e.g. python agent.py")
    doctor_cmd.add_argument("--runs", type=int, default=2, metavar="N",
                            help="how many times to run it (default 2)")
    doctor_cmd.add_argument("--json", action="store_true",
                            help="machine-readable output")
    doctor_cmd.add_argument("--fail-on-findings", action="store_true",
                            help="exit 1 when a source is found (for CI)")
    doctor_cmd.set_defaults(func=cmd_doctor)

    reindex_cmd = sub.add_parser(
        "reindex", help="re-run the provider decoders over an existing run")
    reindex_cmd.add_argument("run", nargs="?", default="last",
                             help="run id, prefix, or 'last' (the default)")
    reindex_cmd.add_argument("--dry-run", action="store_true",
                             help="report what would change without writing")
    reindex_cmd.set_defaults(func=cmd_reindex)

    fork = sub.add_parser(
        "fork", help="replay to event N, then run live from there",
        description="Replays events 0..N-1 from a run, then continues live "
                    "from event N, recording the whole thing as a new run.")
    fork.add_argument("run", nargs="?", default="last",
                      help="run id, prefix, or 'last' (the default)")
    fork.add_argument("--at", type=int, metavar="N", required=False,
                      help="events 0..N-1 are replayed; event N is the first live one")
    fork.add_argument("--patch", action="append", metavar="EXPR",
                      help="change event N, e.g. 'llm.model=gpt-4o' "
                           "(repeatable; see the README for the grammar)")
    fork.add_argument("--edit", action="store_true",
                      help="open $EDITOR on event N before forking")
    fork.add_argument("--loose", action="store_true",
                      help="match the replayed prefix on content hash alone")
    fork.set_defaults(func=cmd_fork)

    ls = sub.add_parser("ls", help="list recorded runs, newest first")
    ls.add_argument("-n", "--limit", type=int, default=20)
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_ls)

    show = sub.add_parser("show", help="inspect a run, or one event in it")
    show.add_argument("run", help="run id, prefix, or 'last' for the most recent")
    show.add_argument("index", nargs="?", type=int, help="event index")
    show.add_argument("--json", action="store_true")
    show.add_argument("--raw", action="store_true",
                      help="keep blob: references instead of resolving them")
    show.add_argument("--context", action="store_true",
                      help="print the full message array sent to the model")
    show.add_argument("--diff", type=int, metavar="M",
                      help="with --context: show what changed since event M")
    show.add_argument("--full", action="store_true",
                      help="with --context: do not collapse long messages")
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
