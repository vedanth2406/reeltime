"""Recording LangChain runs as chain structure rather than as opaque HTTP.

A LangChain agent is a *tree*: an executor calls a model node, the model asks
for a tool, a tool node runs it, the executor loops. Intercepting at the
transport layer sees only the leaves of that tree -- two POSTs to
``/v1/chat/completions`` with a growing message array -- and none of the shape
that decided them. When such a run goes wrong the trace says the agent asked
something different the second time, and nothing in it says why.

So a LangChain node gets its own event kind, carrying node identity, the path
it sits on, its depth, and its inputs and outputs as named fields::

    import reeltime as tape

    tape.langchain.install()          # every chain in this process records
    agent.invoke({"messages": [...]})

or, without touching the script at all::

    tape run --langchain python agent.py

The result is a trace you can read as a tree::

    3  chain    12ms  agent.py:41   LangGraph/model  (depth 1)
    4  llm     840ms  agent.py:41   gpt-4o-mini 120→30 "I'll shout that"
    5  chain     0ms  agent.py:41   LangGraph/tools/shout  (depth 2) → "HI"

**A chain node is structure, not a boundary.** This is the single decision the
rest of the module follows from. The outermost-boundary rule -- an HTTP call
inside a ``@tape.tool`` body is not recorded twice -- cannot apply here,
because a callback handler is an *observer*: it is told that a node started, it
cannot stop the node from running. If a chain node opened a recording boundary,
the model call inside it would be suppressed at record time and would then go
live on replay, which is the one thing this tool must never do. So chain events
nest around other events instead of standing in for them.

The corollary is what keeps the count honest: **the adapter does not record LLM
nodes.** ``on_chat_model_start`` fires for the same crossing the transport shim
already records with the actual wire bytes, the token counts and the streaming
chunks, so recording it again would be two events for one boundary. LLM nodes
are still tracked, because their children need the right depth -- they are just
never written. See :func:`recordable`.

Replay re-runs the chain for real. Its model calls are served from the tape, so
it takes the same path and fires the same callbacks, and each one *consumes* its
recorded event: a chain whose structure changed is reported as drift rather
than passing unnoticed. Nothing about the node's recorded outputs is fed back
into the run -- a callback cannot do that either -- which is why ``chain`` has
no ``--patch`` fields. Patch the ``llm`` boundary inside the node instead.
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
from typing import Any, Dict, Iterator, Optional, Sequence, Tuple

from ..errors import TapeConfigError, TapeError
from . import _originals, callsite, spans
from .serial import to_jsonable
from .tape import current

#: The distribution whose callback contract this adapter is written against.
#: ``langchain``, ``langgraph`` and every partner package route their callbacks
#: through this one, so it is the only version worth gating on.
PACKAGE = "langchain-core"

#: Supported ``langchain-core`` range, ``[minimum, exclusive maximum)``.
#: LangChain's internals move fast; the callback contract does not, but it is
#: not promised either, so the range states what is actually tested rather than
#: what is likely to work. See ``tests/test_langchain.py``.
MINIMUM = (0, 3)
BELOW = (2, 0)

#: Set by :func:`install`, read by ``langchain_core`` itself: with a handler
#: class registered against it, LangChain builds a handler for every run tree
#: it configures, in whatever thread that happens in. A ContextVar alone would
#: miss any chain invoked from a thread the caller did not start.
ENV_VAR = "REELTIME_LANGCHAIN"

#: ``req.type`` for each node the adapter knows how to name.
TYPE_CHAIN = "chain"
TYPE_TOOL = "tool"
TYPE_RETRIEVER = "retriever"
TYPE_LLM = "llm"
TYPE_AGENT_ACTION = "agent_action"
TYPE_AGENT_FINISH = "agent_finish"

#: A prompt template and an output parser are runnables like any other, and
#: LangChain distinguishes them only by a ``run_type`` keyword. Carrying that
#: through is what makes ``tape diff`` able to say "an output parser was added
#: here" rather than "a node called StrOutputParser was added here".
TYPE_PROMPT = "prompt"
TYPE_PARSER = "parser"
REFINED_TYPES = (TYPE_PROMPT, TYPE_PARSER, TYPE_RETRIEVER)

#: **Every node except the model node becomes an event.** One rule, because a
#: rule with exceptions is a rule people get wrong: the transport shim already
#: records the model crossing, with the wire bytes, the token counts and the
#: streaming chunks that the callback does not carry, so recording it here as
#: well would be two events for one boundary. Model nodes are still tracked --
#: their children need the right depth -- they are just never written.
NOT_RECORDED = (TYPE_LLM,)

#: How deep a path is allowed to get before it stops being worth reading. A
#: runaway recursive chain would otherwise put a kilobyte of slash-separated
#: names in every event's match key.
MAX_PATH = 12

SEPARATOR = "/"


def recordable(node_type: str) -> bool:
    """Whether a node of this type becomes an event."""
    return node_type not in NOT_RECORDED


# -- the version gate ----------------------------------------------------


def _installed_version() -> Optional[str]:
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - Python < 3.8
        return None
    try:
        return metadata.version(PACKAGE)
    except Exception:
        return None


def parse_version(text: Optional[str]) -> Optional[Tuple[int, ...]]:
    """``"1.5.6"`` -> ``(1, 5, 6)``. Anything unparseable is None.

    Deliberately not a full PEP 440 parser: only the leading numeric release
    segment decides whether the callback contract is one we have tested, and a
    dependency on ``packaging`` to learn that would be a poor trade.
    """
    if not text:
        return None
    match = re.match(r"^\s*(\d+(?:\.\d+)*)", str(text))
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _render(version: Sequence[int]) -> str:
    return ".".join(str(part) for part in version)


def supported(version: Optional[Tuple[int, ...]]) -> bool:
    if version is None:
        return False
    return MINIMUM <= tuple(version[:2]) < BELOW


def check_version(allow_unsupported: bool = False) -> Optional[Tuple[int, ...]]:
    """Refuse to record against a LangChain this adapter has not been tested on.

    A callback contract that shifted underneath us does not fail loudly -- it
    records a node under a different name, or stops reporting a node at all,
    and the trace looks fine until a replay two weeks later cannot be matched.
    Recording something subtly wrong is worse than recording nothing, so an
    untested version stops here with the range it would need.
    """
    installed = _installed_version()
    if installed is None:
        raise TapeError(
            "recording a LangChain run needs langchain-core: "
            "pip install 'langchain-core>={}'".format(_render(MINIMUM))
        )
    version = parse_version(installed)
    if supported(version) or allow_unsupported:
        return version
    raise TapeError(
        "reeltime's LangChain adapter is tested against langchain-core "
        ">={},<{} and you have {}. The callback contract is not promised "
        "across that gap, so recording would risk a trace that looks right and "
        "replays wrong. Pin a supported version, or pass "
        "allow_unsupported=True (or set {}=force) to record anyway and check "
        "the result yourself.".format(
            _render(MINIMUM), _render(BELOW), installed, ENV_VAR)
    )


# -- naming a node -------------------------------------------------------


def node_name(serialized: Any, kwargs: Optional[Dict[str, Any]] = None) -> str:
    """What to call this node, across three spellings of the same answer.

    ``serialized`` was a dict carrying an ``id`` path in langchain-core 0.x and
    is frequently ``None`` in 1.x, where the name arrives as a keyword instead.
    Trying all of them costs nothing and is the difference between a readable
    trace and a column of ``"chain"``.
    """
    name = (kwargs or {}).get("name")
    if isinstance(name, str) and name:
        return name
    if isinstance(serialized, dict):
        for key in ("name", "id"):
            value = serialized.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, (list, tuple)) and value:
                return str(value[-1])
    return "chain"


#: LangChain's own structural tags: which branch of a sequence or a map this
#: node is. They are deterministic and they separate two otherwise identical
#: sibling nodes, so they belong in the match key; every other tag is a user
#: label and does not.
_STRUCTURAL_TAG = re.compile(r"^(seq:step:\d+|map:key:.+)$")


def step_tag(tags: Optional[Sequence[str]]) -> Optional[str]:
    for tag in tags or ():
        if isinstance(tag, str) and _STRUCTURAL_TAG.match(tag):
            return tag
    return None


def run_type_of(kwargs: Optional[Dict[str, Any]]) -> Optional[str]:
    value = (kwargs or {}).get("run_type")
    return value if isinstance(value, str) and value else None


# -- payloads ------------------------------------------------------------

#: A LangChain message carries an ``id`` minted per run: a bare uuid4 for one
#: the framework created, or a run-derived id with a generation index for one
#: that came back from a model. They are the *only* part of a node's payload
#: that differs between two identical runs -- which was measured, not assumed --
#: and leaving them in would make every downstream node's inputs differ, so
#: `tape diff` would report noise at every step of two runs that did the same
#: thing.
#:
#: The prefix on the run-derived form is ``run--`` on langchain-core 0.3 and
#: ``lc_run--`` on 1.x. That rename is exactly the kind of quiet drift the
#: version floor in CI exists to catch, and it did: both are matched here.
_RUN_ID = re.compile(
    r"^(lc_run--|run--)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(-\d+)?$"
)


def stable(value: Any, _depth: int = 0) -> Any:
    """``value`` with LangChain's per-run message ids removed."""
    if _depth > 24:  # pragma: no cover - deeper than to_jsonable itself allows
        return value
    if isinstance(value, dict):
        return {
            key: stable(item, _depth + 1)
            for key, item in value.items()
            if not (key == "id" and isinstance(item, str) and _RUN_ID.match(item))
        }
    if isinstance(value, list):
        return [stable(item, _depth + 1) for item in value]
    return value


