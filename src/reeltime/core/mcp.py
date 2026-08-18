"""Recording MCP sessions.

An agent that talks to an MCP server crosses a boundary at every ``tools/call``
-- and at every ``tools/list`` too, which is the part that is easy to miss. A
server that exposes a different tool set between two runs changes what the
agent can even attempt, and if that traffic is recorded as opaque HTTP the
resulting divergence is unattributable: the agent simply did something else and
nothing in the trace says why.

So MCP gets its own event kind, carrying server identity, tool name, and
arguments as fields rather than as a serialised payload::

    import reeltime as tape

    async with tape.mcp.connect(command="python", args=["server.py"],
                                server="files") as session:
        tools = await session.list_tools()
        result = await session.call_tool("read_file", {"path": "a.txt"})

Both transports are supported: pass ``command``/``args`` for stdio, or ``url``
for HTTP (streamable HTTP by default, SSE with ``transport="sse"``).

**Replay never starts the server.** :func:`connect` opens a transport only when
the run can actually go live -- a recording, or a fork past its fork point. In
a pure replay the subprocess is never spawned and the URL is never contacted,
which is the whole point: replaying an agent should not require the world it
ran against to still exist. :func:`wrap` cannot make that promise, because the
caller has already opened the transport by the time it is handed over.

The HTTP transports go through httpx, which reeltime already intercepts. That
would record every MCP call twice -- once as ``mcp``, once as opaque ``http``
-- were it not for the boundary rule: the outermost boundary is the one
recorded, and the transport's request happens inside this one.
"""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from ..errors import TapeError
from .serial import to_jsonable
from .tape import current

#: ``req.op`` for each of the three operations that get recorded. The op is
#: part of the match key, so a server that happens to expose a tool called
#: ``initialize`` is still distinguishable from the handshake.
OP_INIT = "initialize"
OP_LIST = "list"
OP_CALL = "call"

#: ``req.name`` for the two operations that are not a tool call. They read as
#: their JSON-RPC method names because that is what they are.
NAME_INIT = "initialize"
NAME_LIST = "tools/list"


# -- talking to the SDK without importing it at module scope -------------


def _types():
    """``mcp.types``, or a TapeError explaining what to install.

    Imported lazily so that ``import reeltime`` costs nothing for the majority
    of users who have no MCP server, and so that reeltime keeps its
    standard-library-only runtime.
    """
    try:
        return importlib.import_module("mcp.types")
    except ImportError as exc:  # pragma: no cover - exercised by hand
        raise TapeError(
            "recording an MCP session needs the MCP SDK: pip install 'mcp>=1.9'"
        ) from exc


def _to_wire(value: Any) -> Any:
    """An SDK result object as its JSON-RPC wire form.

    ``by_alias`` is what makes this worth doing: the wire form is camelCase
    (``inputSchema``, ``isError``) and is fixed by the MCP specification, while
    the Python field names are the SDK's own and have already been renamed once
    between major versions. Recording the wire form means a trace stays
    readable, and rebuildable, across an SDK upgrade.
    """
    dump = getattr(value, "model_dump", None)
    if dump is not None:  # pydantic v2
        return dump(mode="json", by_alias=True, exclude_none=True)
    dump = getattr(value, "dict", None)
    if dump is not None:  # pragma: no cover - pydantic v1
        return dump(by_alias=True, exclude_none=True)
    return to_jsonable(value)


def _from_wire(type_name: str, payload: Any) -> Any:
    """Rebuild an SDK result object from a recorded wire form."""
    cls = getattr(_types(), type_name, None)
    if cls is None or not hasattr(cls, "model_validate"):  # pragma: no cover
        raise TapeError(
            "this MCP SDK has no {} to rebuild a recorded result into".format(type_name)
        )
    try:
        return cls.model_validate(payload)
    except Exception as exc:
        raise TapeError(
            "a recorded {} no longer validates against the installed MCP SDK "
            "({}). The trace header records the version it was recorded with; "
            "install that to replay this run.".format(type_name, exc)
        ) from exc


# -- naming a server -----------------------------------------------------


def _basename(text: str) -> str:
    """The last path segment, so an absolute path does not enter the trace.

    Server identity is part of the match key. A command recorded as
    ``/tmp/pytest-of-vedu/test_x0/server.py`` would fail to match the same
    server under tomorrow's temp directory, and the drift would be reported
    against every MCP event in the run.
    """
    cleaned = text.replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] or text


