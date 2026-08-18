"""reeltime -- deterministic record/replay for LLM agents.

An agent is deterministic *except* at four boundaries: LLM calls, tool and
network results, randomness, and clock reads. Record what crosses those
boundaries and everything between them replays exactly.

::

    import reeltime as tape

    tape.install()          # patches the ambient sources, starts a trace
    ...                     # your agent, unmodified
    tape.uninstall()        # or let the atexit hook do it

Or scope it::

    with tape.session() as run:
        agent.go()
    print(run.summary.line())

Records today: every HTTP call (httpx and requests, streaming included),
``@tape.tool`` functions, randomness, uuids, and clock reads. Replay arrives
in M3.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

__version__ = "0.1.0.dev0"

from .core.spans import span
from .core.tape import (
    Mode,
    RunSummary,
    Tape,
    current,
    install,
    is_recording,
    redact,
    session,
    uninstall,
)
from .core.tools import is_wrapped, tool, wrap, wrap_all
from .core.trace import KINDS, Event, Header, Trace, read_trace
from .errors import TapeConfigError, TapeError, TapeMiss, TapeStateError

__all__ = [
    "__version__",
    "install",
    "uninstall",
    "session",
    "current",
    "is_recording",
    "record_event",
    "span",
    "redact",
    "tool",
    "wrap",
    "wrap_all",
    "is_wrapped",
    "read_trace",
    "Tape",
    "Mode",
    "RunSummary",
    "Event",
    "Header",
    "Trace",
    "KINDS",
    "TapeError",
    "TapeMiss",
    "TapeConfigError",
    "TapeStateError",
]


def record_event(
    kind: str,
    req: Optional[Dict[str, Any]] = None,
    res: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Optional[Event]:
    """Record a boundary crossing reeltime does not know how to see itself.

    ::

        tape.record_event("tool", {"name": "roll_dice"}, {"value": 4})

    Returns the written :class:`Event`, or None when no tape is recording --
    so this is safe to leave in code that runs outside ``tape run``.
    """
    tape = current()
    if tape is None or tape.closed:
        return None
    return tape.record(kind, req, res, **kwargs)


if os.environ.get("REELTIME_AUTOINSTALL"):
    # Set by `tape run`, which also injects a sitecustomize so this happens at
    # interpreter startup -- before the agent imports httpx or anything else.
    try:
        install(run_id=os.environ.get("REELTIME_RUN_ID") or None)
    except Exception as _exc:  # pragma: no cover - never break a user's import
        import warnings

        warnings.warn("reeltime autoinstall failed: {}".format(_exc), RuntimeWarning)
