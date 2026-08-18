"""The httpx transport shim -- the primary interception point.

Both the OpenAI and Anthropic SDKs are built on httpx, so intercepting the
transport catches every provider for free, plus any tool that makes an HTTP
call. Interception happens at the *transport*, not at ``Client.send``: a
transport is httpx's own documented extension point, it sees each redirect hop
individually, and it hands us the response stream unread, which is what makes
chunk-exact streaming capture possible.

``Client._transport_for_url`` is patched rather than a client's ``transport=``
argument, so clients constructed before or after ``install()`` are both caught
-- the lookup happens per request.

The shim is parameterised by module because there is now more than one httpx:
the OpenAI SDK moved to ``httpx2`` while the Anthropic SDK is still on
``httpx``. Both kept the same transport extension point, so supporting the pair
costs one argument. An SDK-layer interceptor would have had to be rewritten for
that migration -- which is the argument for principle 5 in a nutshell.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .. import _originals, callsite
from ..recorder import Recorder, boundary, in_boundary
from . import common, replay as replay_support

_SENTINEL = object()


def _extensions_meta(extensions: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The parts of httpx's response extensions worth keeping in the trace."""
    version = (extensions or {}).get("http_version")
    if not version:
        return {}
    if isinstance(version, bytes):
        version = version.decode("ascii", "replace")
    return {"http_version": version}


def _request_payload(recorder: Recorder, request: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "method": request.method,
        "url": recorder.redactor.scrub_text(str(request.url)),
        "headers": recorder.redactor.scrub_header_pairs(
            common.header_pairs(request.headers)
        ),
    }
    try:
        body = request.content
    except Exception:
        # A streaming upload: reading it here would consume the request.
        payload["body"] = {"streamed": True}
        return payload
    payload["body"] = common.encode_body(body)
    return payload


def _response_payload(
    recorder: Recorder,
    status_code: int,
    headers: Sequence,
    chunks: List[bytes],
    *,
    offsets: Optional[List[float]] = None,
    aborted: bool = False,
) -> Dict[str, Any]:
    pairs = common.header_pairs(headers)
    payload: Dict[str, Any] = {
        "status": status_code,
        "headers": recorder.redactor.scrub_header_pairs(pairs),
    }
    if common.is_stream_content_type(common.content_type_of(pairs)):
        # Chunk boundaries are the data for a stream, so keep the ordered list
        # rather than the assembled body.
        payload["stream"] = common.encode_chunks(chunks, offsets)
    else:
        payload["body"] = common.encode_body(b"".join(chunks))
    if aborted:
        payload["aborted"] = True
    return payload


class _RecordingByteStream:
    """Passes chunks through untouched while keeping the ordered list.

    The event is written when the stream ends or is closed, so a response the
    caller abandons half way through still leaves a record of what arrived.
    """

    def __init__(self, inner: Any, finish, started: float) -> None:
        self._inner = inner
        self._finish = finish
        self._started = started
        self._chunks: List[bytes] = []
        self._offsets: List[float] = []
        self._done = False

    def _mark(self, chunk: bytes) -> None:
        self._chunks.append(chunk)
        self._offsets.append((_originals.perf_counter() - self._started) * 1000.0)

    def __iter__(self) -> Iterator[bytes]:
        iterator = iter(self._inner)
        try:
            while True:
                # The transport's own clock reads and buffers belong to this
                # boundary, not to the agent, and will not happen on replay.
                with boundary():
                    chunk = next(iterator, _SENTINEL)
                if chunk is _SENTINEL:
                    break
                self._mark(chunk)
                yield chunk
        finally:
            self._complete(aborted=False)

    def close(self) -> None:
        try:
            closer = getattr(self._inner, "close", None)
            if closer is not None:
                with boundary():
                    closer()
        finally:
            self._complete(aborted=True)

    def _complete(self, aborted: bool) -> None:
        if self._done:
            return
        self._done = True
        self._finish(self._chunks, self._offsets, aborted and not self._chunks)


class _AsyncRecordingByteStream(_RecordingByteStream):
    async def __aiter__(self):
        iterator = self._inner.__aiter__()
        try:
            while True:
                with boundary():
                    try:
                        chunk = await iterator.__anext__()
                    except StopAsyncIteration:
                        break
                self._mark(chunk)
                yield chunk
        finally:
            self._complete(aborted=False)

    async def aclose(self) -> None:
        try:
            closer = getattr(self._inner, "aclose", None)
            if closer is not None:
                with boundary():
                    await closer()
        finally:
            self._complete(aborted=True)


def _make_recorder_callback(recorder: Recorder, request: Any, started: float, site, span):
    request_payload = _request_payload(recorder, request)

    def finish(chunks: List[bytes], offsets: List[float], aborted: bool) -> None:
        recorder.record(
            "http",
            request_payload,
            _response_payload(
                recorder, finish.status_code, finish.headers, chunks,
                offsets=offsets, aborted=aborted,
            ),
            site=site,
            span=span,
            t_rel=started - recorder.t0,
            dur_ms=(_originals.perf_counter() - started) * 1000.0,
            meta=finish.meta,
        )

    finish.status_code = 0
    finish.headers = []
    finish.meta = {}

    def failed(exc: BaseException) -> None:
        """Record a request that never produced a response.

        A connection error or a timeout is something the agent saw and reacted
        to, so it belongs on the tape; replay has to be able to raise it again
        rather than quietly succeeding where the recorded run failed.
        """
        recorder.record(
            "http",
            request_payload,
            None,
            site=site,
            span=span,
            t_rel=started - recorder.t0,
            dur_ms=(_originals.perf_counter() - started) * 1000.0,
            meta={"error": {"type": type(exc).__name__, "message": str(exc)[:500]}},
        )

    finish.failed = failed
    return finish


