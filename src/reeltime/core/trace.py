"""The trace format: a JSONL header line, then one line per event.

::

    {"v":1,"run_id":"01M0…","started":"…","argv":[…],"packages":{…}, …}
    {"i":0,"kind":"time","site":"agent.py:12","span":"root", …}
    {"i":1,"kind":"llm","site":"agent.py:88","span":"root/plan", …}
    {"end":true,"events":47,"dur_s":43.2,"cost_usd":0.31,"exit":0}

Append-only and flushed per line, so a run that dies mid-flight still leaves a
readable trace of everything up to the crash -- which is the run you most want
to inspect. Readers tolerate a torn final line for the same reason.

The footer is optional: its absence is exactly the signal that the process did
not exit cleanly.

Recording the git SHA and package versions in the header is not decoration.
The most common cause of a failed replay is that the code or the SDK changed
since recording, and a tool that can say so beats one that reports a confusing
mismatch deep in the run.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from . import _originals
from .redact import Redactor, looks_secret

SCHEMA_VERSION = 1

#: Every boundary reeltime knows how to cross.
KINDS = ("llm", "http", "tool", "mcp", "rand", "time", "uuid")

#: Packages whose version can change replay behaviour. Recorded when present.
TRACKED_PACKAGES = (
    "openai",
    "anthropic",
    "httpx",
    "httpcore",
    "requests",
    "aiohttp",
    "langchain",
    "langchain-core",
    "langgraph",
    "llama-index-core",
    "litellm",
    "instructor",
    "mcp",
    "google-genai",
    "pydantic",
    "numpy",
    "reeltime",
)

#: Environment variables worth snapshotting, as fnmatch patterns against the
#: upper-cased name. Anything whose name looks secret is dropped afterwards.
DEFAULT_ENV_CAPTURE = (
    "*MODEL*",
    "*BASE_URL*",
    "*API_BASE*",
    "*ENDPOINT*",
    "*TEMPERATURE*",
    "ENV",
    "ENVIRONMENT",
    "*_ENV",
    "PYTHONHASHSEED",
    "PYTHONPATH",
    "TZ",
    "TAPE_*",
    "REELTIME_*",
    "OPENAI_*",
    "ANTHROPIC_*",
    "LANGCHAIN_*",
    "LANGSMITH_*",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)


@dataclass
class Event:
    """One crossing of one boundary."""

    i: int
    kind: str
    site: str
    span: str = "root"
    t_rel: float = 0.0
    dur_ms: float = 0.0
    req: Dict[str, Any] = field(default_factory=dict)
    res: Optional[Dict[str, Any]] = None
    #: ``<file>::<qualname>`` -- the tier-3 fallback when line numbers shift.
    qual: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> Optional[str]:
        """Tool/function name for kinds that have one."""
        value = self.req.get("name")
        return value if isinstance(value, str) else None

    @property
    def signature(self) -> Tuple[str, str, str]:
        """Alignment key for ``tape diff`` (M6)."""
        return (self.kind, self.site, self.name or "")

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "i": self.i,
            "kind": self.kind,
            "site": self.site,
        }
        if self.qual:
            out["qual"] = self.qual
        out["span"] = self.span
        out["t_rel"] = self.t_rel
        out["dur_ms"] = self.dur_ms
        out["req"] = self.req
        if self.res is not None:
            out["res"] = self.res
        if self.meta:
            out["meta"] = self.meta
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        return cls(
            i=data["i"],
            kind=data["kind"],
            site=data.get("site", "<unknown>"),
            span=data.get("span", "root"),
            t_rel=data.get("t_rel", 0.0),
            dur_ms=data.get("dur_ms", 0.0),
            req=data.get("req") or {},
            res=data.get("res"),
            qual=data.get("qual"),
            meta=data.get("meta") or {},
        )


@dataclass
class Header:
    """First line of every trace."""

    run_id: str
    started: str
    argv: List[str]
    cwd: str
    python: str
    v: int = SCHEMA_VERSION
    mode: str = "record"
    packages: Dict[str, str] = field(default_factory=dict)
    env_snapshot: Dict[str, str] = field(default_factory=dict)
    git: Optional[Dict[str, Any]] = None
    platform: str = ""
    pid: int = 0
    tool: Dict[str, str] = field(default_factory=dict)
    #: Set by ``tape fork`` (M5) so the run tree is reconstructable.
    forked_from: Optional[str] = None
    fork_at: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "v": self.v,
            "run_id": self.run_id,
            "started": self.started,
            "mode": self.mode,
            "argv": self.argv,
            "cwd": self.cwd,
            "python": self.python,
            "platform": self.platform,
            "pid": self.pid,
            "packages": self.packages,
            "env_snapshot": self.env_snapshot,
            "git": self.git,
            "tool": self.tool,
        }
        if self.forked_from:
            out["forked_from"] = self.forked_from
            out["fork_at"] = self.fork_at
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Header":
        return cls(
            run_id=data["run_id"],
            started=data.get("started", ""),
            argv=data.get("argv") or [],
            cwd=data.get("cwd", ""),
            python=data.get("python", ""),
            v=data.get("v", SCHEMA_VERSION),
            mode=data.get("mode", "record"),
            packages=data.get("packages") or {},
            env_snapshot=data.get("env_snapshot") or {},
            git=data.get("git"),
            platform=data.get("platform", ""),
            pid=data.get("pid", 0),
            tool=data.get("tool") or {},
            forked_from=data.get("forked_from"),
            fork_at=data.get("fork_at"),
        )


@dataclass
class Trace:
    """A parsed trace file."""

    header: Header
    events: List[Event]
    footer: Optional[Dict[str, Any]] = None
    path: Optional[Path] = None
    #: True when the last line was torn -- i.e. the process died mid-write.
    truncated: bool = False

    @property
    def run_id(self) -> str:
        return self.header.run_id

    @property
    def complete(self) -> bool:
        return self.footer is not None

    def __len__(self) -> int:
        return len(self.events)

    def __getitem__(self, index: int) -> Event:
        return self.events[index]

    def by_kind(self, kind: str) -> List[Event]:
        return [e for e in self.events if e.kind == kind]


# -- header collection ---------------------------------------------------


def collect_packages(names: Sequence[str] = TRACKED_PACKAGES) -> Dict[str, str]:
    """Installed versions of the packages that can change replay behaviour."""
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - Python < 3.8
        return {}
    out: Dict[str, str] = {}
    for name in names:
        try:
            out[name] = metadata.version(name)
        except Exception:
            continue
    return out


def collect_env(
    patterns: Sequence[str] = DEFAULT_ENV_CAPTURE,
    environ: Optional[Dict[str, str]] = None,
    redactor: Optional[Redactor] = None,
) -> Dict[str, str]:
    """Snapshot the configuration-shaped subset of the environment.

    Allowlist rather than "everything": the whole environment would carry
    secrets into a file whose entire point is being shareable. Names that look
    like credentials are dropped even when a pattern matches them, and the
    surviving values still go through redaction.
    """
    source = os.environ if environ is None else environ
    out: Dict[str, str] = {}
    for name, value in source.items():
        upper = name.upper()
        if not any(fnmatch(upper, pattern) for pattern in patterns):
            continue
        if looks_secret(upper):
            continue
        text = str(value)
        if redactor is not None:
            text = redactor.scrub_text(text)
        out[name] = text
    return dict(sorted(out.items()))


def collect_git(cwd: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """``{"sha": …, "branch": …, "dirty": bool}``, or None outside a repo."""

    def run(*args: str) -> Optional[str]:
        try:
            proc = subprocess.run(
                ("git",) + args,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", "replace").strip()

    if run("rev-parse", "--is-inside-work-tree") != "true":
        return None
    sha = run("rev-parse", "HEAD")
    if sha is None:
        # A repo with no commits yet: still worth recording that we are in one.
        return {"sha": None, "branch": run("branch", "--show-current"), "dirty": True}
    status = run("status", "--porcelain", "--untracked-files=no")
    return {
        "sha": sha,
        "branch": run("branch", "--show-current") or None,
        "dirty": bool(status),
    }


def build_header(
    run_id: str,
    *,
    mode: str = "record",
    argv: Optional[Sequence[str]] = None,
    cwd: Optional[str] = None,
    redactor: Optional[Redactor] = None,
    env_patterns: Sequence[str] = DEFAULT_ENV_CAPTURE,
    collect_git_info: bool = True,
    tool_version: str = "",
) -> Header:
    working_dir = cwd or os.getcwd()
    return Header(
        run_id=run_id,
        started=_originals.utc_now_iso(),
        mode=mode,
        argv=list(argv if argv is not None else sys.argv),
        cwd=working_dir,
        python=platform.python_version(),
        platform="{} {}".format(platform.system(), platform.machine()),
        pid=os.getpid(),
        packages=collect_packages(),
        env_snapshot=collect_env(env_patterns, redactor=redactor),
        git=collect_git(working_dir) if collect_git_info else None,
        tool={"name": "reeltime", "version": tool_version},
    )


# -- reading -------------------------------------------------------------


def dumps(obj: Any) -> str:
    """Compact, strict JSON -- one trace line."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def iter_lines(path: os.PathLike) -> Iterator[Tuple[int, Dict[str, Any]]]:
    """Yield ``(line_number, parsed)`` skipping a torn final line."""
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle):
            line = line.strip()
            if not line:
                continue
            try:
                yield number, json.loads(line)
            except json.JSONDecodeError:
                # Only ever legitimate for the last line of a killed run.
                continue


def read_trace(path: os.PathLike) -> Trace:
    """Parse a trace file into a :class:`Trace`."""
    path = Path(path)
    header: Optional[Header] = None
    events: List[Event] = []
    footer: Optional[Dict[str, Any]] = None
    parsed_lines = 0

    for _, obj in iter_lines(path):
        parsed_lines += 1
        if header is None:
            header = Header.from_dict(obj)
            continue
        if obj.get("end"):
            footer = obj
            continue
        events.append(Event.from_dict(obj))

    if header is None:
        from ..errors import TapeError

        raise TapeError("{} is not a trace file (no header line)".format(path))

    # Concurrent recorders may write lines slightly out of index order; the
    # index is authoritative, so present events sorted by it.
    events.sort(key=lambda event: event.i)

    with open(path, "r", encoding="utf-8") as handle:
        raw_lines = sum(1 for line in handle if line.strip())
    return Trace(
        header=header,
        events=events,
        footer=footer,
        path=path,
        truncated=raw_lines != parsed_lines,
    )
