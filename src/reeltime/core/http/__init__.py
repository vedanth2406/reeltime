"""HTTP interception.

Three shims, installed together and each independently optional: ``httpx``,
``httpx2``, and ``requests``. The first two are the primary path -- every
modern LLM SDK is built on one of them, and as of 2026 the OpenAI SDK has moved
to httpx2 while the Anthropic SDK is still on httpx. ``requests`` is the
fallback for older tool code.

Unlike the numpy patch, which only applies if numpy was already imported, these
import their target module eagerly when it is installed. ``tape run`` installs
reeltime at interpreter startup -- *before* the agent imports anything -- so a
"patch only what is already imported" rule would catch nothing at all.
"""

from __future__ import annotations

from typing import List

from ..recorder import Recorder
from .aiohttp_guard import AiohttpGuard
from .httpx_shim import HttpxShim
from .requests_shim import RequestsShim


class HttpShim:
    """Installs and removes every available HTTP interception point."""

    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self._shims = [
            ("httpx", HttpxShim(recorder, "httpx")),
            ("httpx2", HttpxShim(recorder, "httpx2")),
            ("requests", RequestsShim(recorder)),
        ]
        #: aiohttp is *not* one of the shims and does not go in ``installed``:
        #: it is not intercepted, and saying otherwise in the footer would be a
        #: lie in the one place a user goes to find out why a call was missed.
        #: The guard exists so that not intercepting it is never silent.
        #: See :mod:`reeltime.core.http.aiohttp_guard`.
        self._guards = [AiohttpGuard(recorder)]
        #: Backends actually patched, e.g. ``["httpx", "requests"]``. Written to
        #: the footer, so "why was my call not recorded?" needs no second run.
        self.installed: List[str] = []

    def install(self) -> "HttpShim":
        for name, shim in self._shims:
            if shim.install():
                self.installed.append(name)
        for guard in self._guards:
            guard.install()
        return self

    def uninstall(self) -> None:
        for _, shim in self._shims:
            shim.uninstall()
        for guard in self._guards:
            guard.uninstall()
        self.installed.clear()


__all__ = ["HttpShim", "HttpxShim", "RequestsShim", "AiohttpGuard"]