def payload(value: Any) -> Any:
    """A callback argument, JSON-safe and stripped of per-run ids."""
    return stable(to_jsonable(value))


def request(
    name: str,
    node_type: str,
    path: str,
    depth: int,
    inputs: Any = None,
    step: Optional[str] = None,
) -> Dict[str, Any]:
    """The ``req`` half of a chain event."""
    out: Dict[str, Any] = {
        "framework": "langchain",
        "name": name,
        "type": node_type,
        "path": path,
        "depth": depth,
    }
    if step:
        out["step"] = step
    out["inputs"] = payload(inputs)
    return out


def result(outputs: Any, children: int = 0) -> Dict[str, Any]:
    """The ``res`` half of a chain event.

    ``children`` is how many nodes ran underneath this one. It is the cheapest
    structural signal there is -- a node that used to fan out to three tools and
    now fans out to one has changed what the agent does, and the count says so
    without either payload being read.
    """
    return {"outputs": payload(outputs), "value": render_value(outputs),
            "children": children}


#: A node output rendered longer than this stops being a summary.
VALUE_LIMIT = 200

#: Where LangChain and LangGraph keep the answer, in the order worth trying.
#: A node's full outputs are always recorded; this only decides what fits on
#: one line of `tape show`, so a shape nobody anticipated degrades to the JSON
#: rather than to nothing.
ANSWER_KEYS = ("output", "text", "content", "result", "answer", "update",
               "return_values")


