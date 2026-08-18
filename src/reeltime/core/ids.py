"""Run identifiers.

Run ids are ULIDs: 48 bits of millisecond timestamp then 80 bits of entropy,
rendered in Crockford base32. That buys three properties that matter for a
directory full of traces -- they sort lexicographically by time, they never
collide across parallel runs, and they are case-insensitive and typo-resistant
at the terminal.

26 characters is a lot to type, so every command that takes a run id accepts
any unambiguous prefix; see :func:`resolve_prefix`.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from . import _originals
from ..errors import TapeError

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {c: i for i, c in enumerate(_CROCKFORD)}
# Crockford treats these as visually equivalent, so accept them on input.
_DECODE.update({"O": 0, "o": 0, "I": 1, "i": 1, "L": 1, "l": 1})
for _c, _i in list(_DECODE.items()):
    _DECODE.setdefault(_c.lower(), _i)

RUN_ID_LEN = 26


def _encode(value: int, length: int) -> str:
    out = [""] * length
    for pos in range(length - 1, -1, -1):
        out[pos] = _CROCKFORD[value & 0x1F]
        value >>= 5
    return "".join(out)


def new_run_id(now_ms: Optional[int] = None) -> str:
    """Generate a fresh ULID run id."""
    if now_ms is None:
        now_ms = int(_originals.time() * 1000)
    entropy = int.from_bytes(_originals.urandom(10), "big")
    return _encode(now_ms, 10) + _encode(entropy, 16)


def is_run_id(value: str) -> bool:
    if len(value) != RUN_ID_LEN:
        return False
    return all(ch in _DECODE for ch in value)


def timestamp_ms(run_id: str) -> int:
    """Recover the creation time embedded in a run id."""
    value = 0
    for ch in run_id[:10]:
        try:
            value = (value << 5) | _DECODE[ch]
        except KeyError:
            raise TapeError("not a valid run id: {!r}".format(run_id))
    return value


def normalize(value: str) -> str:
    return value.strip().upper()


def resolve_prefix(prefix: str, candidates: Iterable[str]) -> str:
    """Expand a run-id prefix to the single run it names.

    Raises :class:`TapeError` when nothing matches or when the prefix is
    ambiguous -- guessing between two runs is exactly the kind of quiet
    wrongness this tool exists to eliminate.
    """
    wanted = normalize(prefix)
    pool: List[str] = list(candidates)
    exact = [c for c in pool if normalize(c) == wanted]
    if exact:
        return exact[0]
    hits = [c for c in pool if normalize(c).startswith(wanted)]
    if not hits:
        raise TapeError("no run matches {!r}".format(prefix))
    if len(hits) > 1:
        listing = ", ".join(sorted(hits)[:5])
        raise TapeError(
            "{!r} is ambiguous between {} runs: {}".format(prefix, len(hits), listing)
        )
    return hits[0]
