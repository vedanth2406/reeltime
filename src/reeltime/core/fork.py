"""Forking: replay to step N, then run live from there.

Testing a prompt fix normally means re-running the whole agent -- slow,
expensive, and the bug may not even recur. A fork replays the first N events
from an existing trace, so they are free and identical, then lets the agent
continue against the real world. One variable changes; everything before it is
held fixed.

``--at N`` means **events 0 through N-1 are replayed, and event N is the first
live one.** The patch applies to event N, on its way out. That boundary is the
one thing everybody gets wrong by one, so it is stated the same way everywhere:
in ``--help``, in the summary line, and here.

The engine is a Player and a Recorder side by side. ``replaying`` is a property
rather than a constant, so it flips from true to false at the fork point and
every interception site -- the HTTP shim, ``@tape.tool``, the ambient patches --
picks up the change without knowing forks exist. Both halves are written to the
new run, which is what makes a fork forkable in turn.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from ..errors import TapeConfigError
from .fmt import usd
from .patch import Patch, apply_to_body
from .player import Player
from .recorder import Recorder
from .trace import Event, Trace

#: Hosts whose live calls need a credential, and the variable that carries it.
#: Used for the pre-flight check: a fork that will die at event N for want of a
#: key should say so before spending time replaying events 0..N-1.
CREDENTIALS = {
    "api.openai.com": "OPENAI_API_KEY",
    "api.anthropic.com": "ANTHROPIC_API_KEY",
    "generativelanguage.googleapis.com": "GEMINI_API_KEY",
}


@dataclass
class ForkSummary:
    """What a fork did."""

    run_id: str
    parent: str
    fork_at: int
    replayed: int = 0
    live: int = 0
    patches: List[str] = field(default_factory=list)
    cost_usd: float = 0.0
    path: Optional[Any] = None

    def line(self) -> str:
        return "forked → {}  ({} replayed, {} live, {})".format(
            self.run_id, self.replayed, self.live, usd(self.cost_usd))

    def notes(self) -> List[str]:
        out = ["parent {} · forked at event {}".format(self.parent, self.fork_at)]
        out.extend("patched {}".format(p) for p in self.patches)
        return out


def missing_credentials(trace: Trace, fork_at: int) -> List[Tuple[str, str]]:
    """``(host, variable)`` pairs the live half will need and does not have.

    Read from the events that will actually run live, so a fork whose remaining
    calls are all to localhost asks for nothing.
    """
    needed: Dict[str, str] = {}
    for event in trace.events:
        if event.i < fork_at or event.kind not in ("llm", "http"):
            continue
        url = event.req.get("url")
        if not isinstance(url, str):
            continue
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:  # pragma: no cover - malformed URL
            continue
        variable = CREDENTIALS.get(host)
        if variable and not os.environ.get(variable):
            needed[host] = variable
    return sorted(needed.items())


def check_patches(patches: Sequence[Patch], trace: Trace, fork_at: int) -> None:
    """Refuse a patch that cannot apply to the event it names.

    The patch targets event ``fork_at``. Discovering at the end of a live run
    that it never matched anything is the expensive way to find out.
    """
    if not patches:
        return
    target = next((e for e in trace.events if e.i == fork_at), None)
    if target is None:
        raise TapeConfigError(
            "--at {} is past the end of run {} ({} events), so there is no "
            "event for --patch to apply to".format(fork_at, trace.run_id, len(trace.events))
        )
    name = target.req.get("name") if isinstance(target.req.get("name"), str) else None
    for patch in patches:
        if patch.matches(target.kind, name):
            continue
        raise TapeConfigError(
            "patch {!r} does not apply to event {}, which is a {} event{}. "
            "`tape show {} {}` shows what is there.".format(
                patch.describe(), fork_at, target.kind,
                " for {!r}".format(name) if name else "",
                trace.run_id[:14], fork_at)
        )


def _recorded_result(event: Optional[Event], kind: str) -> Any:
    """What the parent event returned, for ``+=`` and ``~=`` to build on."""
    if event is None:
        return None
    res = event.res or {}
    if kind in ("tool", "mcp"):
        return res.get("value")
    return res.get("preview")


class ForkEngine:
    """A Player for events before the fork, a Recorder for the whole new run."""

    #: Lets the interception layer tell a fork from a plain recording when it
    #: needs to (only the patch hooks do).
    forking = True

    def __init__(
        self,
        player: Player,
        recorder: Recorder,
        fork_at: int,
        patches: Optional[Sequence[Patch]] = None,
        override: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.player = player
        self.recorder = recorder
        self.fork_at = fork_at
        self.patches: List[Patch] = list(patches or [])
        #: A whole event dict from ``--edit``. Its request body replaces the
        #: one the agent builds at the fork point; editing a *result* is what
        #: ``--patch ...result=`` is for.
        self.override = override
        self._override_spent = False
        self.replayed = 0
        self.applied: List[str] = []
        self._spent: List[Patch] = []
        self.boundary_event = next(
            (e for e in player.trace.events if e.i == fork_at), None)
        self.summary: Optional[ForkSummary] = None

    # -- the flag every interception site reads --------------------------

    @property
    def replaying(self) -> bool:
        """True until the fork point, false after it."""
        return self.replayed < self.fork_at

    # -- surface the shims expect ----------------------------------------

    @property
    def enabled(self) -> bool:
        return self.recorder.enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self.recorder.enabled = value
        self.player.enabled = value

    @property
    def t0(self) -> float:
        return self.recorder.t0

    @property
    def redactor(self):
        return self.recorder.redactor

    @property
    def blobs(self):
        return self.recorder.blobs

    @property
    def realtime(self) -> bool:
        return self.player.realtime

    @property
    def stats(self):
        return self.recorder.stats

    def resolved(self, event: Event, payload: Optional[Dict[str, Any]]) -> Any:
        return self.recorder.resolved(event, payload)

    # -- replayed half ---------------------------------------------------

    def consume(self, kind: str, req=None, **kwargs) -> Optional[Event]:
        """Serve a recorded event, and write it to the fork's own trace.

        Copying it across is what makes the fork a complete run rather than a
        suffix: it can be replayed, and forked again, on its own terms.
        """
        event = self.player.consume(kind, req, **kwargs)
        if event is None:
            return None
        self.replayed += 1
        self.recorder.copy_event(event, meta_extra={"replayed_from": self.player.trace.run_id})
        return event

    # -- live half -------------------------------------------------------

    def record(self, *args, **kwargs):
        return self.recorder.record(*args, **kwargs)

    def capture(self, *args, **kwargs):
        return self.recorder.capture(*args, **kwargs)

    # -- patches ---------------------------------------------------------

    def _pending(self, kind: str, name: Optional[str]) -> List[Patch]:
        return [p for p in self.patches
                if p not in self._spent and p.matches(kind, name)]

    def substitute(self, kind: str, name: Optional[str] = None) -> Tuple[bool, Any]:
        """``(True, value)`` when a patch replaces this boundary's result.

        A substituted boundary does not execute at all -- that is the point of
        ``tool.read_file.result="<empty file>"``: you want to see what the agent
        does with an empty file, not to empty the file.
        """
        for patch in self._pending(kind, name):
            if not patch.substitutes_result:
                continue
            current = _recorded_result(self.boundary_event, kind)
            value = patch.apply(current)
            self._spend(patch)
            return (True, value)
        return (False, None)

    def rewrite_body(self, kind: str, body: Any, name: Optional[str] = None) -> Any:
        """Apply request-field patches to an outgoing JSON body."""
        if not isinstance(body, dict):
            return body
        edited = self._edited_body()
        if edited is not None:
            body = edited
        for patch in self._pending(kind, name):
            if patch.substitutes_result:
                continue
            body = apply_to_body(patch, body)
            self._spend(patch)
        return body

    def _edited_body(self) -> Optional[Dict[str, Any]]:
        """The request body from ``--edit``, once."""
        if not self.override or self._override_spent:
            return None
        body = ((self.override.get("req") or {}).get("body") or {}).get("json")
        if not isinstance(body, dict):
            return None
        self._override_spent = True
        self.applied.append("--edit (request body)")
        return body

    def _spend(self, patch: Patch) -> None:
        self._spent.append(patch)
        self.applied.append(patch.describe())

    # -- teardown --------------------------------------------------------

    def close(self, exit_code: Optional[int] = None, **kwargs: Any) -> Dict[str, Any]:
        """Close the recording and return its footer.

        Returns the footer rather than the ForkSummary so that a fork closes
        down exactly the path a recording does; the summary is left on
        :attr:`summary` for anyone who wants the fork-shaped view.
        """
        self.player.enabled = False
        footer = self.recorder.close(exit_code, **kwargs)
        footer["forked_from"] = self.player.trace.run_id
        footer["fork_at"] = self.fork_at
        if self.applied:
            footer["patched"] = list(self.applied)
        self.summary = ForkSummary(
            run_id=self.recorder.writer.path.stem,
            parent=self.player.trace.run_id,
            fork_at=self.fork_at,
            replayed=self.replayed,
            live=footer.get("events", 0) - self.replayed,
            patches=list(self.applied),
            cost_usd=footer.get("cost_usd", 0.0),
            path=self.recorder.writer.path,
        )
        return footer
