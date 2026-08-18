"""Secret redaction.

Traces are meant to be pasted into GitHub issues, so this is mandatory rather
than optional (design principle 3). Redaction runs on every event *before* it
reaches the writer or the blob store, so a secret never touches disk in any
form.

Two layers:

* header stripping -- ``Authorization`` and friends are removed wholesale
* payload scanning -- a regex set for key-shaped strings, replaced with
  ``<redacted:label>``

The regexes are deliberately anchored on real, known key formats. A greedy
"anything that looks like entropy" rule would mangle base64 images and tool
output, and a redactor users disable is worse than a narrow one.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, Iterable, List, Mapping, Pattern, Sequence, Tuple

#: Headers dropped entirely, compared case-insensitively.
SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "x-goog-api-key",
        "cookie",
        "set-cookie",
    }
)

REDACTED_HEADER = "<redacted>"

#: (label, pattern) pairs. Order matters: more specific formats come first so
#: that e.g. an Anthropic key is labelled ``sk-ant`` rather than ``sk``.
DEFAULT_PATTERNS: Sequence[Tuple[str, str]] = (
    ("sk-ant", r"sk-ant-[A-Za-z0-9\-_]{16,}"),
    ("sk", r"sk-(?:proj-|svcacct-)?[A-Za-z0-9\-_]{20,}"),
    ("gh", r"gh[pousr]_[A-Za-z0-9]{30,}"),
    ("aws", r"(?<![A-Z0-9])(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}(?![A-Z0-9])"),
    ("gcp", r"AIza[0-9A-Za-z\-_]{35}"),
    ("slack", r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    ("hf", r"hf_[A-Za-z0-9]{30,}"),
    ("jwt", r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    ("bearer", r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{20,}={0,2}"),
    (
        "private-key",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----",
    ),
)

#: Substrings that make an environment-variable *name* secret-shaped.
SECRET_NAME_HINTS = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
    "SIGNATURE",
    "PRIVATE",
    "SESSION",
    "COOKIE",
)


def looks_secret(name: str) -> bool:
    """True if an identifier's *name* alone marks it as a secret."""
    upper = name.upper()
    return any(hint in upper for hint in SECRET_NAME_HINTS)


class Redactor:
    """Applies the pattern set to arbitrary JSON-shaped values.

    Thread-safe: a single redactor is shared by every thread in the recorded
    process, and hit counts are aggregated under a lock so the end-of-run
    warning is accurate.
    """

    def __init__(self, patterns: Iterable[Tuple[str, str]] = DEFAULT_PATTERNS) -> None:
        self._patterns: List[Tuple[str, Pattern[str]]] = []
        self._lock = threading.Lock()
        self._hits: Dict[str, int] = {}
        for label, pattern in patterns:
            self.add(pattern, label)

    def add(self, pattern: str, label: str = "custom") -> None:
        """Register another pattern. Exposed publicly as ``tape.redact()``."""
        self._patterns.append((label, re.compile(pattern)))

    @property
    def hits(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._hits)

    @property
    def total_hits(self) -> int:
        with self._lock:
            return sum(self._hits.values())

    def summary(self) -> str:
        """``"2 sk, 1 gh"`` -- the tail of the end-of-run warning."""
        hits = self.hits
        if not hits:
            return ""
        parts = sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))
        return ", ".join("{} {}".format(count, label) for label, count in parts)

    def _count(self, label: str, n: int = 1) -> None:
        with self._lock:
            self._hits[label] = self._hits.get(label, 0) + n

    def scrub_text(self, text: str) -> str:
        for label, pattern in self._patterns:
            replacement = "<redacted:{}>".format(label)
            text, n = pattern.subn(replacement, text)
            if n:
                self._count(label, n)
        return text

    def scrub(self, value: Any) -> Any:
        """Deep-scrub a JSON-shaped value, returning a scrubbed copy."""
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, Mapping):
            out: Dict[str, Any] = {}
            for key, item in value.items():
                name = str(key)
                if looks_secret(name) and isinstance(item, str) and item:
                    self._count("named")
                    out[name] = "<redacted:named>"
                else:
                    out[name] = self.scrub(item)
            return out
        if isinstance(value, (list, tuple)):
            return [self.scrub(item) for item in value]
        return value

    def scrub_headers(self, headers: Mapping[str, Any]) -> Dict[str, str]:
        """Drop sensitive headers outright; scrub the values of the rest."""
        out: Dict[str, str] = {}
        for key, item in headers.items():
            name = str(key)
            if name.lower() in SENSITIVE_HEADERS:
                self._count("header")
                out[name] = REDACTED_HEADER
            else:
                out[name] = self.scrub_text(str(item))
        return out