def _unwrap(value: Any, budget: int = 6) -> Any:
    """Follow the framework's own envelopes down to the thing a human wanted."""
    if budget <= 0:
        return value
    if isinstance(value, list) and len(value) == 1:
        return _unwrap(value[0], budget - 1)
    if isinstance(value, dict):
        # `messages` is the shape a graph carries state in, and the last one is
        # what this node just produced.
        messages = value.get("messages")
        if isinstance(messages, list) and messages:
            return _unwrap(messages[-1], budget - 1)
        for key in ANSWER_KEYS:
            if key in value:
                return _unwrap(value[key], budget - 1)
    return value


def render_value(outputs: Any) -> Any:
    """What the node produced, as something worth putting on one line."""
    import json

    value = _unwrap(payload(outputs))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:  # pragma: no cover - defensive
            text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= VALUE_LIMIT else text[: VALUE_LIMIT - 1] + "…"


def join(parts: Sequence[str]) -> str:
    """A node path, capped so a recursive chain cannot run away with it."""
    if len(parts) <= MAX_PATH:
        return SEPARATOR.join(parts)
    return SEPARATOR.join(("…",) + tuple(parts[-MAX_PATH:]))


# -- the tracker ---------------------------------------------------------


class Node:
    """One in-flight LangChain run."""

    __slots__ = ("name", "type", "path", "depth", "step", "inputs",
                 "site", "span", "t_rel", "started", "children", "record")

    def __init__(self, name: str, node_type: str, path: str, depth: int,
                 step: Optional[str], inputs: Any, site: Any, span: str,
                 t_rel: float, started: float) -> None:
        self.name = name
        self.type = node_type
        self.path = path
        self.depth = depth
        self.step = step
        self.inputs = inputs
        self.site = site
        self.span = span
        self.t_rel = t_rel
        self.started = started
        self.children = 0
        self.record = recordable(node_type)


def _engine() -> Any:
    tape = current()
    if tape is None or tape.closed:
        return None
    engine = tape.engine
    return engine if engine.enabled else None


