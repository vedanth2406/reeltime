"""Public surface for the MCP adapter -- see :mod:`reeltime.core.mcp`.

Exists so that both spellings work::

    import reeltime as tape
    async with tape.mcp.connect("python", ["server.py"]) as session: ...

    from reeltime.mcp import connect
"""

from __future__ import annotations

from .core.mcp import TapedSession, connect, server_id, wrap

__all__ = ["connect", "wrap", "server_id", "TapedSession"]
