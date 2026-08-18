"""Pristine references to everything reeltime patches.

Imported before any patch is installed, so reeltime's own bookkeeping (event
timestamps, run ids) keeps using the real clock and the real entropy source
even while the process-wide patches are active. Patch installation captures
its *own* copies for restoration, so uninstall correctly hands control back to
whatever was in place beforehand -- these are strictly for internal use.
"""

from __future__ import annotations

import datetime as _datetime
import os as _os
import time as _time
import uuid as _uuid

time = _time.time
monotonic = _time.monotonic
perf_counter = _time.perf_counter

datetime_cls = _datetime.datetime
timezone = _datetime.timezone

uuid4 = _uuid.uuid4

urandom = _os.urandom


def utc_now() -> _datetime.datetime:
    """Timezone-aware "now", immune to a patched ``datetime.datetime``."""
    return datetime_cls.fromtimestamp(time(), timezone.utc)


def utc_now_iso() -> str:
    """RFC3339 UTC timestamp with a trailing ``Z``, second-ish precision."""
    return utc_now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