def server_id(
    command: Optional[str] = None,
    args: Optional[Sequence[str]] = None,
    url: Optional[str] = None,
) -> str:
    """A stable name for a server the caller did not name themselves."""
    if url:
        return url
    parts = [_basename(command or "?")]
    parts.extend(_basename(str(arg)) for arg in (args or []))
    return " ".join(parts)


# -- event payloads ------------------------------------------------------


def init_request(server: str) -> Dict[str, Any]:
    return {"server": server, "op": OP_INIT, "name": NAME_INIT, "args": {}}


def list_request(server: str) -> Dict[str, Any]:
    return {"server": server, "op": OP_LIST, "name": NAME_LIST, "args": {}}


def call_request(server: str, name: str, args: Any) -> Dict[str, Any]:
    return {"server": server, "op": OP_CALL, "name": name,
            "args": to_jsonable(args if args is not None else {})}


def init_result(wire: Dict[str, Any]) -> Dict[str, Any]:
    info = wire.get("serverInfo") or {}
    return {
        "server_name": info.get("name"),
        "server_version": info.get("version"),
        "protocol": wire.get("protocolVersion"),
        "capabilities": sorted(wire.get("capabilities") or {}),
        "result": wire,
    }


def list_result(wire: Dict[str, Any]) -> Dict[str, Any]:
    """The discovery result, with the tool *names* kept inline.

    ``result`` holds the full definitions and is large enough to be
    externalised into a blob. ``tools`` is the name list, and it deliberately
    stays inline and small: ``tape diff`` compares two traces without a blob
    store to hand, so a tool set that changed between runs has to be visible in
    the event itself or it cannot be reported as anything better than "the
    payload differs".
    """
    tools = [t.get("name") for t in (wire.get("tools") or []) if t.get("name")]
    return {"tools": tools, "count": len(tools), "result": wire}


def call_result(wire: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "value": render_value(wire),
        "is_error": bool(wire.get("isError")),
        "result": wire,
    }


def render_value(wire: Dict[str, Any]) -> Any:
    """What the tool returned, as something worth putting on one line.

    Structured output is the value if the server sent it; otherwise the text
    blocks joined, which is what an agent puts in front of the model anyway.
    """
    if "structuredContent" in wire:
        return wire["structuredContent"]
    texts = [block.get("text", "") for block in (wire.get("content") or [])
             if isinstance(block, dict) and block.get("type") == "text"]
    if texts:
        return "\n".join(texts)
    return wire.get("content")


# -- the session wrapper -------------------------------------------------


def _engine():
    tape = current()
    if tape is None or tape.closed:
        return None
    engine = tape.engine
    return engine if engine.enabled else None


def _substitute(engine: Any, name: str):
    """Ask a fork whether a ``--patch`` replaces this call's result."""
    if not getattr(engine, "forking", False):
        return (False, None)
    return engine.substitute("mcp", name)


def _patched_args(engine: Any, name: str, arguments: Any) -> Any:
    """``--patch mcp.<tool>.args`` -- the call still goes to the server."""
    if not getattr(engine, "forking", False):
        return arguments
    return engine.rewrite_args("mcp", name, arguments)


