"""Ambient nondeterminism: randomness, uuids, and the clock.

These are the three boundaries an agent crosses without ever making a call you
can see. They are patched process-wide at :func:`reeltime.install` and restored
exactly at uninstall.

Two design decisions worth knowing about:

**Only the user's own code is recorded.** ``asyncio`` reads ``time.monotonic()``
on every loop iteration, ``logging`` timestamps every record, and httpx reads
``perf_counter()`` twice per request. Those reads belong to the runtime and to
installed libraries, not to the agent: recording them would bury the trace in
thousands of uninteresting events and replaying them would hand a replayed
clock to the event loop's own timeouts. The same filter applies on replay, so
those reads stay live in both directions -- consistent, and never a spurious
miss. Set ``record_library_ambient=True`` if you really want them.

**``datetime`` patching is opt-in, and off by default.** ``datetime`` is a C
type whose methods cannot be assigned to, so the only way to see
``datetime.now()`` is to swap the module attribute for a subclass. Two problems
follow, one solvable and one not:

* Solvable: ``isinstance(x, datetime.datetime)`` would go false for any
  datetime produced by an unpatched path, because arithmetic on datetimes
  returns the base C type. The metaclass overrides ``__instancecheck__`` to
  answer for the real class, so this is invisible again.
* Not solvable: pydantic v2 dispatches on type *identity* -- ``obj is
  datetime.datetime`` -- so once the module attribute is ours, the **real**
  datetime class becomes unrecognisable to it. Any library that did ``from
  datetime import datetime`` before ``install()`` then fails to build its
  models, which is not a subtle degradation: the Anthropic SDK stops working
  entirely. Install order does not help, because the annotation and the
  dispatch table can hold different classes either way.

So ``"datetime"`` is not in the default patch set. Turn it on with
``patch=("random", "uuid", "time", "datetime")`` if your agent puts wall-clock
time into prompts and your stack is not pydantic v2. ``time.time()`` is patched
either way and covers most clock reads.
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

#: Patched unless configured otherwise. ``datetime`` is deliberately absent --
#: see the module docstring.
GROUPS = ("random", "uuid", "time", "numpy")

#: Every group that can be requested, including the opt-in ones.
ALL_GROUPS = ("random", "uuid", "time", "datetime", "numpy")


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


def _restore_value(event: Any) -> Any:
    """The recorded value, rebuilt into the type the caller expects.

    Only numpy needs work: everything else round-trips through JSON as itself.
    """
    value = (event.res or {}).get("value")
    if isinstance(value, dict) and "__ndarray__" in value:
        numpy = sys.modules.get("numpy")
        if numpy is not None:
            return numpy.array(value["__ndarray__"], dtype=value.get("dtype"))
    return value


class AmbientPatcher:
    """Installs and removes the ambient patches for one run.

    The same wrappers serve recording and replay. On replay each one asks the
    player for the recorded value instead of producing a fresh one, so the
    agent sees the identical random draw, uuid, and clock reading it saw
    originally.
    """

    def __init__(
        self,
        engine: Any,
        groups: Sequence[str] = GROUPS,
    ) -> None:
        self.engine = engine
        self.groups = tuple(groups)
        self._saved: List[Tuple[Any, str, Any]] = []
        self.installed = False
        #: Groups that were requested but not applicable, e.g. numpy absent.
        self.skipped: List[str] = []

    def _cross(self, kind, req, produce, restore=_restore_value, capture=None):
        """Cross one ambient boundary, recording or replaying as appropriate.

        Returning None from ``consume`` means this read is not ours to serve --
        a library reading its own clock -- so it runs live in both directions,
        which is exactly what recording did with it.
        """
        engine = self.engine
        if engine.replaying:
            event = engine.consume(kind, req, ambient=True)
            if event is not None:
                return restore(event)
            return produce()
        value = produce()
        engine.record(kind, req, (capture or (lambda v: {"value": v}))(value),
                      ambient=True)
        return value

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
        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            return self._cross(
                "rand",
                {"name": name, "args": list(args), "kwargs": kwargs},
                lambda: original(*args, **kwargs),
            )

        return wrapper

    def _wrap_shuffle(self, original):
        @functools.wraps(original)
        def wrapper(seq, *args, **kwargs):
            before = list(seq)

            def produce():
                original(seq, *args, **kwargs)
                return None

            def restore(event):
                # Replay the permutation rather than the elements: applying the
                # recorded index mapping reproduces the shuffle on whatever
                # list this call was handed.
                order = (event.res or {}).get("perm") or []
                if len(order) == len(before):
                    seq[:] = [before[i] for i in order]
                return None

            return self._cross(
                "rand", {"name": "shuffle", "n": len(before)}, produce, restore,
                capture=lambda _value: {"perm": _permutation(before, seq)},
            )

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
        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            return self._cross(
                "uuid",
                {"name": name},
                lambda: original(*args, **kwargs),
                restore=lambda event: _uuid.UUID((event.res or {}).get("value")),
                capture=lambda value: {"value": str(value)},
            )

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
        @functools.wraps(original)
        def wrapper():
            return self._cross("time", {"name": "time.{}".format(name)}, original)

        return wrapper

    # -- datetime --------------------------------------------------------

    def _patch_datetime(self) -> None:
        cross = self._cross
        real = _datetime.datetime

        def restore(event):
            return real.fromisoformat((event.res or {}).get("value"))

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
            def __get_pydantic_core_schema__(cls, source_type, handler):
                """Tell pydantic v2 to treat this exactly like a datetime.

                Only repairs annotations that resolve to *this* class. An
                annotation still holding the real class cannot be repaired from
                here, which is why the whole group is opt-in. Importing
                pydantic_core inside the method is safe: nothing calls this
                unless pydantic is already running.
                """
                from pydantic_core import core_schema

                return core_schema.datetime_schema()

            @classmethod
            def now(cls, tz=None):
                return cross(
                    "time", {"name": "datetime.now", "tz": str(tz) if tz else None},
                    lambda: real.now(tz), restore,
                    capture=lambda value: {"value": value.isoformat()},
                )

            @classmethod
            def utcnow(cls):
                return cross(
                    "time", {"name": "datetime.utcnow"}, real.utcnow, restore,
                    capture=lambda value: {"value": value.isoformat()},
                )

            @classmethod
            def today(cls):
                return cross(
                    "time", {"name": "datetime.today"}, real.today, restore,
                    capture=lambda value: {"value": value.isoformat()},
                )

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
        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            return self._cross(
                "rand",
                {"name": "numpy.random.{}".format(name),
                 "args": list(args), "kwargs": kwargs},
                lambda: original(*args, **kwargs),
            )

        return wrapper
