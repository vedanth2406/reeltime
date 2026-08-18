"""Locating the ``.tape`` directory.

Resolution order, highest priority first:

1. an explicit ``tape_dir=`` argument to :func:`reeltime.install`
2. ``$TAPE_DIR``
3. the nearest ``.tape`` directory at or above the current directory
4. ``./.tape`` (created on demand)

Step 3 is what makes ``tape run`` behave sanely from a subdirectory of a
project: traces land in one place per repo rather than scattering.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

DIR_NAME = ".tape"
ENV_VAR = "TAPE_DIR"
CONFIG_NAME = ".tapeconfig"


def find_tape_dir(start: Optional[os.PathLike] = None) -> Path:
    """Return the tape directory to use. Does not create it."""
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env).expanduser().resolve()

    here = Path(start) if start is not None else Path.cwd()
    here = here.resolve()
    for candidate in [here, *here.parents]:
        existing = candidate / DIR_NAME
        if existing.is_dir():
            return existing
    return here / DIR_NAME


def ensure_tape_dir(path: os.PathLike) -> Path:
    """Create ``<tape_dir>/runs`` and ``<tape_dir>/blobs`` if needed."""
    root = Path(path)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    (root / "blobs").mkdir(parents=True, exist_ok=True)
    return root


def runs_dir(tape_dir: os.PathLike) -> Path:
    return Path(tape_dir) / "runs"


def blobs_dir(tape_dir: os.PathLike) -> Path:
    return Path(tape_dir) / "blobs"


def trace_path(tape_dir: os.PathLike, run_id: str) -> Path:
    return runs_dir(tape_dir) / "{}.jsonl".format(run_id)


def list_run_ids(tape_dir: os.PathLike) -> List[str]:
    """Run ids present on disk, oldest first (ULIDs sort chronologically)."""
    directory = runs_dir(tape_dir)
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.jsonl"))


def find_config(start: Optional[os.PathLike] = None) -> Optional[Path]:
    """Nearest ``.tapeconfig`` at or above ``start``."""
    here = Path(start) if start is not None else Path.cwd()
    here = here.resolve()
    for candidate in [here, *here.parents]:
        cfg = candidate / CONFIG_NAME
        if cfg.is_file():
            return cfg
    return None


def display_path(path: os.PathLike) -> str:
    """Path relative to the cwd when that is shorter, else absolute."""
    p = Path(path)
    try:
        rel = p.resolve().relative_to(Path.cwd().resolve())
    except (ValueError, OSError):
        return str(p)
    return str(rel)
