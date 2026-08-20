"""Putting a recorded HTTP exchange back on the wire.

Replay never touches the network. The transport shim, in replay mode, asks the
player for the matching event and rebuilds a response object indistinguishable
to the SDK above it -- same status, same headers, same body bytes, and for a
stream the same chunks in the same order.

Errors are rebuilt too. A recorded ``ConnectError`` has to be raised again: a
replay in which a call that failed now succeeds is not a replay of that run.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .. import _originals
from ..trace import Event
from . import common

#: Recomputed by the client from the body we hand back; keeping the recorded
#: values risks contradicting the bytes we actually deliver.
DROP_HEADERS = frozenset({"content-length", "transfer-encoding", "content-encoding"})


def response_headers(res: Dict[str, Any]) -> List[Tuple[str, str]]:
    return [
        (str(key), str(value))
        for key, value in (res.get("headers") or [])
        if str(key).lower() not in DROP_HEADERS
    ]


def recorded_error(event: Event, module: Any) -> Optional[BaseException]:
    """The exception this event recorded, rebuilt from the client's own class.

    **The class is the part that matters**, not the message. An agent that
    catches ``urllib3.exceptions.HTTPError`` and retries has a code path the
    recording went down, and a replay that raises something else takes a
    different path -- a divergence with no network call in sight, which is the
    quietest way for a replay to lie.

    Which is exactly what happened: httpx's exceptions all take a single
    message, but urllib3's take the connection or the pool as well
    (``NewConnectionError(conn, message)``,
    ``MaxRetryError(pool, url, reason)``). A one-argument construction raises
    ``TypeError`` on those, and the fallback below turned a recorded
    ``NewConnectionError`` into a ``RuntimeError`` that no ``except`` clause in
    the agent was written for.

    So a constructor that refuses its arguments is not the end of it: the class
    is instantiated without running ``__init__`` and given the message
    directly. The result is a genuine instance of the recorded class -- it
    catches correctly and prints correctly -- with whatever attributes
    ``__init__`` would have set left unset, which is the honest trade. The
    objects it would have stored (a live connection, a pool) belong to a
    request that is not being made.
    """
    error = event.meta.get("error")
    if not error:
        return None
    name = error.get("type", "TransportError")
    message = "{} (replayed)".format(error.get("message", ""))
    exception_class = getattr(module, name, None)
    if not (isinstance(exception_class, type) and issubclass(exception_class, BaseException)):
        exception_class = getattr(module, "TransportError", RuntimeError)
    try:
        return exception_class(message)
    except Exception:
        pass
    try:
        rebuilt = exception_class.__new__(exception_class)
        BaseException.__init__(rebuilt, message)
        return rebuilt
    except Exception:  # pragma: no cover - exotic metaclass
        return RuntimeError("{}: {}".format(name, message))


def is_stream(res: Optional[Dict[str, Any]]) -> bool:
    return bool(res) and "stream" in res


def stream_parts(res: Dict[str, Any], realtime: bool) -> Tuple[List[bytes], List[float]]:
    """Recorded chunks, and the pauses to take before each one."""
    chunks = common.decode_chunks(res["stream"])
    delays = common.chunk_delays(res["stream"]) if realtime else []
    if len(delays) != len(chunks):
        delays = [0.0] * len(chunks)
    return chunks, delays


class ReplayedByteStream:
    """Re-emits recorded chunks, boundary for boundary.

    Instant by default -- that is what makes scrubbing a timeline feel
    immediate. ``--realtime`` reinstates the recorded gaps, which is the only
    way to reproduce a bug in code that cancels a stream early or barges in.
    """

    def __init__(self, chunks: List[bytes], delays: List[float]) -> None:
        self._chunks = chunks
        self._delays = delays

    def __iter__(self):
        for delay, chunk in zip(self._delays, self._chunks):
            if delay:
                _originals.sleep(delay)
            yield chunk

    def close(self) -> None:
        pass

    async def __aiter__(self):
        import asyncio

        for delay, chunk in zip(self._delays, self._chunks):
            if delay:
                await asyncio.sleep(delay)
            yield chunk

    async def aclose(self) -> None:
        pass
