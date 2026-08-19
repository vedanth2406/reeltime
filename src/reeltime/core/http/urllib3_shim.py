"""The urllib3 shim -- what closes the Bedrock gap.

``botocore`` is built on ``urllib3``, not on httpx or requests, so until this
existed an agent talking to Bedrock recorded **nothing at all**: no event, no
error, and a replay that quietly went to the network. That is the largest
version of the failure design principle 4 forbids, and it is why this milestone
took priority over a viewer.

The seam is ``HTTPConnectionPool.urlopen`` -- public, documented, and where
``botocore.httpsession.URLLib3Session`` calls it. That is one layer above the
socket and one layer below every AWS SDK, so it catches boto3, aiobotocore's
sync paths, and anything else built on urllib3 without knowing that any of them
exist.

**Why this is not the aiohttp situation.** aiohttp was assessed and refused in
M9 because replay meant fabricating a response over private internals. urllib3
hands back a ``HTTPResponse`` with a *public* constructor that takes a
file-like body, so both directions of this shim are ordinary code:

* recording wraps the real response in a file-like that keeps every chunk the
  caller reads, which preserves streaming for the caller instead of buffering
  it away;
* replay builds a new ``HTTPResponse`` over a file-like that re-emits the
  recorded chunks, boundary for boundary.

**One event, not two.** ``requests`` is also built on urllib3, so a
``requests`` call passes through ``HTTPAdapter.send`` -- already recorded by
:mod:`reeltime.core.http.requests_shim` -- and then through this seam
underneath it. Nothing special is done about that: the M1 boundary rule already
covers it, because the outer shim wraps its inner call in ``boundary()`` and
this one declines to record anything while ``in_boundary()`` is true. Same
reason a redirect retried inside ``urlopen`` produces one event rather than
one per attempt. There is a regression test pinning it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import _originals, callsite
from ..recorder import boundary, in_boundary
from . import common, replay as replay_support

#: Ports that are implied by the scheme and so must not enter the recorded URL.
#: The URL is part of the match key, and ``https://host:443/x`` and
#: ``https://host/x`` are the same request spelled two ways.
DEFAULT_PORTS = {"http": 80, "https": 443}


def absolute_url(pool: Any, url: str) -> str:
    """The full URL for a request the pool only knows the target of.

    ``urlopen`` is given a request *target* -- a path and query -- because the
    pool already knows the host it is connected to. botocore passes an absolute
    URL instead when it is going through a proxy, so both are accepted.
    """
    if url.startswith("http://") or url.startswith("https://"):
        return url
    scheme = getattr(pool, "scheme", None) or "http"
    host = getattr(pool, "host", None) or ""
    port = getattr(pool, "port", None)
    netloc = host
    if port and port != DEFAULT_PORTS.get(scheme):
        netloc = "{}:{}".format(host, port)
    if not url.startswith("/"):
        url = "/" + url
    return "{}://{}{}".format(scheme, netloc, url)


def _body_bytes(body: Any) -> Optional[bytes]:
    """``body`` as bytes, or None when reading it would consume the request."""
    if body is None:
        return b""
    if isinstance(body, bytes):
        return body
    if isinstance(body, bytearray):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    return None


def _request_payload(engine: Any, pool: Any, method: str, url: str,
                     body: Any, headers: Any) -> Dict[str, Any]:
    scrub = engine.redactor
    payload: Dict[str, Any] = {
        "method": str(method).upper(),
        "url": scrub.scrub_text(absolute_url(pool, str(url))),
        "headers": scrub.scrub_header_pairs(common.header_pairs(headers or {})),
    }
    raw = _body_bytes(body)
    # A file object or a generator: reading it here would consume the very
    # thing the caller is about to send.
    payload["body"] = {"streamed": True} if raw is None else common.encode_body(raw)
    return payload


#: What to ask the inner response for when the caller did not say. urllib3's
#: own default for ``stream()``.
READ_SIZE = 2 ** 16


class _RecordingBody:
    """A file-like between the caller and the real response.

    It drives the inner response with ``stream()`` rather than ``read(n)``, and
    that is the whole design. ``read(n)`` on a buffered response **blocks until
    it has n bytes or the connection ends**, so a six-frame event stream comes
    back as one 1376-byte read and every chunk boundary is gone before anything
    can record it. ``stream()`` yields what arrived, when it arrived -- for a
    chunked response that is one HTTP chunk at a time. Measured: the same
    stream records as six chunks this way and one the other way.

    Each ``read`` therefore returns a single inner chunk, which is a short read
    and legal for any file-like. ``read(None)`` still returns the whole body,
    because that is what asking for everything means and botocore reads a
    non-streaming response exactly that way -- the chunks are recorded
    individually and joined for the caller.

    The event is written when the body runs out or is closed, so a response the
    caller abandons half way through still leaves a record of what arrived.
    """

    def __init__(self, inner: Any, finish: Any, started: float) -> None:
        self._inner = inner
        self._finish = finish
        self._started = started
        self._chunks: List[bytes] = []
        self._offsets: List[float] = []
        self._iterator: Any = None
        self._done = False
        self._exhausted = False

    def _next_chunk(self, amt: Optional[int]) -> bytes:
        if self._iterator is None:
            with boundary():
                self._iterator = self._inner.stream(amt or READ_SIZE,
                                                    decode_content=False)
        with boundary():
            chunk = next(self._iterator, b"")
        if chunk:
            self._chunks.append(chunk)
            self._offsets.append(
                (_originals.perf_counter() - self._started) * 1000.0)
        else:
            self._exhausted = True
        return chunk

    def read(self, amt: Optional[int] = None) -> bytes:
        if amt is None:
            parts = []
            while True:
                chunk = self._next_chunk(READ_SIZE)
                if not chunk:
                    break
                parts.append(chunk)
            self._complete(aborted=False)
            return b"".join(parts)

        chunk = self._next_chunk(amt)
        if not chunk:
            self._complete(aborted=False)
        return chunk

    def close(self) -> None:
        try:
            with boundary():
                self._inner.close()
        finally:
            self._complete(aborted=True)

    @property
    def closed(self) -> bool:
        # urllib3's `is_fp_closed` asks this to decide whether `stream()` has
        # more to yield. It must answer for *this* object rather than delegate:
        # the inner response reports closed as soon as its connection is
        # drained, which happens while chunks are still queued in the iterator.
        return self._exhausted

    def _complete(self, aborted: bool) -> None:
        if self._done:
            return
        self._done = True
        self._finish(self._chunks, self._offsets, aborted and not self._chunks)


class _ReplayedBody:
    """Re-emits recorded chunks, one per read, boundary for boundary.

    One chunk per ``read`` regardless of the size asked for: a short read is
    legal for any file-like, and it is what reproduces the recorded framing
    exactly rather than a re-chunking of the same bytes.
    """

    def __init__(self, chunks: List[bytes], delays: List[float]) -> None:
        self._chunks = list(chunks)
        self._delays = list(delays)
        self._index = 0

    def read(self, amt: Optional[int] = None) -> bytes:
        if self._index >= len(self._chunks):
            return b""
        if self._index < len(self._delays) and self._delays[self._index]:
            _originals.sleep(self._delays[self._index])
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    def close(self) -> None:
        self._index = len(self._chunks)

    @property
    def closed(self) -> bool:
        return self._index >= len(self._chunks)


def _response_payload(engine: Any, status: int, headers: Any,
                      chunks: List[bytes], offsets: Optional[List[float]] = None,
                      aborted: bool = False) -> Dict[str, Any]:
    pairs = common.header_pairs(headers)
    payload: Dict[str, Any] = {
        "status": status,
        "headers": engine.redactor.scrub_header_pairs(pairs),
    }
    if common.is_stream_content_type(common.content_type_of(pairs)):
        payload["stream"] = common.encode_chunks(chunks, offsets)
    else:
        payload["body"] = common.encode_body(b"".join(chunks))
    if aborted:
        payload["aborted"] = True
    return payload


def _replayed_response(engine: Any, event: Any, module: Any, pool: Any,
                       method: str, url: str) -> Any:
    """Rebuild a ``urllib3.HTTPResponse`` from the tape, without a socket."""
    import urllib3.exceptions as exceptions
    from urllib3._collections import HTTPHeaderDict

    error = replay_support.recorded_error(event, exceptions)
    if error is not None:
        raise error

    res = engine.resolved(event, event.res) or {}
    headers = HTTPHeaderDict()
    for key, value in replay_support.response_headers(res):
        headers.add(key, value)

    if replay_support.is_stream(res):
        chunks, delays = replay_support.stream_parts(res, engine.realtime)
        body: Any = _ReplayedBody(chunks, delays)
    else:
        body = _ReplayedBody([common.decode_body(res.get("body"))], [])

    return module.HTTPResponse(
        body=body,
        headers=headers,
        status=res.get("status", 200),
        version=11,
        reason=res.get("reason") or "OK",
        preload_content=False,
        decode_content=False,
        request_method=str(method).upper(),
        request_url=absolute_url(pool, str(url)),
    )


class Urllib3Shim:
    """Patches ``HTTPConnectionPool.urlopen``."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self._restores: List = []

    def install(self) -> bool:
        try:
            import urllib3
            from urllib3.connectionpool import HTTPConnectionPool
        except ImportError:
            return False

        engine = self.engine
        original = HTTPConnectionPool.urlopen

        def urlopen(pool, method, url, body=None, headers=None, **kwargs):
            if not engine.enabled or in_boundary():
                return original(pool, method, url, body=body, headers=headers,
                                **kwargs)

            started = _originals.perf_counter()
            site = callsite.caller(1, skip_libraries=True)
            payload = _request_payload(engine, pool, method, url, body, headers)

            if getattr(engine, "replaying", False):
                event = engine.consume("http", payload, site=site)
                if event is not None:
                    return _replayed_response(engine, event, urllib3, pool,
                                              method, url)
                return original(pool, method, url, body=body, headers=headers,
                                **kwargs)

            try:
                with boundary():
                    response = original(pool, method, url, body=body,
                                        headers=headers, **kwargs)
            except BaseException as exc:
                # A connection error is a boundary crossing too: the agent saw
                # it and reacted to it, and a replay in which it now succeeds
                # is not a replay of that run.
                engine.record(
                    "http", payload, None, site=site,
                    t_rel=started - engine.t0,
                    dur_ms=(_originals.perf_counter() - started) * 1000.0,
                    meta={"error": {"type": type(exc).__name__,
                                    "message": str(exc)[:500]}},
                )
                raise

            def finish(chunks, offsets, aborted):
                engine.record(
                    "http", payload,
                    _response_payload(engine, response.status, response.headers,
                                      chunks, offsets, aborted),
                    site=site,
                    t_rel=started - engine.t0,
                    dur_ms=(_originals.perf_counter() - started) * 1000.0,
                )

            if kwargs.get("preload_content", True):
                # urllib3 already drained the body inside the call above, so
                # there is nothing left to wrap -- record what it read.
                with boundary():
                    data = response.data
                finish([data] if data else [], None, False)
                return response

            return urllib3.HTTPResponse(
                body=_RecordingBody(response, finish, started),
                headers=response.headers,
                status=response.status,
                version=getattr(response, "version", 11),
                reason=getattr(response, "reason", None),
                preload_content=False,
                decode_content=False,
                request_method=str(method).upper(),
                request_url=absolute_url(pool, str(url)),
            )

        HTTPConnectionPool.urlopen = urlopen
        self._restores.append(
            lambda: setattr(HTTPConnectionPool, "urlopen", original))
        return True

    def uninstall(self) -> None:
        for restore in reversed(self._restores):
            try:
                restore()
            except Exception:  # pragma: no cover - defensive
                pass
        self._restores.clear()
