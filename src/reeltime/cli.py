"""The ``tape`` command.

A stub until milestone 2. The entry point exists so that installing the
package gives you the command, and so ``tape --version`` can confirm which
build you are running.
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence

from . import __version__

ROADMAP = (
    ("run",     "record an unmodified script",          "M2"),
    ("ls",      "list recorded runs",                   "M2"),
    ("show",    "inspect an event, or its full context", "M2"),
    ("replay",  "replay a run offline, for free",       "M3"),
    ("fork",    "replay to step N, then run live",      "M5"),
    ("diff",    "align and compare two runs",           "M6"),
    ("doctor",  "find a run's nondeterminism sources",  "M7"),
    ("ui",      "browse traces at localhost:7654",      "M8"),
)

USAGE = """usage: tape <command> [options]

reeltime {version} -- deterministic record/replay for LLM agents

no commands are available yet; this build ships the recording core only.

  import reeltime as tape
  tape.install()

planned commands:
{commands}
""".format(
    version=__version__,
    commands="\n".join(
        "  {:<8} {:<40} ({})".format(name, description, milestone)
        for name, description, milestone in ROADMAP
    ),
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-V", "--version", "version"):
        print("reeltime {}".format(__version__))
        return 0
    sys.stderr.write(USAGE)
    return 0 if (not args or args[0] in ("-h", "--help", "help")) else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
