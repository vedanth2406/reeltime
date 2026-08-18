"""Working out where in the user's code a recorded call came from.

Call-site identity is the backbone of replay matching (spec section 6): an
event is matched by *where it was called from* and its sequence number at that
site, not by its global index. That is what lets a user edit unrelated code
between record and replay without every subsequent event misaligning.

Two forms are stored per event:

``site``
    ``agent.py:88`` -- file and line. The primary key.
``qual``
    ``agent.py::Planner.step`` -- file and enclosing qualified name. Line
    numbers shift the moment anyone inserts an import at the top of a file, so
    the matcher falls back to this.

``sys._getframe`` rather than ``inspect.stack()``: the latter builds a
FrameInfo and reads source context for every frame on the stack, which costs
milliseconds per call and would dominate recording overhead.
"""

from __future__ import annotations

import contextlib
import functools
import os
import sys
from pathlib import Path
from typing import Dict, NamedTuple, Optional

_PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent)

# Frames from these helpers sit between user code and us whenever a recorded
# call is made through a context manager or a decorator, and naming them as the
# call site would be useless.
_TRANSPARENT_FILES = frozenset(
    {
        getattr(contextlib, "__file__", "") or "",
        getattr(functools, "__file__", "") or "",
    }
)

_STDLIB_DIR = str(Path(os.__file__).resolve().parent)

UNKNOWN = "<unknown>:0"

_display_cache: Dict[str, str] = {}


class CallSite(NamedTuple):
    file: str
    lineno: int
    qualname: str
    filename: str  # absolute, uncached -- used for the stdlib check

    @property
    def site(self) -> str:
        return "{}:{}".format(self.file, self.lineno)

    @property
    def qual(self) -> str:
        return "{}::{}".format(self.file, self.qualname)

    @property
    def is_stdlib(self) -> bool:
        return _is_stdlib(self.filename)


def _display(filename: str) -> str:
    """Path relative to the cwd where possible -- traces should read like the repo."""
    cached = _display_cache.get(filename)
    if cached is not None:
        return cached
    try:
        resolved = Path(filename).resolve()
        try:
            shown = str(resolved.relative_to(Path.cwd().resolve()))
        except ValueError:
            shown = str(resolved)
    except (OSError, ValueError):  # pragma: no cover - exotic filenames
        shown = filename
    _display_cache[filename] = shown
    return shown


def _is_stdlib(filename: str) -> bool:
    return filename.startswith(_STDLIB_DIR) and "site-packages" not in filename


def _is_internal(filename: str) -> bool:
    return filename.startswith(_PACKAGE_ROOT) or filename in _TRANSPARENT_FILES


def _qualname(frame) -> str:
    code = frame.f_code
    # co_qualname lands in 3.11; before that, reconstruct the common case of a
    # method by reading `self`/`cls` out of the frame's locals.
    qual = getattr(code, "co_qualname", None)
    if qual:
        return qual
    name = code.co_name
    argnames = code.co_varnames[: code.co_argcount]
    if argnames and argnames[0] in ("self", "cls"):
        obj = frame.f_locals.get(argnames[0])
        cls = obj if isinstance(obj, type) else type(obj)
        owner = getattr(cls, "__name__", None)
        if owner:
            return "{}.{}".format(owner, name)
    return name


def caller(depth: int = 1) -> CallSite:
    """The nearest frame outside reeltime, starting ``depth`` frames up."""
    try:
        frame = sys._getframe(depth)
    except ValueError:  # pragma: no cover - stack shallower than depth
        return CallSite("<unknown>", 0, "<unknown>", "")
    while frame is not None and _is_internal(frame.f_code.co_filename):
        frame = frame.f_back
    if frame is None:
        return CallSite("<unknown>", 0, "<unknown>", "")
    filename = frame.f_code.co_filename
    return CallSite(
        file=_display(filename),
        lineno=frame.f_lineno,
        qualname=_qualname(frame),
        filename=filename,
    )


def clear_cache() -> None:
    """Drop the display-path cache (the cwd changed, or a test moved dirs)."""
    _display_cache.clear()
