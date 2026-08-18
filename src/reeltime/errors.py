"""Exception hierarchy for reeltime.

Kept in one small module so that ``from reeltime.errors import *`` is a
complete picture of everything the library can raise at a user.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence


class TapeError(Exception):
    """Base class for every error reeltime raises."""


class TapeConfigError(TapeError):
    """Bad configuration: unknown mode, unreadable tape dir, bad .tapeconfig."""


class TapeStateError(TapeError):
    """An operation was attempted in the wrong mode or at the wrong time."""


class ReplayedError(TapeError):
    """Stands in for a recorded exception whose class cannot be imported.

    Used only when the original type is not a builtin and not reachable from
    the module it came from. The name and message are preserved so the agent's
    own error handling still has something to look at.
    """


class StopReplay(BaseException):
    """Raised inside the agent to end a ``--to N`` replay.

    Deliberately not an :class:`Exception`: agent code is full of broad
    ``except Exception`` handlers, and a stepper that can be swallowed by the
    program it is stepping through is useless.
    """

    def __init__(self, stopped_at: int) -> None:
        self.stopped_at = stopped_at
        super().__init__("replay stopped after event {}".format(stopped_at))


class TapeMiss(TapeError):
    """Replay could not match a call against the recorded trace.

    Never swallowed and never downgraded to a live request: a debugger that
    silently falls through to the network is a debugger that lies to you.
    """

    def __init__(
        self,
        kind: str,
        site: str,
        *,
        span: str = "root",
        qual: Optional[str] = None,
        preview: str = "",
        candidates: Optional[Sequence[Any]] = None,
        strictness: str = "default",
        remaining: int = 0,
        run_id: str = "",
    ) -> None:
        self.kind = kind
        self.site = site
        self.span = span
        self.qual = qual
        self.preview = preview
        #: Objects with a ``line()`` method, or plain strings.
        self.candidates: List[Any] = list(candidates or [])
        self.strictness = strictness
        self.remaining = remaining
        self.run_id = run_id
        super().__init__(self._message())

    def _message(self) -> str:
        where = self.site
        if self.qual:
            function = self.qual.split("::")[-1]
            where = "{}  (in {})".format(self.site, function)

        lines = [
            "no recorded {} event matches this call".format(self.kind),
            "",
            "  at        {}".format(where),
            "  span      {}".format(self.span),
        ]
        if self.preview:
            lines.append("  sent      {}".format(self.preview))
        lines.append("")

        if self.candidates:
            lines.append("  nearest unconsumed events, and why each was rejected:")
            for candidate in self.candidates:
                text = candidate.line() if hasattr(candidate, "line") else str(candidate)
                lines.append("    {}".format(text))
        elif self.remaining:
            lines.append(
                "  {} recorded event(s) remain, none of them a {}.".format(
                    self.remaining, self.kind
                )
            )
        else:
            lines.append("  the tape is fully consumed: the agent made more calls "
                         "than were recorded.")
        lines.append("")
        lines.append("  matching is '{}'. {}".format(self.strictness, self._advice()))
        return "\n".join(lines)

    def _advice(self) -> str:
        if self.strictness == "strict":
            return "Drop --strict to allow drifted content, or re-record."
        if self.strictness == "default":
            return "Try --loose to match on content alone, or re-record."
        return "Even --loose found nothing; the code has changed materially. Re-record."
