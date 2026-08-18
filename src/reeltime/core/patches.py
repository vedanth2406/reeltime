"""Ambient nondeterminism: randomness, uuids, and the clock.

These are the three boundaries an agent crosses without ever making a call you
can see. They are patched process-wide at :func:`reeltime.install` and restored
exactly at uninstall.

Two design decisions worth knowing about:

**Standard-library callers are ignored by default.** ``asyncio`` reads
``time.monotonic()`` on every loop iteration and ``logging`` timestamps every
record. Those reads belong to the runtime, not to the agent; recording them
would bury the trace in thousands of uninteresting events and replaying them
would hand a replayed clock to the event loop's own timeouts. Set
``record_stdlib_ambient=True`` if you really want them.

**``datetime.datetime`` is replaced by a subclass with a custom metaclass.**
``datetime`` is a C type whose methods cannot be assigned to, so the module
attribute is swapped for a subclass. That alone would break ``isinstance(x,
datetime.datetime)`` for any datetime produced by an unpatched path (arithmetic
on datetimes returns the base type), so the metaclass overrides
``__instancecheck__`` to answer for the real class.
"""

from __future__ import annotations

import datetime as _datetime
import functools
import random as _random
import sys
import uuid as _uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..errors import TapeStateError
from .recorder import Recorder

#: Global ``random`` functions worth recording. All are bound methods of the
#: module's shared ``Random`` instance.
RANDOM_FUNCTIONS = (
    "random",
    "randint",
    "randrange",
    "uniform",
    "choice",
    "choices",
    "sample",
    "getrandbits",
    "randbytes",
    "triangular",
    "betavariate",
    "expovariate",
    "gammavariate",
    "gauss",
    "lognormvariate",
    "normalvariate",
    "paretovariate",
    "vonmisesvariate",
    "weibullvariate",
)

TIME_FUNCTIONS = (
    "time",
    "time_ns",
    "monotonic",
    "monotonic_ns",
    "perf_counter",
    "perf_counter_ns",
)

NUMPY_FUNCTIONS = (
    "rand",
    "randn",
    "random",
    "random_sample",
    "randint",
    "random_integers",
    "choice",
    "permutation",
    "normal",
    "uniform",
    "poisson",
    "binomial",
    "exponential",
    "beta",
    "gamma",
    "standard_normal",
    "bytes",
)

GROUPS = ("random", "uuid", "time", "datetime", "numpy")


def _permutation(before: Sequence[Any], after: Sequence[Any]) -> List[int]:
    """Index mapping that turns ``before`` into ``after``.

    Matched by identity rather than equality so that a list of equal-comparing
    or unhashable elements still yields the true permutation. Replay applies
    the indices to whatever list it is given, so the elements themselves never
    need to be recorded.
    """
    buckets: Dict[int, List[int]] = {}
    for index, item in enumerate(before):
        buckets.setdefault(id(item), []).append(index)
    order: List[int] = []
    for item in after:
        slots = buckets.get(id(item))
        if not slots:  # pragma: no cover - only if the callee replaced elements
            return list(range(len(after)))
        order.append(slots.pop(0))
    return order