class HttpxShim:
    """Patches an httpx-shaped module so every request through it is recorded.

    ``module_name`` is ``"httpx"`` or ``"httpx2"``; both expose the same
    ``Client._transport_for_url`` hook and the same byte-stream ABCs.

    The same patch serves replay: in that mode the wrapped transport never
    calls the one underneath it, and answers from the tape instead.
    """

    def __init__(self, engine: Any, module_name: str = "httpx") -> None:
        self.engine = engine
        self.module_name = module_name
        self._restores: List = []

    def install(self) -> bool:
        try:
            httpx = importlib.import_module(self.module_name)
        except ImportError:
            return False

        recorder = self.engine
        original_sync = httpx.Client._transport_for_url
        original_async = httpx.AsyncClient._transport_for_url

        # httpx asserts the stream it gets back is one of its own ABCs, and
        # those only exist once httpx is importable -- hence subclassing here
        # rather than at module scope. Our implementation comes first in the
        # MRO so it wins over the base class's NotImplementedError stubs.
        class SyncStream(_RecordingByteStream, httpx.SyncByteStream):
            pass

        class AsyncStream(_AsyncRecordingByteStream, httpx.AsyncByteStream):
            pass

        class ReplayStream(replay_support.ReplayedByteStream, httpx.SyncByteStream):
            pass

        class AsyncReplayStream(replay_support.ReplayedByteStream, httpx.AsyncByteStream):
            pass

        def replayed_response(event):
            """Rebuild the recorded exchange, or re-raise what it recorded."""
            error = replay_support.recorded_error(event, httpx)
            if error is not None:
                raise error
            res = recorder.resolved(event, event.res) or {}
            headers = replay_support.response_headers(res)
            status = res.get("status", 200)
            if replay_support.is_stream(res):
                chunks, delays = replay_support.stream_parts(res, recorder.realtime)
                return httpx.Response(status, headers=headers,
                                      stream=ReplayStream(chunks, delays))
            return httpx.Response(status, headers=headers,
                                  content=common.decode_body(res.get("body")))

        def replayed_async_response(event):
            error = replay_support.recorded_error(event, httpx)
            if error is not None:
                raise error
            res = recorder.resolved(event, event.res) or {}
            headers = replay_support.response_headers(res)
            status = res.get("status", 200)
            if replay_support.is_stream(res):
                chunks, delays = replay_support.stream_parts(res, recorder.realtime)
                return httpx.Response(status, headers=headers,
                                      stream=AsyncReplayStream(chunks, delays))
            return httpx.Response(status, headers=headers,
                                  content=common.decode_body(res.get("body")))

        class RecordingTransport(httpx.BaseTransport):
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def handle_request(self, request):
                if not recorder.enabled or in_boundary():
                    return self._inner.handle_request(request)
                if recorder.replaying:
                    # Never reaches self._inner: replay makes no network calls.
                    event = recorder.consume(
                        "http", _request_payload(recorder, request),
                        site=callsite.caller(1, skip_libraries=True))
                    if event is not None:
                        return replayed_response(event)
                    return self._inner.handle_request(request)
                started = _originals.perf_counter()
                site = callsite.caller(1, skip_libraries=True)
                finish = _make_recorder_callback(recorder, request, started, site, None)
                try:
                    with boundary():
                        response = self._inner.handle_request(request)
                except BaseException as exc:
                    finish.failed(exc)
                    raise
                finish.status_code = response.status_code
                finish.headers = response.headers
                finish.meta = _extensions_meta(response.extensions)
                stream = SyncStream(response.stream, finish, started)
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    stream=stream,
                    extensions=response.extensions,
                )

            def close(self) -> None:
                self._inner.close()

        class AsyncRecordingTransport(httpx.AsyncBaseTransport):
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            async def handle_async_request(self, request):
                if not recorder.enabled or in_boundary():
                    return await self._inner.handle_async_request(request)
                if recorder.replaying:
                    event = recorder.consume(
                        "http", _request_payload(recorder, request),
                        site=callsite.caller(1, skip_libraries=True))
                    if event is not None:
                        return replayed_async_response(event)
                    return await self._inner.handle_async_request(request)
                started = _originals.perf_counter()
                site = callsite.caller(1, skip_libraries=True)
                finish = _make_recorder_callback(recorder, request, started, site, None)
                try:
                    with boundary():
                        response = await self._inner.handle_async_request(request)
                except BaseException as exc:
                    finish.failed(exc)
                    raise
                finish.status_code = response.status_code
                finish.headers = response.headers
                finish.meta = _extensions_meta(response.extensions)
                stream = AsyncStream(response.stream, finish, started)
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    stream=stream,
                    extensions=response.extensions,
                )

            async def aclose(self) -> None:
                await self._inner.aclose()

        def transport_for_url(client, url):
            return RecordingTransport(original_sync(client, url))

        def async_transport_for_url(client, url):
            return AsyncRecordingTransport(original_async(client, url))

        httpx.Client._transport_for_url = transport_for_url
        httpx.AsyncClient._transport_for_url = async_transport_for_url
        self._restores.append(
            lambda: setattr(httpx.Client, "_transport_for_url", original_sync)
        )
        self._restores.append(
            lambda: setattr(httpx.AsyncClient, "_transport_for_url", original_async)
        )
        return True

    def uninstall(self) -> None:
        for restore in reversed(self._restores):
            try:
                restore()
            except Exception:  # pragma: no cover - defensive
                pass
        self._restores.clear()
