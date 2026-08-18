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
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ..errors import TapeConfigError, TapeStateError
from . import fmt, ids, paths, spans
from .blobs import BlobStore
from .config import Config
from .decoders import decode as decode_event
from .http import HttpShim
from .patches import AmbientPatcher
from .recorder import Recorder
from .redact import DEFAULT_PATTERNS, Redactor
from .trace import Event, Header, build_header
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
    """A live recording (or, from M3, replay) session."""

    def __init__(
        self,
        mode: Mode,
        run_id: str,
        config: Config,
        header: Header,
        recorder: Recorder,
        patcher: Optional[AmbientPatcher],
        path: Path,
        http: Optional[HttpShim] = None,
    ) -> None:
        self.mode = mode
        self.run_id = run_id
        self.config = config
        self.header = header
        self.recorder = recorder
        self.patcher = patcher
        self.http = http
        self.path = path
        self.closed = False
        self.summary: Optional[RunSummary] = None

    # -- convenience -----------------------------------------------------

    @property
    def blobs(self) -> BlobStore:
        return self.recorder.blobs

    @property
    def redactor(self) -> Redactor:
        return self.recorder.redactor

    def record(
        self,
        kind: str,
        req: Optional[Dict[str, Any]] = None,
        res: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Optional[Event]:
        return self.recorder.record(kind, req, res, **kwargs)

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Stop recording for the duration of the block (setup, teardown…)."""
        was = self.recorder.enabled
        self.recorder.enabled = False
        try:
            yield
        finally:
            self.recorder.enabled = was

    # -- teardown --------------------------------------------------------

    def close(self, exit_code: Optional[int] = None) -> RunSummary:
        if self.closed:
            return self.summary  # type: ignore[return-value]
        self.closed = True
        if self.patcher is not None:
            self.patcher.uninstall()
        intercepted = list(self.http.installed) if self.http is not None else []
        if self.http is not None:
            self.http.uninstall()
        footer = self.recorder.close(exit_code, intercepted=intercepted)
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
        return "<Tape {} mode={} events={}>".format(
            self.run_id, self.mode.value, self.recorder.stats.events
        )


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
    **config_overrides: Any,
) -> Tape:
    """Start recording. Patches the ambient nondeterminism sources.

    ::

        import reeltime as tape
        tape.install()

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
    if resolved_mode in (Mode.REPLAY, Mode.FORK):
        raise TapeConfigError(
            "mode {!r} is not implemented yet -- replay lands in milestone 3".format(
                resolved_mode.value
            )
        )

    config = Config.resolve(tape_dir=tape_dir, **config_overrides)
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
