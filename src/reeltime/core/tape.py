"""The tape itself: process-wide state, install, and uninstall.

One tape per process, in one of four modes -- ``RECORD``, ``REPLAY``,
``FORK``, ``OFF``. M1 implements ``RECORD``; the others are reserved so that
mode-aware code written now stays correct.

State lives in *both* a module global and a :class:`~contextvars.ContextVar`.
The ContextVar is the scoped view (a block can suspend recording without
disturbing anything else), but a plain thread started by the agent begins with
a fresh context and would see the default -- and since the ambient patches are
process-wide, a ContextVar-only tape would leave worker threads patched but
silently unrecorded. The global is the process-wide fallback that prevents
that.
"""

from __future__ import annotations

import atexit
import enum
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ..errors import StopReplay, TapeConfigError, TapeStateError
from . import fmt, ids, paths, spans
from .blobs import BlobStore
from .config import Config
from .decoders import decode as decode_event
from .fork import ForkEngine, check_patches, missing_credentials
from .http import HttpShim
from .matching import DEFAULT as DEFAULT_STRICTNESS
from .matching import STRICTNESSES
from .patches import AmbientPatcher
from .player import Player, ReplaySummary
from .recorder import Recorder
from .redact import DEFAULT_PATTERNS, Redactor
from .trace import Event, Header, build_header, read_trace
from .writer import TraceWriter


class Mode(str, enum.Enum):
    OFF = "off"
    RECORD = "record"
    REPLAY = "replay"
    FORK = "fork"


