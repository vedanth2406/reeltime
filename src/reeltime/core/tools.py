"""Recording local tools.

A tool that does not cross the network is still a boundary: it reads a file,
queries a database, charges a card. Decorate it and its arguments and result
are recorded; on replay (M3) the result is served from the tape and the body
never runs, which is what makes replaying an agent that deletes files safe.

::

    @tape.tool
    def read_file(path: str) -> str:
        return open(path).read()

    search = tape.wrap(third_party_search, name="search")
    tools = tape.wrap_all({"read_file": read_file, "search": search})

Outside a recording session a wrapped function behaves exactly like the
original, so decorated code is safe to ship.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, Dict, Mapping, Optional, Tuple, TypeVar

from ..errors import ReplayedError, TapeConfigError
from .tape import current

F = TypeVar("F", bound=Callable[..., Any])


def _bind(fn: Callable[..., Any], args: tuple, kwargs: dict) -> Tuple[Dict[str, Any], bool]:
    """Arguments as a name -> value mapping, and whether binding worked.

    Positional and keyword calls of the same function have to produce the same
    recorded shape, or the M3 matcher would treat ``read_file("a.txt")`` and
    ``read_file(path="a.txt")`` as different calls.

    The flag matters for ``--patch tool.<name>.args``: a patched mapping can
    only be turned back into a call when the names came from the signature. An
    unbindable callable (a builtin, say) records a positional fallback, and
    that shape is not a keyword call.
    """
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        values = dict(bound.arguments)
    except (TypeError, ValueError):
        return {"args": list(args), "kwargs": dict(kwargs)}, False
    values.pop("self", None)
    values.pop("cls", None)
    return values, True


def _patched_args(engine: Any, name: str, bound: Dict[str, Any], bindable: bool):
    """``(args, kwargs, patched)`` for a call a fork may have rewritten."""
    if not getattr(engine, "forking", False):
        return None
    patched = engine.rewrite_args("tool", name, bound)
    if patched == bound:
        return None
    if not bindable:
        raise TapeConfigError(
            "cannot apply --patch tool.{}.args: {!r}'s arguments could not be "
            "bound to its signature, so they were recorded positionally and "
            "there is no way to call it back with names".format(name, name)
        )
    return patched


def _engine():
    tape = current()
    if tape is None or tape.closed:
        return None
    engine = tape.engine
    return engine if engine.enabled else None


def _rebuild_error(error: Dict[str, Any]) -> BaseException:
    """Raise again what the tool raised when it was recorded.

    The agent reacted to that exception -- retried, fell back, gave up -- so a
    replay in which the call quietly succeeds is a replay of a different run.
    Builtin types are rebuilt exactly; anything else becomes a named
    :class:`ReplayedError` rather than a lie about the type.
    """
    import builtins

    name = error.get("type", "Exception")
    message = error.get("message", "")
    candidate = getattr(builtins, name, None)
    if isinstance(candidate, type) and issubclass(candidate, BaseException):
        return candidate(message)
    return type(name, (ReplayedError,), {})(message)


def _substitute(engine: Any, kind: str, name: str):
    """Ask a fork whether a ``--patch`` replaces this call's result."""
    if not getattr(engine, "forking", False):
        return (False, None)
    return engine.substitute(kind, name)


def _record_substitute(engine: Any, request: Dict[str, Any], value: Any) -> Any:
    """Record a patched result as a real event, without running anything."""
    engine.record("tool", request, {"value": value}, meta={"patched": True})
    return value


def _replayed_result(engine: Any, event: Any) -> Any:
    error = event.meta.get("error")
    if error:
        raise _rebuild_error(error)
    res = engine.resolved(event, event.res) or {}
    return res.get("value")


def wrap(fn: F, name: Optional[str] = None) -> F:
    """Record calls to ``fn``. Works on functions you do not own."""
    tool_name = name or getattr(fn, "__name__", "tool")

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            engine = _engine()
            if engine is None:
                return await fn(*args, **kwargs)
            bound, bindable = _bind(fn, args, kwargs)
            request = {"name": tool_name, "args": bound}
            if engine.replaying:
                event = engine.consume("tool", request)
                if event is not None:
                    # The body never runs: that is what makes replaying an
                    # agent that deletes files or charges cards safe.
                    return _replayed_result(engine, event)
                return await fn(*args, **kwargs)
            substituted, value = _substitute(engine, "tool", tool_name)
            if substituted:
                return _record_substitute(engine, request, value)
            patched = _patched_args(engine, tool_name, bound, bindable)
            if patched is not None:
                args, kwargs, request = (), patched, {"name": tool_name, "args": patched}
            with engine.capture("tool", request) as event:
                result = await fn(*args, **kwargs)
                event.res = {"value": result}
                return result

        async_wrapper.__reeltime_wrapped__ = True  # type: ignore[attr-defined]
        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        engine = _engine()
        if engine is None:
            return fn(*args, **kwargs)
        bound, bindable = _bind(fn, args, kwargs)
        request = {"name": tool_name, "args": bound}
        if engine.replaying:
            event = engine.consume("tool", request)
            if event is not None:
                return _replayed_result(engine, event)
            return fn(*args, **kwargs)
        substituted, value = _substitute(engine, "tool", tool_name)
        if substituted:
            return _record_substitute(engine, request, value)
        patched = _patched_args(engine, tool_name, bound, bindable)
        if patched is not None:
            # Recorded as the call that was actually made, not the one the
            # agent asked for -- the trace has to describe what happened.
            args, kwargs, request = (), patched, {"name": tool_name, "args": patched}
        with engine.capture("tool", request) as event:
            result = fn(*args, **kwargs)
            event.res = {"value": result}
            return result

    wrapper.__reeltime_wrapped__ = True  # type: ignore[attr-defined]
    return wrapper  # type: ignore[return-value]


def tool(fn: Optional[F] = None, *, name: Optional[str] = None) -> Any:
    """Decorator form of :func:`wrap`. Usable bare or with ``name=``."""
    if fn is not None:
        return wrap(fn)

    def decorate(inner: F) -> F:
        return wrap(inner, name=name)

    return decorate


def wrap_all(tools: Mapping[str, Callable[..., Any]]) -> Dict[str, Callable[..., Any]]:
    """Wrap a whole tool registry at once, keyed by the name you already use."""
    return {key: wrap(fn, name=key) for key, fn in tools.items()}


def is_wrapped(fn: Any) -> bool:
    return bool(getattr(fn, "__reeltime_wrapped__", False))
