"""aiohttp: not intercepted, and never quietly so.

``aiohttp`` is listed as an unsupported stack, and after M9 it stays that way.
The reason is not that nobody uses it; it is that aiohttp has no seam at the
level reeltime records at, and faking one means owning aiohttp's private
internals release by release:

* **Recording** would mean patching ``ClientSession._request`` -- a private
  method with 33 parameters -- rather than a documented transport. httpx
  publishes ``BaseTransport.handle_request(Request) -> Response`` and promises
  it; aiohttp publishes ``TraceConfig``, which is observe-only and can neither
  substitute a response nor supply a chunk.
* **Replay** would mean constructing a ``ClientResponse`` from recorded bytes.
  Its constructor demands a writer task, a timer, a stream writer and a live
  session, and the ``StreamReader`` under it calls
  ``protocol.resume_reading(resume_parser=…)`` -- a keyword that appears in no
  public interface. It can be done; it was prototyped; it is eight private
  attributes and two fake objects that would need re-verifying on every aiohttp
  release. The httpx equivalent is one public constructor.
* **Nothing reeltime targets needs it.** OpenAI, Anthropic, Google GenAI and
  the MCP SDK are all built on httpx or httpx2. The realistic exposure is an
  agent's own tool code fetching something -- and ``@tape.tool`` already covers
  that at a better boundary, because what replay needs is the tool's result,
  not the HTTP call inside it.

What *is* unacceptable is the consequence of leaving it alone. An unrecorded
aiohttp request during a replay goes to the real network: no event, no error,
no sign. That is precisely the silent divergence design principle 4 forbids, so
this module patches the same private method -- **not to intercept it, but to
refuse it**. A replay that reaches an aiohttp request stops and says why. A
recording that reaches one warns once, so the gap is visible in the run that
created it rather than in the replay a week later.

The escape hatch is the same boundary rule as everywhere else: an aiohttp call
inside a ``@tape.tool`` body is not this boundary, so it passes through
untouched, and on replay it is never reached at all because the body does not
run.
"""

from __future__ import annotations

import logging
from typing import Any, List

from ...errors import TapeError
from ..recorder import in_boundary

logger = logging.getLogger("reeltime")

ADVICE = (
    "reeltime does not intercept aiohttp -- only httpx, httpx2 and requests. "
    "Wrap the call in a @tape.tool function and reeltime will record its "
    "result, which is the boundary replay actually needs."
)


class AiohttpGuard:
    """Makes an un-intercepted aiohttp request loud instead of invisible."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self._restores: List = []
        #: One warning per run. An agent in a loop would otherwise emit
        #: hundreds, and the second one carries no information the first did
        #: not.
        self.warned = False

    def install(self) -> bool:
        try:
            from aiohttp import ClientSession
        except ImportError:
            return False

        engine = self.engine
        guard = self
        original = ClientSession._request

        def _request(session, method, str_or_url, *args, **kwargs):
            if not engine.enabled or in_boundary():
                return original(session, method, str_or_url, *args, **kwargs)
            if getattr(engine, "replaying", False):
                raise TapeError(
                    "this replay reached a live aiohttp request -- {} {} -- and "
                    "a replay must never quietly do the real thing.\n\n{}".format(
                        method, str_or_url, ADVICE)
                )
            if not guard.warned:
                guard.warned = True
                logger.warning(
                    "reeltime: %s %s was not recorded. %s", method, str_or_url,
                    ADVICE)
            return original(session, method, str_or_url, *args, **kwargs)

        ClientSession._request = _request
        self._restores.append(
            lambda: setattr(ClientSession, "_request", original))
        return True

    def uninstall(self) -> None:
        for restore in reversed(self._restores):
            try:
                restore()
            except Exception:  # pragma: no cover - defensive
                pass
        self._restores.clear()
        self.warned = False
