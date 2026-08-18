"""Span paths, the unit of ordering for concurrent agents.

Async agents interleave calls, and wall-clock order across concurrent tasks is
not reproducible between runs -- so matching replay events by global order
would fail on any agent that fans out tool calls. Instead every event carries a
span path from a :class:`~contextvars.ContextVar`, and the matcher (M3) works
*within* a span. Two tool calls in different spans may then replay in either
order without breaking anything.

Because it is a ContextVar, ``asyncio`` tasks inherit the span active where
they were created, which is normally what you want: a subtask fanned out from
``root/plan`` records under ``root/plan`` unless it opens a span of its own.

Concurrent calls in the *same* span are matched in recorded order. That is a
documented limitation, not an accident (spec section 13).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Tuple

ROOT = "root"
SEPARATOR = "/"

_span_stack: ContextVar[Tuple[str, ...]] = ContextVar("reeltime_span", default=(ROOT,))


def _clean(name: str) -> str:
    name = str(name).strip().replace(SEPARATOR, "_")
    return name or "anon"


def current() -> str:
    """The active span path, e.g. ``root/plan/tools``."""
    return SEPARATOR.join(_span_stack.get())


def stack() -> Tuple[str, ...]:
    return _span_stack.get()


def push(name: str):
    """Enter a span; returns the token needed to :func:`pop` it."""
    return _span_stack.set(_span_stack.get() + (_clean(name),))


def pop(token) -> None:
    _span_stack.reset(token)


@contextmanager
def span(name: str) -> Iterator[str]:
    """Group the events recorded inside this block under ``name``.

    ::

        with tape.span("plan"):
            plan = llm(...)     # recorded under root/plan
    """
    token = push(name)
    try:
        yield current()
    finally:
        pop(token)


def reset() -> None:
    """Return to the root span. For tests and for run teardown."""
    _span_stack.set((ROOT,))
