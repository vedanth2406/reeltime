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
import sysconfig
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


def _library_dirs() -> frozenset:
    """Directories holding installed third-party code."""
    found = set()
    for key in ("purelib", "platlib"):
        try:
            found.add(str(Path(sysconfig.get_paths()[key]).resolve()))
        except (KeyError, OSError):  # pragma: no cover - exotic layouts
            continue
    return frozenset(found)


_LIBRARY_DIRS = _library_dirs()

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

    @property
    def is_library(self) -> bool:
        """True for the standard library and for installed packages alike."""
        return _is_library(self.filename)


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


def _is_library(filename: str) -> bool:
    """True for installed third-party or standard-library code."""
    if _is_stdlib(filename):
        return True
    if "site-packages" in filename or "dist-packages" in filename:
        return True
    return any(filename.startswith(directory) for directory in _LIBRARY_DIRS)


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


def caller(depth: int = 1, skip_libraries: bool = False) -> CallSite:
    """The nearest frame outside reeltime, starting ``depth`` frames up.

    With ``skip_libraries``, keep walking out through installed packages until
    the caller's own code is reached. An HTTP event intercepted at the
    transport layer is many frames below the agent -- through the OpenAI SDK,
    httpx, and httpcore -- and blaming ``httpx/_client.py:1234`` for it would
    make call-site matching worthless. The first frame that is neither
    reeltime, nor the standard library, nor site-packages is the one the user
    can actually go and look at.
    """
    try:
        frame = sys._getframe(depth)
    except ValueError:  # pragma: no cover - stack shallower than depth
        return CallSite("<unknown>", 0, "<unknown>", "")
    while frame is not None and _is_internal(frame.f_code.co_filename):
        frame = frame.f_back

    if skip_libraries:
        candidate = frame
        while candidate is not None and _is_library(candidate.f_code.co_filename):
            candidate = candidate.f_back
        # An agent that itself lives in site-packages has no such frame; the
        # innermost library frame is a worse answer than none, but it is still
        # a real location, so fall back to it rather than reporting nothing.
        frame = candidate if candidate is not None else frame

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