class TapedSession:
    """An MCP client session whose calls are recorded, or served from a tape.

    Duck-typed on the session it wraps rather than subclassing it: the SDK has
    already renamed and resigned ``ClientSession`` once between major versions,
    and the two methods that matter here -- ``list_tools`` and ``call_tool`` --
    are the two that did not change.
    """

    def __init__(self, inner: Any, server: str, transport: str = "unknown") -> None:
        self._inner = inner
        self.server = server
        self.transport = transport
        self._initialized: Optional[Any] = None

    def __repr__(self) -> str:
        return "<TapedSession {} via {}{}>".format(
            self.server, self.transport, "" if self._inner is not None else " (replay)")

    @property
    def live(self) -> bool:
        """Whether a real server is on the other end of this session."""
        return self._inner is not None

    def _require_live(self, op: str) -> Any:
        """The session to call, or an error that says why there is not one.

        Reached only if the tape declined to serve a call that a pure replay
        opened no transport for -- a boundary nested inside another one, which
        replay does not execute at all. Failing here beats an ``AttributeError``
        on ``None``, and beats starting the server behind the user's back.
        """
        if self._inner is None:
            raise TapeError(
                "replaying {} on {}, but the tape did not serve this {} and "
                "there is no live session to fall back to -- a replay must "
                "never quietly do the real thing".format(self, self.server, op)
            )
        return self._inner

    def __getattr__(self, name: str) -> Any:
        """Pass anything reeltime does not record through to the session.

        On replay there is nothing to pass it to, and answering with a live
        call would be worse than failing: it would make a replay depend on the
        server still existing, silently.
        """
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(
                "{!r} is replaying from a tape, so it has no live session to "
                "forward {!r} to. Only initialize, list_tools and call_tool "
                "are recorded.".format(self, name)
            )
        return getattr(inner, name)

    # -- the three recorded operations -----------------------------------

    async def initialize(self) -> Any:
        engine = _engine()
        if engine is None:
            return await self._require_live("initialize").initialize()

        request = init_request(self.server)
        if engine.replaying:
            event = engine.consume("mcp", request)
            if event is not None:
                res = engine.resolved(event, event.res) or {}
                self._initialized = _from_wire("InitializeResult", res.get("result"))
                return self._initialized

        with engine.capture("mcp", request) as cap:
            result = await self._require_live("initialize").initialize()
            cap.res = init_result(_to_wire(result))
            self._initialized = result
            return result

    async def list_tools(self, *args: Any, **kwargs: Any) -> Any:
        engine = _engine()
        if engine is None:
            return await self._require_live("list_tools").list_tools(*args, **kwargs)

        request = list_request(self.server)
        if engine.replaying:
            event = engine.consume("mcp", request)
            if event is not None:
                res = engine.resolved(event, event.res) or {}
                return _from_wire("ListToolsResult", res.get("result"))

        with engine.capture("mcp", request) as cap:
            result = await self._require_live("list_tools").list_tools(*args, **kwargs)
            cap.res = list_result(_to_wire(result))
            return result

    async def call_tool(self, name: str, arguments: Any = None,
                        *args: Any, **kwargs: Any) -> Any:
        engine = _engine()
        if engine is None:
            return await self._require_live("call_tool").call_tool(
                name, arguments, *args, **kwargs)

        request = call_request(self.server, name, arguments)
        if engine.replaying:
            event = engine.consume("mcp", request)
            if event is not None:
                # The call never reaches the server: that is what makes
                # replaying an agent whose tools delete things safe.
                res = engine.resolved(event, event.res) or {}
                return _from_wire("CallToolResult", res.get("result"))

        substituted, value = _substitute(engine, name)
        if substituted:
            wire = {"content": [{"type": "text", "text": _as_text(value)}],
                    "isError": False}
            engine.record("mcp", request, call_result(wire), meta={"patched": True})
            return _from_wire("CallToolResult", wire)

        arguments = _patched_args(engine, name, arguments)
        request = call_request(self.server, name, arguments)

        with engine.capture("mcp", request) as cap:
            result = await self._require_live("call_tool").call_tool(
                name, arguments, *args, **kwargs)
            cap.res = call_result(_to_wire(result))
            return result


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    import json

    return json.dumps(to_jsonable(value), ensure_ascii=False)


def wrap(session: Any, server: str, transport: str = "unknown") -> TapedSession:
    """Record an MCP session the caller opened themselves.

    Use this when a framework hands you a session. It records exactly what
    :func:`connect` records, but it cannot keep replay from starting the
    server: by the time a session exists, the subprocess is running or the
    connection is open. :func:`connect` owns the transport for that reason.
    """
    return TapedSession(session, server=server, transport=transport)


# -- opening a connection ------------------------------------------------


#: Environment that makes an interpreter start recording. An MCP server is a
#: subprocess of the agent, so it inherits whatever the agent was given --
#: including ``REELTIME_RUN_ID``, which would have it open the *same* trace file
#: and append its own header and events to a run it is not part of. The SDK
#: happens to pass only a small allowlist when ``env`` is None, but a caller who
#: passes ``env=os.environ`` to get one variable through would hand over all of
#: them, so this is scrubbed on the way out regardless.
def _clean_env(env, bootstrap_marker: str = "_bootstrap"):
    """``env`` with reeltime's own control variables removed."""
    if env is None:
        return None
    cleaned = {k: v for k, v in env.items()
               if not k.startswith("REELTIME_") and k != "TAPE_DIR"}
    path = cleaned.get("PYTHONPATH")
    if path:
        import os as _os

        kept = [part for part in path.split(_os.pathsep)
                if part and not part.endswith(bootstrap_marker)]
        if kept:
            cleaned["PYTHONPATH"] = _os.pathsep.join(kept)
        else:
            cleaned.pop("PYTHONPATH")
    return cleaned