class Tracker:
    """The whole adapter, with no LangChain import anywhere in it.

    The callback handler in :func:`handler_class` is a shell that forwards to
    this. Keeping the logic here means every rule in the module docstring is
    testable without a framework installed, and means the shell -- the only part
    that touches an API that moves -- stays small enough to read in one go.
    """

    def __init__(self) -> None:
        #: run id -> Node. LangChain runs callbacks from whichever thread is
        #: executing the node, and ``batch`` uses a pool, so this is locked.
        self._nodes: Dict[Any, Node] = {}
        self._lock = threading.Lock()
        self._checked_tape = False

    # -- the four things a handler tells us ------------------------------

    def start(
        self,
        node_type: str,
        serialized: Any,
        inputs: Any,
        run_id: Any,
        parent_run_id: Any = None,
        tags: Optional[Sequence[str]] = None,
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        """A node began. Nothing is written until it ends."""
        engine = _engine()
        name = node_name(serialized, kwargs)
        run_type = run_type_of(kwargs)
        if node_type == TYPE_CHAIN and run_type in REFINED_TYPES:
            node_type = run_type
        with self._lock:
            parent = self._nodes.get(parent_run_id)
            depth = 0 if parent is None else parent.depth + 1
            trail = (name,) if parent is None else tuple(
                parent.path.split(SEPARATOR)) + (name,)
            if parent is not None:
                parent.children += 1
            self._nodes[run_id] = Node(
                name=name,
                node_type=node_type,
                path=join(trail),
                depth=depth,
                step=step_tag(tags),
                inputs=inputs,
                # A child inherits its parent's site and span rather than
                # reading its own. LangGraph runs nodes on a thread pool, so a
                # child's callback frequently fires on a stack with no user
                # frame on it at all, and the nearest answer is then a line
                # number inside langchain -- which moves on every upgrade and
                # would drift every event in the run. Every node in one run
                # tree belongs to the one `.invoke()` that started it, so that
                # is the line to name; only a root walks the stack.
                site=parent.site if parent is not None else callsite.caller(
                    1, skip_libraries=True),
                span=parent.span if parent is not None else spans.current(),
                t_rel=0.0 if engine is None else _originals.perf_counter() - engine.t0,
                started=_originals.perf_counter(),
            )

    def end(self, run_id: Any, outputs: Any) -> None:
        """A node finished. This is where the event is written."""
        node = self._take(run_id)
        if node is not None:
            self._emit(node, result(outputs, node.children))

    def error(self, run_id: Any, exc: BaseException) -> None:
        """A node raised. Still a crossing: the agent saw this and acted on it."""
        node = self._take(run_id)
        if node is not None:
            self._emit(node, result(None, node.children), meta={"error": {
                "type": type(exc).__name__, "message": str(exc)[:500]}})

    def point(self, node_type: str, name: str, run_id: Any,
              parent_run_id: Any, value: Any) -> None:
        """A node with no duration -- an agent's decision to act, or to stop."""
        engine = _engine()
        with self._lock:
            parent = self._nodes.get(parent_run_id) or self._nodes.get(run_id)
            depth = 0 if parent is None else parent.depth + 1
            trail = (name,) if parent is None else tuple(
                parent.path.split(SEPARATOR)) + (name,)
            node = Node(
                name=name, node_type=node_type, path=join(trail), depth=depth,
                step=None, inputs=None,
                site=parent.site if parent is not None else callsite.caller(
                    1, skip_libraries=True),
                span=parent.span if parent is not None else spans.current(),
                t_rel=0.0 if engine is None else _originals.perf_counter() - engine.t0,
                started=_originals.perf_counter(),
            )
        self._emit(node, result(value, 0))

    # -- writing ---------------------------------------------------------

    def _take(self, run_id: Any) -> Optional[Node]:
        with self._lock:
            return self._nodes.pop(run_id, None)

    def _emit(self, node: Node, res: Dict[str, Any],
              meta: Optional[Dict[str, Any]] = None) -> None:
        if not node.record:
            return
        engine = _engine()
        if engine is None:
            return

        req = request(node.name, node.type, node.path, node.depth,
                      node.inputs, node.step)

        if getattr(engine, "replaying", False):
            self._guard(engine)
            # The recorded outputs are not fed back: the chain has already
            # computed its own, from model calls the tape served. Consuming the
            # event is what turns a changed chain structure into a reported
            # drift instead of a silent one.
            engine.consume("chain", req, site=node.site, span=node.span)
            return

        engine.record(
            "chain", req, res, meta=meta, site=node.site, span=node.span,
            t_rel=node.t_rel,
            dur_ms=(_originals.perf_counter() - node.started) * 1000.0,
        )

    def _guard(self, engine: Any) -> None:
        """Say what is wrong when the tape has no chain events at all.

        Replaying a run that was recorded without the adapter would otherwise
        fail as an ordinary TapeMiss on the first node, and the honest answer --
        the adapter was turned on after the recording was made -- is not
        something a match failure can express.
        """
        if self._checked_tape:
            return
        self._checked_tape = True
        trace = _trace_of(engine)
        if trace is None or any(e.kind == "chain" for e in trace.events):
            return
        raise TapeConfigError(
            "run {} has no chain events, so it was recorded without the "
            "LangChain adapter, and this replay has it switched on. Replay "
            "without it, or record the run again with it.".format(trace.run_id)
        )


def _trace_of(engine: Any) -> Any:
    """The trace an engine is replaying, whether it is a Player or a fork."""
    trace = getattr(engine, "trace", None)
    if trace is not None:
        return trace
    player = getattr(engine, "player", None)
    return None if player is None else getattr(player, "trace", None)


# -- the callback handler ------------------------------------------------

_handler_class: Optional[type] = None


def _base_handler() -> type:
    """``BaseCallbackHandler``, or a TapeError explaining what to install.

    Imported lazily, and subclassed lazily with it: ``import reeltime`` must
    stay free for the majority of users who have no LangChain, and reeltime's
    runtime must stay standard-library-only.
    """
    import importlib

    try:
        module = importlib.import_module("langchain_core.callbacks.base")
    except ImportError as exc:
        raise TapeError(
            "recording a LangChain run needs langchain-core: "
            "pip install 'langchain-core>={}'".format(_render(MINIMUM))
        ) from exc
    return module.BaseCallbackHandler


def handler_class() -> type:
    """The :class:`BaseCallbackHandler` subclass, built once per process."""
    global _handler_class
    if _handler_class is not None:
        return _handler_class

    base = _base_handler()

    class TapeCallbackHandler(base):  # type: ignore[misc,valid-type]
        """Records LangChain nodes onto the active tape.

        Two class attributes carry more weight than anything in the body.

        ``run_inline`` -- without it LangChain dispatches a synchronous handler
        to a thread-pool executor on every async path. The executor thread's
        stack contains no user frame at all, so every call site would be lost;
        and the handlers are gathered concurrently, so the order events were
        written in would stop being the order they happened in. Both were
        measured, not assumed.

        ``raise_error`` -- LangChain catches every exception a handler raises
        and logs it at warning level. A ``TapeMiss`` swallowed like that would
        let a replay sail past a call it could not match, which is precisely
        the silent divergence this tool exists to prevent.
        """

        run_inline = True
        raise_error = True

        def __init__(self, tracker: Optional[Tracker] = None) -> None:
            super().__init__()
            self.tracker = tracker if tracker is not None else Tracker()

        # -- chains, and everything shaped like one ----------------------

        def on_chain_start(self, serialized, inputs, *, run_id,
                           parent_run_id=None, tags=None, metadata=None, **kwargs):
            self.tracker.start(TYPE_CHAIN, serialized, inputs, run_id,
                               parent_run_id, tags, kwargs)

        def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
            self.tracker.end(run_id, outputs)

        def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs):
            self.tracker.error(run_id, error)

        def on_tool_start(self, serialized, input_str, *, run_id,
                          parent_run_id=None, tags=None, metadata=None,
                          inputs=None, **kwargs):
            self.tracker.start(TYPE_TOOL, serialized,
                               inputs if inputs is not None else input_str,
                               run_id, parent_run_id, tags, kwargs)

        def on_tool_end(self, output, *, run_id, parent_run_id=None, **kwargs):
            self.tracker.end(run_id, output)

        def on_tool_error(self, error, *, run_id, parent_run_id=None, **kwargs):
            self.tracker.error(run_id, error)

        def on_retriever_start(self, serialized, query, *, run_id,
                               parent_run_id=None, tags=None, metadata=None,
                               **kwargs):
            self.tracker.start(TYPE_RETRIEVER, serialized, {"query": query},
                               run_id, parent_run_id, tags, kwargs)

        def on_retriever_end(self, documents, *, run_id, parent_run_id=None, **kwargs):
            self.tracker.end(run_id, documents)

        def on_retriever_error(self, error, *, run_id, parent_run_id=None, **kwargs):
            self.tracker.error(run_id, error)

        # -- model nodes: tracked for depth, never written ---------------

        def on_llm_start(self, serialized, prompts, *, run_id,
                         parent_run_id=None, tags=None, metadata=None, **kwargs):
            self.tracker.start(TYPE_LLM, serialized, prompts, run_id,
                               parent_run_id, tags, kwargs)

        def on_chat_model_start(self, serialized, messages, *, run_id,
                                parent_run_id=None, tags=None, metadata=None,
                                **kwargs):
            self.tracker.start(TYPE_LLM, serialized, messages, run_id,
                               parent_run_id, tags, kwargs)

        def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs):
            self.tracker.end(run_id, response)

        def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs):
            self.tracker.error(run_id, error)

        # -- agent steps -------------------------------------------------

        def on_agent_action(self, action, *, run_id, parent_run_id=None, **kwargs):
            self.tracker.point(TYPE_AGENT_ACTION,
                               str(getattr(action, "tool", "action")),
                               run_id, parent_run_id,
                               {"tool": getattr(action, "tool", None),
                                "tool_input": getattr(action, "tool_input", None)})

        def on_agent_finish(self, finish, *, run_id, parent_run_id=None, **kwargs):
            self.tracker.point(TYPE_AGENT_FINISH, "finish", run_id, parent_run_id,
                               getattr(finish, "return_values", None))

    _handler_class = TapeCallbackHandler
    return _handler_class


