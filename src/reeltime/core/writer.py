"""Append-only JSONL writing.

Flushed after every line. Buffering a trace would mean losing the tail of any
run that crashes -- and the run that crashes is the run being debugged.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from .trace import Event, Header, dumps


class TraceWriter:
    """Serialises writes to one trace file. Safe to share across threads."""

    def __init__(self, path: os.PathLike) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._handle = None
        self._closed = False
        self.lines_written = 0

    def open(self) -> "TraceWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a", encoding="utf-8")
        return self

    @property
    def closed(self) -> bool:
        return self._closed

    def _write(self, obj: Dict[str, Any]) -> None:
        if self._handle is None:
            self.open()
        line = dumps(obj)
        with self._lock:
            if self._closed:
                return
            self._handle.write(line + "\n")
            self._handle.flush()
            self.lines_written += 1

    def write_header(self, header: Header) -> None:
        self._write(header.to_dict())

    def write_event(self, event: Event) -> None:
        self._write(event.to_dict())

    def write_footer(self, footer: Dict[str, Any]) -> None:
        self._write(dict(footer, end=True))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._handle is not None:
                try:
                    self._handle.flush()
                finally:
                    self._handle.close()
                    self._handle = None