@dataclass
class RunSummary:
    """What a finished run amounts to. Printed by ``tape run``."""

    run_id: str
    path: Path
    events: int = 0
    dur_s: float = 0.0
    cost_usd: float = 0.0
    kinds: Dict[str, int] = field(default_factory=dict)
    tokens: Dict[str, int] = field(default_factory=dict)
    redacted: Dict[str, int] = field(default_factory=dict)

    def line(self) -> str:
        return "recorded {} event{} → {}  ({:.1f}s, {})".format(
            self.events,
            "" if self.events == 1 else "s",
            paths.display_path(self.path),
            self.dur_s,
            fmt.usd(self.cost_usd),
        )

    def redaction_line(self) -> Optional[str]:
        if not self.redacted:
            return None
        parts = ", ".join(
            "{} {}".format(count, label)
            for label, count in sorted(self.redacted.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        total = sum(self.redacted.values())
        return "redacted {} secret{} before writing ({})".format(
            total, "" if total == 1 else "s", parts
        )


class Tape:
    """A live recording or replay session."""

    def __init__(
        self,
        mode: Mode,
        run_id: str,
        config: Config,
        header: Optional[Header],
        engine: Any,
        patcher: Optional[AmbientPatcher],
        path: Optional[Path],
        http: Optional[HttpShim] = None,
    ) -> None:
        self.mode = mode
        self.run_id = run_id
        self.config = config
        self.header = header
        #: Recorder while recording, Player while replaying. Both answer
        #: ``.replaying``, which is all the interception layer needs to know.
        self.engine = engine
        self.patcher = patcher
        self.http = http
        self.path = path
        self.closed = False
        self.summary: Optional[Any] = None
        self.restore_excepthook: Optional[Any] = None

    @property
    def replaying(self) -> bool:
        return bool(getattr(self.engine, "replaying", False))

    @property
    def recorder(self) -> Optional[Recorder]:
        return None if self.replaying else self.engine

    @property
    def player(self) -> Optional[Player]:
        return self.engine if self.replaying else None

    # -- convenience -----------------------------------------------------

    @property
    def blobs(self) -> BlobStore:
        return self.engine.blobs

    @property
    def redactor(self) -> Redactor:
        return self.engine.redactor

    def record(
        self,
        kind: str,
        req: Optional[Dict[str, Any]] = None,
        res: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Optional[Event]:
        if self.replaying:
            raise TapeStateError("cannot record while replaying run {}".format(self.run_id))
        return self.engine.record(kind, req, res, **kwargs)

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Stop recording for the duration of the block (setup, teardown…)."""
        was = self.engine.enabled
        self.engine.enabled = False
        try:
            yield
        finally:
            self.engine.enabled = was

    # -- teardown --------------------------------------------------------

    def close(self, exit_code: Optional[int] = None) -> Any:
        if self.closed:
            return self.summary
        self.closed = True
        if self.patcher is not None:
            self.patcher.uninstall()
        intercepted = list(self.http.installed) if self.http is not None else []
        if self.http is not None:
            self.http.uninstall()

        if self.restore_excepthook is not None:
            self.restore_excepthook()
        if self.replaying:
            self.summary = self.engine.close(exit_code)
            return self.summary

        footer = self.engine.close(exit_code, intercepted=intercepted)
        self.summary = RunSummary(
            run_id=self.run_id,
            path=self.path,
            events=footer.get("events", 0),
            dur_s=footer.get("dur_s", 0.0),
            cost_usd=footer.get("cost_usd", 0.0),
            kinds=footer.get("kinds", {}),
            tokens=footer.get("tokens", {}),
            redacted=footer.get("redacted", {}),
        )
        return self.summary

    def __enter__(self) -> "Tape":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        uninstall()

    def __repr__(self) -> str:
        # A fork is both at once, so ask for whichever counter exists.
        stats = getattr(self.engine, "stats", None)
        count = stats.events if stats is not None else getattr(
            self.engine, "consumed", 0)
        return "<Tape {} mode={} events={}>".format(self.run_id, self.mode.value, count)


# -- process state -------------------------------------------------------

_scoped: ContextVar[Optional[Tape]] = ContextVar("reeltime_tape", default=None)
_global: Optional[Tape] = None
_extra_patterns: List[Tuple[str, str]] = []


def current() -> Optional[Tape]:
    """The active tape, or None."""
    return _scoped.get() or _global


def is_recording() -> bool:
    tape = current()
    return tape is not None and tape.mode is Mode.RECORD and not tape.closed


def redact(pattern: str, label: str = "custom") -> None:
    """Register an extra redaction regex.

    Applies to the running tape immediately and to every tape installed later
    in this process, so it can be called at import time before ``install()``.
    """
    _extra_patterns.append((pattern, label))
    tape = current()
    if tape is not None:
        tape.redactor.add(pattern, label)


def install(
    mode: str = "record",
    *,
    run_id: Optional[str] = None,
    tape_dir: Optional[os.PathLike] = None,
    argv: Optional[Sequence[str]] = None,
    replay: Optional[str] = None,
    strictness: str = DEFAULT_STRICTNESS,
    stop_at: Optional[int] = None,
    realtime: bool = False,
    stepper: Optional[Any] = None,
    fork_at: Optional[int] = None,
    patches: Optional[Sequence[Any]] = None,
    override: Optional[Dict[str, Any]] = None,
    **config_overrides: Any,
) -> Tape:
    """Start recording, or start replaying.

    ::

        import reeltime as tape
        tape.install()                         # record
        tape.install("replay", replay="01M09")  # replay that run

    Returns the :class:`Tape`, which is also reachable via
    :func:`reeltime.current`. Call :func:`reeltime.uninstall` to stop; an
    ``atexit`` hook does it for you at normal interpreter shutdown.
    """
    global _global

    if _global is not None and not _global.closed:
        raise TapeStateError(
            "a tape is already installed for run {} -- call uninstall() first".format(
                _global.run_id
            )
        )

    try:
        resolved_mode = Mode(str(mode).lower())
    except ValueError:
        raise TapeConfigError(
            "unknown mode {!r}; expected one of {}".format(
                mode, ", ".join(m.value for m in Mode)
            )
        )

    config = Config.resolve(tape_dir=tape_dir, **config_overrides)

    if resolved_mode is Mode.REPLAY:
        return _install_player(
            config, replay, strictness=strictness, stop_at=stop_at,
            realtime=realtime, stepper=stepper,
        )

    if resolved_mode is Mode.FORK:
        return _install_fork(
            config, replay, fork_at=fork_at, patches=patches, override=override,
            strictness=strictness, run_id=run_id, argv=argv,
        )

    paths.ensure_tape_dir(config.tape_dir)

    run = run_id or ids.new_run_id()
    trace_file = paths.trace_path(config.tape_dir, run)

    redactor = Redactor(DEFAULT_PATTERNS)
    for pattern in config.redact:
        redactor.add(pattern, "config")
    for pattern, label in _extra_patterns:
        redactor.add(pattern, label)

    from .. import __version__

    header = build_header(
        run,
        mode=resolved_mode.value,
        argv=argv,
        cwd=os.getcwd(),
        redactor=redactor,
        env_patterns=config.env_capture,
        collect_git_info=config.collect_git,
        tool_version=__version__,
    )

    writer = TraceWriter(trace_file).open()
    writer.write_header(header)

    recorder = Recorder(
        writer,
        BlobStore(paths.blobs_dir(config.tape_dir), config.blob_threshold),
        redactor,
        record_library_ambient=config.record_library_ambient,
        enrich=decode_event if config.decode else None,
    )

    patcher = None
    if resolved_mode is Mode.RECORD and config.patch:
        patcher = AmbientPatcher(recorder, config.patch).install()

    http = None
    if resolved_mode is Mode.RECORD and config.http:
        http = HttpShim(recorder).install()

    tape = Tape(resolved_mode, run, config, header, recorder, patcher, trace_file, http)
    _global = tape
    spans.reset()
    atexit.register(_close_at_exit)
    return tape


def _install_player(
    config: Config,
    replay: Optional[str],
    *,
    strictness: str,
    stop_at: Optional[int],
    realtime: bool,
    stepper: Optional[Any],
) -> Tape:
    """Load a trace and hand its events to a re-running agent."""
    global _global

    if not replay:
        raise TapeConfigError("replay mode needs a run to replay: install('replay', replay=...)")
    if strictness not in STRICTNESSES:
        raise TapeConfigError(
            "unknown strictness {!r}; expected one of {}".format(
                strictness, ", ".join(STRICTNESSES))
        )

    run, trace = _load_parent(config, replay)
    redactor = _build_redactor(config)

    player = Player(
        trace,
        BlobStore(paths.blobs_dir(config.tape_dir), config.blob_threshold),
        redactor,
        strictness=strictness,
        stop_at=stop_at,
        realtime=realtime,
        record_library_ambient=config.record_library_ambient,
        stepper=stepper,
    )

    patcher = AmbientPatcher(player, config.patch).install() if config.patch else None
    http = HttpShim(player).install() if config.http else None

    tape = Tape(Mode.REPLAY, run, config, trace.header, player, patcher, trace.path, http)
    tape.restore_excepthook = _install_stop_excepthook()
    _global = tape
    spans.reset()
    atexit.register(_close_at_exit)
    return tape


def _load_parent(config: Config, replay: Optional[str]):
    """Resolve a run id (or prefix, or `last`) and read its trace."""
    available = paths.list_run_ids(config.tape_dir)
    if not available:
        raise TapeConfigError(
            "no runs in {}".format(paths.display_path(config.tape_dir))
        )
    if not replay or str(replay).lower() in ("last", "latest", "-"):
        run = available[-1]
    else:
        run = ids.resolve_prefix(replay, available)
    return run, read_trace(paths.trace_path(config.tape_dir, run))


def _build_redactor(config: Config) -> Redactor:
    redactor = Redactor(DEFAULT_PATTERNS)
    for pattern in config.redact:
        redactor.add(pattern, "config")
    for pattern, label in _extra_patterns:
        redactor.add(pattern, label)
    return redactor


def _install_fork(
    config: Config,
    replay: Optional[str],
    *,
    fork_at: Optional[int],
    patches: Optional[Sequence[Any]],
    override: Optional[Dict[str, Any]],
    strictness: str,
    run_id: Optional[str],
    argv: Optional[Sequence[str]],
) -> Tape:
    """Replay a prefix, then run live, recording the whole thing as a new run."""
    global _global

    if not replay:
        raise TapeConfigError(
            "fork mode needs a run to fork: install('fork', replay=..., fork_at=N)")
    if fork_at is None:
        raise TapeConfigError("fork mode needs --at N")

    parent, trace = _load_parent(config, replay)
    if fork_at < 0:
        raise TapeConfigError("--at must be zero or more, not {}".format(fork_at))
    if fork_at > len(trace.events):
        raise TapeConfigError(
            "--at {} is past the end of run {}, which has {} events".format(
                fork_at, parent[:14], len(trace.events))
        )

    parsed = list(patches or [])
    check_patches(parsed, trace, fork_at)

    # Fail before replaying anything: burning the prefix and then dying for
    # want of a key is the worst possible order to discover that in.
    missing = missing_credentials(trace, fork_at)
    if missing:
        raise TapeConfigError(
            "this fork runs live from event {}, and those calls need:\n{}\n"
            "Set them and try again.".format(
                fork_at,
                "\n".join("  {}  (for {})".format(var, host) for host, var in missing))
        )

    redactor = _build_redactor(config)
    blobs = BlobStore(paths.blobs_dir(config.tape_dir), config.blob_threshold)

    child = run_id or ids.new_run_id()
    trace_file = paths.trace_path(config.tape_dir, child)
    from .. import __version__

    header = build_header(
        child, mode=Mode.FORK.value, argv=argv or trace.header.argv,
        cwd=os.getcwd(), redactor=redactor, env_patterns=config.env_capture,
        collect_git_info=config.collect_git, tool_version=__version__,
    )
    header.forked_from = parent
    header.fork_at = fork_at

    writer = TraceWriter(trace_file).open()
    writer.write_header(header)

    recorder = Recorder(
        writer, blobs, redactor,
        record_library_ambient=config.record_library_ambient,
        enrich=decode_event if config.decode else None,
    )
    player = Player(
        trace, blobs, redactor, strictness=strictness,
        record_library_ambient=config.record_library_ambient,
    )
    engine = ForkEngine(player, recorder, fork_at, parsed, override)

    patcher = AmbientPatcher(engine, config.patch).install() if config.patch else None
    http = HttpShim(engine).install() if config.http else None

    tape = Tape(Mode.FORK, child, config, header, engine, patcher, trace_file, http)
    _global = tape
    spans.reset()
    atexit.register(_close_at_exit)
    return tape


def _install_stop_excepthook():
    """Make ``--to N`` end the run quietly instead of with a traceback.

    ``StopReplay`` is raised through the agent's own stack, which is the only
    way to stop a program mid-flight. Letting it print a traceback would make a
    deliberate stop look like a crash.
    """
    original = sys.excepthook

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, StopReplay):
            sys.stderr.write("\n⏹  {}\n".format(exc))
            return
        original(exc_type, exc, tb)

    sys.excepthook = hook

    def restore():
        if sys.excepthook is hook:
            sys.excepthook = original

    return restore


def uninstall(exit_code: Optional[int] = None) -> Optional[RunSummary]:
    """Stop recording, restore every patch, and finish the trace."""
    global _global

    tape = _global
    _global = None
    _scoped.set(None)
    if tape is None:
        return None
    summary = tape.close(exit_code)
    try:
        atexit.unregister(_close_at_exit)
    except Exception:  # pragma: no cover - defensive
        pass
    if os.environ.get("REELTIME_ANNOUNCE") and summary is not None:
        sys.stderr.write("\n✓ {}\n".format(summary.line()))
        for note in summary.notes() if hasattr(summary, "notes") else ():
            sys.stderr.write("  {}\n".format(note))
    return summary


def _close_at_exit() -> None:
    """Finish the trace on normal interpreter shutdown.

    Runs after an unhandled exception too, since the interpreter still exits
    through the normal path. A SIGKILL skips it -- which is precisely why every
    event is flushed as it is written rather than at close.
    """
    if _global is not None and not _global.closed:
        uninstall()


@contextmanager
def session(mode: str = "record", **kwargs: Any) -> Iterator[Tape]:
    """Record a block of code.

    ::

        with tape.session() as run:
            agent.go()
        print(run.summary.line())
    """
    tape = install(mode, **kwargs)
    try:
        yield tape
    finally:
        uninstall()


def _reset_for_tests() -> None:
    """Drop all process state. Used by the test suite's autouse fixture."""
    global _global
    if _global is not None and not _global.closed:
        _global.close()
    _global = None
    _scoped.set(None)
    _extra_patterns.clear()
    spans.reset()
    from .http import common as http_common

    http_common._reset_owned_for_tests()