def _stdio_transport(command: str, args: Sequence[str], env, cwd):
    mcp = importlib.import_module("mcp")
    params_cls = getattr(mcp, "StdioServerParameters", None)
    client = getattr(mcp, "stdio_client", None)
    if client is None:  # pragma: no cover - SDK 1.x layout
        client = importlib.import_module("mcp.client.stdio").stdio_client
    if params_cls is None:  # pragma: no cover - SDK 1.x layout
        params_cls = importlib.import_module("mcp.client.stdio").StdioServerParameters
    kwargs = {"command": command, "args": list(args or [])}
    if env is not None:
        kwargs["env"] = _clean_env(dict(env))
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    return client(params_cls(**kwargs))


def _http_transport(url: str, transport: str, headers):
    if transport == "sse":
        client = importlib.import_module("mcp.client.sse").sse_client
        return client(url, headers=dict(headers)) if headers else client(url)

    module = importlib.import_module("mcp.client.streamable_http")
    client = getattr(module, "streamable_http_client", None)
    if client is None:  # pragma: no cover - SDK 1.x spelling
        client = module.streamablehttp_client
    return client(url)


def _session_class():
    mcp = importlib.import_module("mcp")
    return mcp.ClientSession


@asynccontextmanager
async def connect(
    command: Optional[str] = None,
    args: Optional[Sequence[str]] = None,
    *,
    url: Optional[str] = None,
    server: Optional[str] = None,
    transport: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    initialize: bool = True,
) -> AsyncIterator[TapedSession]:
    """Open a recorded MCP session, or a replayed one that opens nothing.

    ::

        async with tape.mcp.connect("python", ["server.py"], server="files") as s:
            for tool in (await s.list_tools()).tools:
                ...

        async with tape.mcp.connect(url="http://localhost:8931/mcp") as s:
            ...

    The transport is opened eagerly whenever the run can go live, and not at
    all when it cannot. Deciding this once, at entry, rather than lazily at the
    first call is deliberate: the stdio transport runs its subprocess under an
    ``anyio`` task group, which has to be entered and exited from the same
    task, and a lazily-opened one would be entered from whichever task happened
    to make the first call.

    A fork counts as able to go live -- it continues for real past its fork
    point -- so it starts the server even when every MCP call it makes turns
    out to come from the replayed prefix.
    """
    if bool(command) == bool(url):
        raise TapeError(
            "tape.mcp.connect needs exactly one of command= (stdio) or url= (HTTP)"
        )
    name = server or server_id(command, args, url)
    kind = transport or ("stdio" if command else "http")

    engine = _engine()
    if engine is not None and engine.replaying and not getattr(engine, "forking", False):
        # A pure replay: no subprocess, no socket, no server needed. The
        # session serves every call from the tape and refuses anything else.
        taped = TapedSession(None, server=name, transport=kind)
        if initialize:
            await taped.initialize()
        yield taped
        return

    from contextlib import AsyncExitStack

    from .http import common as http_common

    async with AsyncExitStack() as stack:
        if command:
            streams = await stack.enter_async_context(
                _stdio_transport(command, args or [], env, cwd))
        else:
            # The transport's own POSTs are this boundary, not a second one.
            # Nesting cannot say so here: the SDK issues them from a task it
            # spawned before any boundary existed, and the flag is a
            # contextvar. So the endpoint is claimed for the duration instead.
            http_common.own_endpoint(url or "")
            stack.callback(http_common.release_endpoint, url or "")
            streams = await stack.enter_async_context(
                _http_transport(url or "", kind, headers))
        read, write = streams[0], streams[1]
        session = await stack.enter_async_context(_session_class()(read, write))
        taped = TapedSession(session, server=name, transport=kind)
        if initialize:
            await taped.initialize()
        yield taped


# -- reading mcp events back out -----------------------------------------


def tool_names(event: Any) -> Optional[List[str]]:
    """The tool set a discovery event recorded, without resolving blobs."""
    if event.kind != "mcp" or event.req.get("op") != OP_LIST:
        return None
    names = (event.res or {}).get("tools")
    return list(names) if isinstance(names, list) else None


def definitions_ref(event: Any) -> Any:
    """The full definitions a discovery event recorded, blob reference and all.

    Two runs whose definitions externalised to the same blob hash recorded the
    same tool definitions -- so content addressing answers "did the schemas
    change?" without either payload being read.
    """
    return (event.res or {}).get("result")