def handler(allow_unsupported: bool = False) -> Any:
    """A callback handler to hand to one chain::

        chain.invoke(x, config={"callbacks": [tape.langchain.handler()]})

    :func:`install` is the usual way in; this is for scoping the recording to
    part of a program, or for a framework that wants the handler itself.
    """
    check_version(allow_unsupported)
    return handler_class()()


# -- installing process-wide ---------------------------------------------

_registered = False


def _register() -> None:
    """Tell langchain-core to attach a handler to every run tree it configures.

    ``register_configure_hook`` appends to a module-level list with no way to
    take an entry back off it, so this happens exactly once per process and
    :func:`uninstall` clears the environment variable that arms it instead.
    """
    global _registered
    if _registered:
        return
    import importlib
    from contextvars import ContextVar

    context = importlib.import_module("langchain_core.tracers.context")
    context.register_configure_hook(
        ContextVar("reeltime_langchain", default=None), True,
        handler_class(), ENV_VAR,
    )
    _registered = True


def install(allow_unsupported: bool = False) -> None:
    """Record every LangChain chain, agent and tool in this process.

    ::

        import reeltime as tape

        tape.langchain.install()
        agent.invoke({"messages": [...]})

    Safe to call more than once. ``tape run --langchain`` calls this for you at
    interpreter startup, which is the zero-edit way in.
    """
    if os.environ.get(ENV_VAR) == "force":
        allow_unsupported = True
    check_version(allow_unsupported)
    _register()
    os.environ.setdefault(ENV_VAR, "1")


def uninstall() -> None:
    """Stop attaching handlers to new run trees."""
    os.environ.pop(ENV_VAR, None)


def installed() -> bool:
    return bool(os.environ.get(ENV_VAR))


@contextlib.contextmanager
def recording(allow_unsupported: bool = False) -> Iterator[None]:
    """Record LangChain for the duration of a block."""
    was = os.environ.get(ENV_VAR)
    install(allow_unsupported)
    try:
        yield
    finally:
        if was is None:
            uninstall()
        else:  # pragma: no cover - nested installs
            os.environ[ENV_VAR] = was


# -- reading chain events back out ---------------------------------------


def is_chain(event: Any) -> bool:
    return getattr(event, "kind", None) == "chain"


def structure(event: Any) -> Optional[Tuple[str, int, int]]:
    """``(path, depth, children)`` -- what a diff compares when it asks whether
    the shape of the run changed, rather than what flowed through it."""
    if not is_chain(event):
        return None
    req, res = event.req or {}, event.res or {}
    children = res.get("children")
    return (str(req.get("path", "")), int(req.get("depth", 0) or 0),
            int(children) if isinstance(children, int) else 0)
