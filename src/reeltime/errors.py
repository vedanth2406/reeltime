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
        preview: str = "",
        candidates: Optional[Sequence[Any]] = None,
        strictness: str = "default",
    ) -> None:
        self.kind = kind
        self.site = site
        self.span = span
        self.preview = preview
        self.candidates: List[Any] = list(candidates or [])
        self.strictness = strictness
        super().__init__(self._message())

    def _message(self) -> str:
        lines = [
            "no recorded {} event matches this call".format(self.kind),
            "  at        {}".format(self.site),
            "  span      {}".format(self.span),
        ]
        if self.preview:
            lines.append("  content   {}".format(self.preview))
        if self.candidates:
            lines.append("  nearest unconsumed events:")
            for cand in self.candidates[:5]:
                lines.append("    {}".format(cand))
        else:
            lines.append("  (no unconsumed events of this kind remain)")
        lines.append(
            "  matching is '{}'; try --loose, or re-record if the code changed".format(
                self.strictness
            )
        )
        return "\n".join(lines)