class AmbientPatcher:
    """Installs and removes the ambient patches for one run."""

    def __init__(
        self,
        recorder: Recorder,
        groups: Sequence[str] = GROUPS,
    ) -> None:
        self.recorder = recorder
        self.groups = tuple(groups)
        self._saved: List[Tuple[Any, str, Any]] = []
        self.installed = False
        #: Groups that were requested but not applicable, e.g. numpy absent.
        self.skipped: List[str] = []

    # -- patch bookkeeping ----------------------------------------------

    def _set(self, obj: Any, attr: str, value: Any) -> None:
        """Replace ``obj.attr``, remembering whatever was there before.

        Restoration returns the *previous* value rather than a pristine one, so
        stacking reeltime under another patching tool unwinds correctly.
        """
        self._saved.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def install(self) -> "AmbientPatcher":
        if self.installed:
            raise TapeStateError("ambient patches are already installed")
        self.installed = True
        if "random" in self.groups:
            self._patch_random()
        if "uuid" in self.groups:
            self._patch_uuid()
        if "time" in self.groups:
            self._patch_time()
        if "datetime" in self.groups:
            self._patch_datetime()
        if "numpy" in self.groups:
            self.patch_numpy()
        return self

    def uninstall(self) -> None:
        for obj, attr, original in reversed(self._saved):
            try:
                setattr(obj, attr, original)
            except Exception:  # pragma: no cover - defensive
                pass
        self._saved.clear()
        self.installed = False

    # -- random ----------------------------------------------------------

    def _patch_random(self) -> None:
        for name in RANDOM_FUNCTIONS:
            original = getattr(_random, name, None)
            if original is None:  # randbytes is 3.9+, randbytes/others vary
                continue
            self._set(_random, name, self._wrap_random(name, original))
        shuffle = getattr(_random, "shuffle", None)
        if shuffle is not None:
            self._set(_random, "shuffle", self._wrap_shuffle(shuffle))

    def _wrap_random(self, name: str, original):
        recorder = self.recorder

        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            value = original(*args, **kwargs)
            recorder.record(
                "rand",
                {"name": name, "args": list(args), "kwargs": kwargs},
                {"value": value},
                ambient=True,
            )
            return value

        return wrapper

    def _wrap_shuffle(self, original):
        recorder = self.recorder

        @functools.wraps(original)
        def wrapper(seq, *args, **kwargs):
            before = list(seq)
            result = original(seq, *args, **kwargs)
            recorder.record(
                "rand",
                {"name": "shuffle", "n": len(before)},
                {"perm": _permutation(before, seq)},
                ambient=True,
            )
            return result

        return wrapper

    # -- uuid ------------------------------------------------------------

    def _patch_uuid(self) -> None:
        # uuid3/uuid5 are pure functions of their inputs -- nothing to record.
        for name in ("uuid1", "uuid4"):
            original = getattr(_uuid, name, None)
            if original is None:  # pragma: no cover
                continue
            self._set(_uuid, name, self._wrap_uuid(name, original))

    def _wrap_uuid(self, name: str, original):
        recorder = self.recorder

        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            value = original(*args, **kwargs)
            recorder.record(
                "uuid",
                {"name": name},
                {"value": str(value)},
                ambient=True,
            )
            return value

        return wrapper

    # -- time ------------------------------------------------------------

    def _patch_time(self) -> None:
        import time as _time

        for name in TIME_FUNCTIONS:
            original = getattr(_time, name, None)
            if original is None:  # pragma: no cover
                continue
            self._set(_time, name, self._wrap_time(name, original))

    def _wrap_time(self, name: str, original):
        recorder = self.recorder

        @functools.wraps(original)
        def wrapper():
            value = original()
            recorder.record(
                "time",
                {"name": "time.{}".format(name)},
                {"value": value},
                ambient=True,
            )
            return value

        return wrapper

    # -- datetime --------------------------------------------------------

    def _patch_datetime(self) -> None:
        recorder = self.recorder
        real = _datetime.datetime

        class _RecordingMeta(type):
            # Keeps isinstance/issubclass answering about the real class, so
            # user code and libraries cannot tell the difference.
            def __instancecheck__(cls, instance):
                return isinstance(instance, real)

            def __subclasscheck__(cls, subclass):
                return issubclass(subclass, real)

        class RecordingDateTime(real, metaclass=_RecordingMeta):
            """``datetime.datetime`` that reports its wall-clock reads."""

            @classmethod
            def now(cls, tz=None):
                value = real.now(tz)
                recorder.record(
                    "time",
                    {"name": "datetime.now", "tz": str(tz) if tz else None},
                    {"value": value.isoformat()},
                    ambient=True,
                )
                return value

            @classmethod
            def utcnow(cls):
                value = real.utcnow()
                recorder.record(
                    "time",
                    {"name": "datetime.utcnow"},
                    {"value": value.isoformat()},
                    ambient=True,
                )
                return value

            @classmethod
            def today(cls):
                value = real.today()
                recorder.record(
                    "time",
                    {"name": "datetime.today"},
                    {"value": value.isoformat()},
                    ambient=True,
                )
                return value

        RecordingDateTime.__name__ = "datetime"
        RecordingDateTime.__qualname__ = "datetime"
        self._set(_datetime, "datetime", RecordingDateTime)

    # -- numpy -----------------------------------------------------------

    def patch_numpy(self) -> bool:
        """Patch the legacy ``numpy.random`` global functions.

        Only if numpy is already imported -- importing it here would add a
        second of startup to every run that does not use it. Call this again
        after your own import if you need it, or import numpy before
        ``install()``.

        ``numpy.random.default_rng()`` generators are not patched; they are
        explicit objects a caller can seed.
        """
        if "numpy" not in sys.modules:
            if "numpy" in self.groups:
                self.skipped.append("numpy")
            return False
        module = sys.modules["numpy"].random
        for name in NUMPY_FUNCTIONS:
            original = getattr(module, name, None)
            if original is None:
                continue
            self._set(module, name, self._wrap_numpy(name, original))
        return True

    def _wrap_numpy(self, name: str, original):
        recorder = self.recorder

        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            value = original(*args, **kwargs)
            recorder.record(
                "rand",
                {"name": "numpy.random.{}".format(name), "args": list(args), "kwargs": kwargs},
                {"value": value},
                ambient=True,
            )
            return value

        return wrapper
