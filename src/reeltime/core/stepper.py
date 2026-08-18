"""The interactive stepper behind ``tape replay --step``.

Deliberately tiny. Replay is free, so stepping backward is just replaying
forward again from zero -- there is no state to unwind here, and the stepper
does not need to be more than a prompt between events.
"""

from __future__ import annotations

import json
import sys
from typing import Any

HELP = "  [enter] next   c continue   s show this event in full   q quit"


def interactive(event: Any, player: Any) -> None:
    """Pause before handing ``event`` back to the agent."""
    if getattr(player, "_stepping_off", False):
        return

    summary = "#{:<4} {:<5} {:<26} {}".format(
        event.i, event.kind, event.site, _one_line(event)
    )
    while True:
        sys.stderr.write(summary + "\n")
        try:
            answer = input("tape> ").strip().lower()
        except EOFError:
            player._stepping_off = True
            return
        if answer in ("", "n", "next"):
            return
        if answer in ("c", "continue"):
            player._stepping_off = True
            return
        if answer in ("q", "quit"):
            from ..errors import StopReplay

            raise StopReplay(event.i)
        if answer in ("s", "show"):
            sys.stderr.write(json.dumps(
                player.resolved(event, event.to_dict()), indent=2)[:4000] + "\n")
            continue
        sys.stderr.write(HELP + "\n")


def _one_line(event: Any) -> str:
    req = event.req or {}
    if event.kind in ("llm", "http"):
        return "{} {}".format(req.get("method", ""), req.get("url", ""))[:70]
    if event.kind == "tool":
        return "{}({})".format(req.get("name", "?"),
                               json.dumps(req.get("args", {}))[1:-1][:50])
    return str(req.get("name", event.kind))
