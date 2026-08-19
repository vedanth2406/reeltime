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
        # SigV4's session credential, sent by anything using temporary AWS
        # credentials -- an assumed role, an instance profile, SSO, a Lambda.
        # It is an opaque blob with no recognisable prefix, so the payload scan
        # below cannot catch it and dropping it by name is the only thing that
        # does. The `Authorization` header beside it is already covered, and the
        # `AKIA`/`ASIA` key id inside it is matched by pattern as well -- this
        # is the one piece of a signed AWS request that was reaching disk.
        "x-amz-security-token",
        "x-amzn-authorization",
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
    # The same session credential again, carried in a query string instead of a
    # header. A presigned URL puts it there, and a URL is recorded as text, so
    # the header rule above never sees it.
    ("aws-session", r"(?i)X-Amz-Security-Token=[A-Za-z0-9%\-._~+/]{20,}"),
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

#: Substrings that make an environment *variable* name secret-shaped. Broad on
#: purpose: an env var called TOKEN is a credential essentially always, and the
#: cost of dropping a harmless one from the header snapshot is nil.
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

#: Words that make a *payload field* name a credential on their own.
SECRET_FIELD_WORDS = frozenset(
    {
        "secret",
        "password",
        "passwd",
        "passphrase",
        "credential",
        "credentials",
        "apikey",
        "privatekey",
        "accesskey",
        "secretkey",
    }
)

#: Word pairs that make a payload field name a credential together.
SECRET_FIELD_PAIRS = frozenset(
    {
        ("api", "key"),
        ("access", "key"),
        ("secret", "key"),
        ("private", "key"),
        ("client", "secret"),
        ("auth", "token"),
        ("access", "token"),
        ("refresh", "token"),
        ("id", "token"),
        ("session", "token"),
        ("bearer", "token"),
    }
)

_WORD_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def looks_secret(name: str) -> bool:
    """True if an environment variable's *name* alone marks it as a secret."""
    upper = name.upper()
    return any(hint in upper for hint in SECRET_NAME_HINTS)


def looks_secret_field(name: str) -> bool:
    """True if a payload field's *name* alone marks it as a credential.

    Deliberately stricter than :func:`looks_secret`. Payload fields are tool
    arguments and request bodies, where ``key``, ``token``, and ``auth`` are
    ordinary words -- ``sort(key=...)``, ``max_tokens``, ``auth_mode``.
    Redacting those would quietly destroy the data the trace exists to show,
    so a bare ambiguous word is not enough: it has to be qualified
    (``api_key``, ``apiKey``, ``client_secret``) or unambiguous on its own
    (``password``). Key-shaped *values* are still caught by the regex set
    wherever they appear, so nothing is riding on this alone.
    """
    words = [w.lower() for w in _WORD_SPLIT.split(name) if w]
    if not words:
        return False
    if any(word in SECRET_FIELD_WORDS for word in words):
        return True
    if "".join(words) in SECRET_FIELD_WORDS:
        return True
    return any(pair in SECRET_FIELD_PAIRS for pair in zip(words, words[1:]))


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
                if looks_secret_field(name) and isinstance(item, str) and item:
                    self._count("named")
                    out[name] = "<redacted:named>"
                else:
                    out[name] = self.scrub(item)
            return out
        if isinstance(value, (list, tuple)):
            return [self.scrub(item) for item in value]
        return value

    def scrub_header_pairs(self, pairs: Sequence[Tuple[str, str]]) -> List[List[str]]:
        """Scrub headers recorded as ordered pairs.

        HTTP headers repeat (``set-cookie``) and their order is part of the
        message, so they are recorded as pairs rather than a mapping. That also
        means the generic value scan is not enough on its own: it would only
        catch a *key-shaped* credential, leaving an opaque session token or a
        basic-auth blob sitting in the trace. Sensitive headers are dropped by
        name here, wholesale, before anything else looks at them.
        """
        out: List[List[str]] = []
        for key, value in pairs:
            name = str(key)
            if name.lower() in SENSITIVE_HEADERS:
                self._count("header")
                out.append([name, REDACTED_HEADER])
            else:
                out.append([name, self.scrub_text(str(value))])
        return out

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
