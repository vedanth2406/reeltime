"""The ``requests`` fallback.

``requests`` predates httpx and is still what a lot of tool code uses. It has
no transport abstraction, so the interception point is
``HTTPAdapter.send`` -- one level higher than the httpx shim, and correspondingly
less faithful: a streamed response cannot be captured without consuming the very
stream the caller asked to iterate itself.

That case is recorded with ``meta.stream_not_captured`` rather than silently
producing a body-less event, so replay (M3) can refuse it loudly instead of
serving something wrong.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .. import _originals, callsite
from ..recorder import Recorder, boundary, in_boundary
from . import common


def _request_payload(recorder: Recorder, request: Any) -> Dict[str, Any]:
    scrub = recorder.redactor
    body = request.body
    if isinstance(body, str):
        body = body.encode("utf-8")
    elif body is not None and not isinstance(body, bytes):
        # A generator or file object: consuming it here would break the send.
        return {
            "method": request.method,
            "url": scrub.scrub_text(str(request.url)),
            "headers": scrub.scrub_header_pairs(common.header_pairs(request.headers)),
            "body": {"streamed": True},
        }
    return {
        "method": request.method,
        "url": scrub.scrub_text(str(request.url)),
        "headers": scrub.scrub_header_pairs(common.header_pairs(request.headers)),
        "body": common.encode_body(body),
    }


class RequestsShim:
    """Patches ``requests.adapters.HTTPAdapter.send``."""

    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self._restores: List = []

    def install(self) -> bool:
        try:
            from requests.adapters import HTTPAdapter
        except ImportError:
            return False

        recorder = self.recorder
        original = HTTPAdapter.send

        def send(adapter, request, **kwargs):
            if not recorder.enabled or in_boundary():
                return original(adapter, request, **kwargs)

            started = _originals.perf_counter()
            site = callsite.caller(1, skip_libraries=True)
            payload = _request_payload(recorder, request)
            try:
                with boundary():
                    response = original(adapter, request, **kwargs)
            except BaseException as exc:
                # A connection error is a boundary crossing too: the agent saw
                # it and reacted to it.
                recorder.record(
                    "http", payload, None, site=site,
                    t_rel=started - recorder.t0,
                    dur_ms=(_originals.perf_counter() - started) * 1000.0,
                    meta={"error": {"type": type(exc).__name__,
                                    "message": str(exc)[:500]}},
                )
                raise

            result: Dict[str, Any] = {
                "status": response.status_code,
                "headers": recorder.redactor.scrub_header_pairs(
                    common.header_pairs(response.headers)
                ),
            }
            meta: Dict[str, Any] = {}
            if kwargs.get("stream"):
                # Reading .content here would consume the stream the caller
                # asked to iterate. Say so in the trace instead of guessing.
                meta["stream_not_captured"] = True
            else:
                with boundary():
                    content = response.content
                result["body"] = common.encode_body(content)

            recorder.record(
                "http",
                payload,
                result,
                site=site,
                t_rel=started - recorder.t0,
                dur_ms=(_originals.perf_counter() - started) * 1000.0,
                meta=meta,
            )
            return response

        HTTPAdapter.send = send
        self._restores.append(lambda: setattr(HTTPAdapter, "send", original))
        return True

    def uninstall(self) -> None:
        for restore in reversed(self._restores):
            try:
                restore()
            except Exception:  # pragma: no cover - defensive
                pass
        self._restores.clear()
