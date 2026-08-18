"""Run configuration, resolved from three layers.

Explicit keyword arguments beat environment variables, which beat the nearest
``.tapeconfig``. Anything left over falls back to the defaults here.

``.tapeconfig`` is JSON and lives at the root of a project::

    {
      "blob_threshold": 16384,
      "patch": ["random", "uuid", "time", "datetime"],
      "redact": ["ACME-[A-Z0-9]{24}"]
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from ..errors import TapeConfigError
from . import paths
from .blobs import DEFAULT_THRESHOLD
from .patches import GROUPS
from .trace import DEFAULT_ENV_CAPTURE

_KNOWN_KEYS = {
    "tape_dir",
    "blob_threshold",
    "patch",
    "record_stdlib_ambient",
    "redact",
    "env_capture",
    "collect_git",
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_tuple(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        if value.strip().lower() in ("none", "off", ""):
            return ()
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return tuple(str(item) for item in value)


@dataclass
class Config:
    """Everything :func:`reeltime.install` needs to know."""

    tape_dir: Path
    blob_threshold: int = DEFAULT_THRESHOLD
    #: Which ambient groups to patch: random, uuid, time, datetime, numpy.
    patch: Tuple[str, ...] = GROUPS
    #: Record ambient reads made by the standard library itself. Noisy.
    record_stdlib_ambient: bool = False
    #: Extra redaction regexes, on top of the built-in key formats.
    redact: Tuple[str, ...] = ()
    env_capture: Tuple[str, ...] = DEFAULT_ENV_CAPTURE
    collect_git: bool = True

    @classmethod
    def resolve(cls, **overrides: Any) -> "Config":
        values: Dict[str, Any] = {}
        values.update(_from_file(paths.find_config()))
        values.update(_from_env())
        values.update({k: v for k, v in overrides.items() if v is not None})

        unknown = set(values) - _KNOWN_KEYS
        if unknown:
            raise TapeConfigError(
                "unknown configuration key(s): {}".format(", ".join(sorted(unknown)))
            )

        tape_dir = values.get("tape_dir")
        tape_dir = Path(tape_dir) if tape_dir else paths.find_tape_dir()
        return cls(
            tape_dir=tape_dir,
            blob_threshold=int(values.get("blob_threshold", DEFAULT_THRESHOLD)),
            patch=_as_tuple(values.get("patch", GROUPS)),
            record_stdlib_ambient=_as_bool(values.get("record_stdlib_ambient", False)),
            redact=_as_tuple(values.get("redact", ())),
            env_capture=_as_tuple(values.get("env_capture", DEFAULT_ENV_CAPTURE)),
            collect_git=_as_bool(values.get("collect_git", True)),
        )


def _from_file(path: Optional[Path]) -> Dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TapeConfigError("could not read {}: {}".format(path, exc))
    if not isinstance(data, dict):
        raise TapeConfigError("{} must contain a JSON object".format(path))
    return data


def _from_env() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    mapping = {
        "REELTIME_BLOB_THRESHOLD": "blob_threshold",
        "REELTIME_PATCH": "patch",
        "REELTIME_STDLIB_AMBIENT": "record_stdlib_ambient",
        "REELTIME_REDACT": "redact",
        "REELTIME_COLLECT_GIT": "collect_git",
    }
    for env_name, key in mapping.items():
        if env_name in os.environ:
            out[key] = os.environ[env_name]
    return out
