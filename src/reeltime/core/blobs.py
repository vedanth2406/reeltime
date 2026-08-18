"""Content-addressed storage for large payloads.

Any field bigger than the threshold (8KB by default) is written to
``.tape/blobs/<sha256>`` and replaced in the event with ``"blob:<sha256>"``.

Two reasons this earns its complexity. First, traces stay greppable: a
40-message conversation history would otherwise put a 200KB line in the JSONL
and make the file unreadable in a terminal. Second, agent context is enormously
repetitive -- the same system prompt and the same growing message array are
resent every turn -- so content addressing dedupes a run down to a fraction of
its nominal size.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..errors import TapeError

PREFIX = "blob:"
DEFAULT_THRESHOLD = 8192


def canonical_bytes(value: Any) -> bytes:
    """Deterministic JSON encoding -- the input to every content hash."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def is_ref(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX) and len(value) == len(PREFIX) + 64


class BlobStore:
    """Reads and writes ``.tape/blobs``. Safe to share across threads."""

    def __init__(self, root: os.PathLike, threshold: int = DEFAULT_THRESHOLD) -> None:
        self.root = Path(root)
        self.threshold = threshold
        self._known: set = set()
        self._lock = threading.Lock()
        self.bytes_written = 0
        self.bytes_deduped = 0

    # -- writing ---------------------------------------------------------

    def put(self, value: Any) -> str:
        """Store ``value`` unconditionally and return its ``blob:`` reference."""
        return self.put_bytes(canonical_bytes(value))

    def put_bytes(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self.path_for(digest)
        with self._lock:
            seen = digest in self._known
            if seen or path.exists():
                self._known.add(digest)
                self.bytes_deduped += len(data)
                return PREFIX + digest
            self._known.add(digest)
        self.root.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a crashed run must never leave a half-written blob
        # under a hash that claims to describe its contents.
        tmp = path.with_name(path.name + ".{}.tmp".format(os.getpid()))
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
        with self._lock:
            self.bytes_written += len(data)
        return PREFIX + digest

    def maybe_put(self, value: Any) -> Any:
        """Externalise ``value`` if it is over the threshold, else pass it through."""
        if value is None or isinstance(value, bool) or isinstance(value, (int, float)):
            return value
        if isinstance(value, str) and len(value) < self.threshold:
            return value
        data = canonical_bytes(value)
        if len(data) <= self.threshold:
            return value
        return self.put_bytes(data)

    def externalize(self, payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Apply :meth:`maybe_put` to each top-level field of an event payload.

        Field-level rather than whole-event: an event whose ``messages`` are a
        blob but whose ``model`` and ``temperature`` are still inline stays
        useful to ``grep``, which is most of why the format is JSONL.
        """
        if payload is None:
            return None
        return {key: self.maybe_put(value) for key, value in payload.items()}

    # -- reading ---------------------------------------------------------

    def path_for(self, digest: str) -> Path:
        return self.root / digest

    def get(self, ref: str) -> Any:
        digest = ref[len(PREFIX):] if ref.startswith(PREFIX) else ref
        path = self.path_for(digest)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise TapeError(
                "blob {} is missing from {} -- the trace was moved without its "
                "blobs directory".format(digest[:12], self.root)
            )
        return json.loads(data.decode("utf-8"))

    def resolve(self, value: Any) -> Any:
        """Recursively replace ``blob:`` references with their contents."""
        if is_ref(value):
            return self.resolve(self.get(value))
        if isinstance(value, dict):
            return {key: self.resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        return value

    def refs_in(self, value: Any) -> List[str]:
        found: List[str] = []
        stack = [value]
        while stack:
            item = stack.pop()
            if is_ref(item):
                found.append(item)
            elif isinstance(item, dict):
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        return found
