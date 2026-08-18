"""Turning arbitrary Python values into JSON-safe ones.

Everything that crosses a recorded boundary -- tool arguments, tool results,
random draws, request bodies -- is user data of unknown shape, and a recorder
that raises ``TypeError`` on an unusual object is a recorder people uninstall.
So conversion never fails: anything unrecognised degrades to a tagged repr.

Round-trippable types use a ``__tag__`` envelope (``{"__bytes__": ...}``) so
replay can reconstruct them and so a human reading the JSONL can still tell
what they are looking at.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as _dt
import decimal
import enum
import math
import pathlib
import uuid as _uuid
from typing import Any, Dict, List, Mapping, Set

MAX_DEPTH = 16
MAX_REPR = 2000


def _type_name(value: Any) -> str:
    cls = type(value)
    module = getattr(cls, "__module__", "")
    name = getattr(cls, "__qualname__", cls.__name__)
    return "{}.{}".format(module, name) if module and module != "builtins" else name


def _opaque(value: Any) -> Dict[str, Any]:
    try:
        text = repr(value)
    except Exception:  # pragma: no cover - pathological __repr__
        text = "<unreprable {}>".format(_type_name(value))
    if len(text) > MAX_REPR:
        text = text[:MAX_REPR] + "…"
    return {"__repr__": text, "__type__": _type_name(value)}


def to_jsonable(value: Any, _depth: int = 0, _seen: Set[int] = None) -> Any:
    """Best-effort conversion of ``value`` to something ``json.dumps`` accepts."""
    if value is None or isinstance(value, (bool, int, str)):
        return value

    if isinstance(value, float):
        # NaN/Infinity are not JSON. Tag them rather than emit invalid output:
        # traces must stay parseable by tools that are not this one.
        if math.isnan(value):
            return {"__float__": "nan"}
        if math.isinf(value):
            return {"__float__": "inf" if value > 0 else "-inf"}
        return value

    if _depth > MAX_DEPTH:
        return _opaque(value)

    _seen = set() if _seen is None else _seen
    marker = id(value)
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        if marker in _seen:
            return {"__cycle__": _type_name(value)}
        _seen = _seen | {marker}

    if isinstance(value, bytes):
        return {
            "__bytes__": base64.b64encode(value).decode("ascii"),
            "len": len(value),
        }
    if isinstance(value, bytearray):
        return to_jsonable(bytes(value), _depth, _seen)

    if isinstance(value, enum.Enum):
        return to_jsonable(value.value, _depth + 1, _seen)

    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, _dt.timedelta):
        return {"__timedelta__": value.total_seconds()}
    if isinstance(value, decimal.Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, _uuid.UUID):
        return {"__uuid__": str(value)}
    if isinstance(value, pathlib.PurePath):
        return str(value)

    if isinstance(value, Mapping):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            out[key if isinstance(key, str) else str(key)] = to_jsonable(
                item, _depth + 1, _seen
            )
        return out

    if isinstance(value, (list, tuple)):
        return [to_jsonable(item, _depth + 1, _seen) for item in value]

    if isinstance(value, (set, frozenset)):
        items: List[Any] = [to_jsonable(item, _depth + 1, _seen) for item in value]
        try:
            items.sort(key=repr)
        except Exception:  # pragma: no cover
            pass
        return {"__set__": items}

    ndarray = _as_ndarray(value)
    if ndarray is not None:
        return ndarray

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(dataclasses.asdict(value), _depth + 1, _seen)

    # Pydantic v2, then v1, then anything offering a dict-ish view. These cover
    # essentially every LLM SDK response object.
    for attr in ("model_dump", "dict", "to_dict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                return to_jsonable(method(), _depth + 1, _seen)
            except Exception:
                break

    return _opaque(value)


def _as_ndarray(value: Any) -> Any:
    """Serialise a numpy array without importing numpy ourselves."""
    if type(value).__module__.split(".")[0] != "numpy":
        return None
    tolist = getattr(value, "tolist", None)
    if not callable(tolist):
        return None
    shape = getattr(value, "shape", None)
    dtype = getattr(value, "dtype", None)
    try:
        data = tolist()
    except Exception:  # pragma: no cover
        return _opaque(value)
    if shape is None:
        # numpy scalar (np.float64 and friends): unwrap to a plain Python value.
        return to_jsonable(data)
    return {
        "__ndarray__": to_jsonable(data),
        "dtype": str(dtype),
        "shape": list(shape),
    }
