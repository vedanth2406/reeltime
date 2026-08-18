"""The ``--patch`` expression grammar.

A patch changes one field of the event at the fork point, so that a fork tests
exactly one variable::

    llm.model=claude-sonnet-4-5
    llm.system+="Ask before destructive actions."
    tool.read_file.result="<empty file>"

Grammar, deliberately small -- anything it cannot express is what ``--edit`` is
for::

    patch  := kind ("." name)? "." field op value
    kind   := llm | tool | http
    op     := "="   replace
            | "+="  append to a string, add to a number
            | "~="  regex substitution, written /pattern/replacement/
    value  := JSON when it parses as JSON, otherwise a bare string

``name`` selects which boundary the patch applies to when the kind alone is
ambiguous -- for tools it is the tool's name.

Supported fields, by kind:

======  ============================  ====================================
kind    field                         effect
======  ============================  ====================================
llm     model                         swap the model on the outgoing request
llm     system                        the system prompt, wherever the
                                      provider keeps it
llm     temperature, top_p,           request parameters
        max_tokens, seed
llm     response                      substitute the completion; no live
                                      call is made
tool    result                        substitute the return value; the tool
                                      body does not run
http    url                           rewrite the request URL
http    body                          replace the request body (JSON)
======  ============================  ====================================

Fields that substitute a *result* (``llm.response``, ``tool.result``) stop the
boundary from executing at all. Every other field rewrites the request on its
way out, and the call still happens live.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..errors import TapeConfigError

KINDS = ("llm", "tool", "http", "mcp")

#: field -> whether it substitutes a result instead of rewriting a request
REQUEST_FIELDS = {
    "llm": ("model", "system", "temperature", "top_p", "max_tokens", "seed"),
    "tool": ("args",),
    "http": ("url", "body"),
}
RESULT_FIELDS = {
    "llm": ("response",),
    "tool": ("result",),
    "http": ("body_response",),
    # An MCP call is a tool call over a wire, so it patches like one. There is
    # deliberately no request field: rewriting an outgoing body is an HTTP-shaped
    # operation, and `mcp.read_file.args=` would have to be either implemented
    # or silently ignored -- so it is rejected instead.
    "mcp": ("result",),
}

OPERATORS = ("+=", "~=", "=")

_EXPR = re.compile(r"^(?P<target>[A-Za-z0-9_.\-]+?)\s*(?P<op>\+=|~=|=)\s*(?P<value>.*)$",
                   re.DOTALL)


@dataclass
class Patch:
    """One parsed ``--patch`` expression."""

    kind: str
    field: str
    op: str
    value: Any
    name: Optional[str] = None
    #: The literal text, kept for error messages and for the trace header.
    source: str = ""

    @property
    def substitutes_result(self) -> bool:
        return self.field in RESULT_FIELDS.get(self.kind, ())

    def matches(self, kind: str, name: Optional[str]) -> bool:
        if kind != self.kind:
            # An llm event is an http event a decoder recognised; a patch
            # written against either should find it.
            if not (self.kind == "http" and kind == "llm"):
                return False
        if self.name is None:
            return True
        return name == self.name

    def describe(self) -> str:
        return self.source or "{}{}{}{}".format(
            self.kind, "." + self.name if self.name else "", "." + self.field,
            self.op + json.dumps(self.value))

    # -- application -----------------------------------------------------

    def apply(self, current: Any) -> Any:
        """The new value for the field, given what is there now."""
        if self.op == "=":
            return self.value
        if self.op == "+=":
            return self._append(current)
        return self._substitute(current)

    def _append(self, current: Any) -> Any:
        if current is None:
            return self.value
        if isinstance(current, str) and isinstance(self.value, str):
            # A sentence appended to a prompt wants a space, not a jam.
            joiner = "" if current.endswith(("\n", " ")) else " "
            return current + joiner + self.value
        if isinstance(current, (int, float)) and isinstance(self.value, (int, float)):
            return current + self.value
        if isinstance(current, list):
            return current + (self.value if isinstance(self.value, list) else [self.value])
        raise TapeConfigError(
            "cannot append {} to {} in patch {!r}".format(
                type(self.value).__name__, type(current).__name__, self.describe())
        )

    def _substitute(self, current: Any) -> Any:
        if not isinstance(current, str):
            raise TapeConfigError(
                "~= needs text to work on, but {} is {} in patch {!r}".format(
                    self.field, type(current).__name__, self.describe())
            )
        pattern, replacement = self.value
        try:
            return re.sub(pattern, replacement, current)
        except re.error as exc:
            raise TapeConfigError(
                "bad regex in patch {!r}: {}".format(self.describe(), exc))


def _parse_value(text: str, op: str, source: str) -> Any:
    text = text.strip()
    if op == "~=":
        return _parse_substitution(text, source)
    if not text:
        raise TapeConfigError(
            "patch {!r} has no value; write {}\"...\" or {}<json>".format(
                source, op, op)
        )
    try:
        return json.loads(text)
    except ValueError:
        # A bare word is the common case: llm.model=claude-sonnet-4-5
        return text


def _parse_substitution(text: str, source: str) -> Tuple[str, str]:
    """``/pattern/replacement/`` -- a leading ``s`` is allowed."""
    body = text[1:] if text[:1] == "s" else text
    if len(body) < 3 or body[0] != body[-1]:
        raise TapeConfigError(
            "patch {!r}: ~= expects /pattern/replacement/, e.g. "
            "'llm.system~=/careful/very careful/'".format(source)
        )
    delimiter = body[0]
    parts = body[1:-1].split(delimiter)
    if len(parts) != 2:
        raise TapeConfigError(
            "patch {!r}: ~= expects exactly one {} between pattern and "
            "replacement".format(source, delimiter)
        )
    return (parts[0], parts[1])


def parse(expression: str) -> Patch:
    """Parse one ``--patch`` expression, or explain why it cannot be parsed."""
    text = (expression or "").strip()
    if not text:
        raise TapeConfigError("empty --patch expression")

    match = _EXPR.match(text)
    if not match:
        raise TapeConfigError(
            "cannot read patch {!r}. Expected <kind>[.<name>].<field><op><value>, "
            "e.g. 'llm.model=gpt-4o' or 'tool.read_file.result=\"\"'".format(text)
        )

    target, op = match.group("target"), match.group("op")
    parts = target.split(".")
    if len(parts) < 2:
        raise TapeConfigError(
            "patch {!r} is missing a field: write {}.<field>{}...".format(
                text, target, op)
        )

    kind, field = parts[0], parts[-1]
    name = ".".join(parts[1:-1]) or None
    if kind not in KINDS:
        raise TapeConfigError(
            "patch {!r}: unknown kind {!r}; expected one of {}".format(
                text, kind, ", ".join(KINDS))
        )

    known = tuple(REQUEST_FIELDS.get(kind, ())) + tuple(RESULT_FIELDS.get(kind, ()))
    if field not in known:
        raise TapeConfigError(
            "patch {!r}: {} has no field {!r}. Try one of: {}".format(
                text, kind, field, ", ".join(sorted(known)))
        )
    if op == "~=" and field in RESULT_FIELDS.get(kind, ()) + ("model",):
        pass  # regex on a result or a model name is unusual but legal

    return Patch(kind=kind, field=field, op=op,
                 value=_parse_value(match.group("value"), op, text),
                 name=name, source=text)


def parse_all(expressions: Sequence[str]) -> List[Patch]:
    return [parse(expression) for expression in expressions or ()]


# -- applying to a request payload ---------------------------------------


def _system_of(body: Dict[str, Any]) -> Tuple[str, Any]:
    """Where this provider keeps the system prompt, and what is there.

    Anthropic uses a top-level ``system``; OpenAI uses the first message with
    ``role: system``. Returning a locator rather than a value lets one patch
    expression work against both.
    """
    if "system" in body:
        return ("top-level", body.get("system"))
    for index, message in enumerate(body.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "system":
            return ("message:{}".format(index), message.get("content"))
    return ("absent", None)


def apply_to_body(patch: Patch, body: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a request-field patch to a decoded JSON request body."""
    body = dict(body)
    if patch.field == "system":
        where, current = _system_of(body)
        updated = patch.apply(current)
        if where == "top-level":
            body["system"] = updated
        elif where.startswith("message:"):
            index = int(where.split(":")[1])
            messages = list(body.get("messages") or [])
            messages[index] = dict(messages[index], content=updated)
            body["messages"] = messages
        else:
            # No system prompt yet: adding one is the obvious reading of
            # `llm.system+="..."` on a request that has none.
            body["messages"] = [{"role": "system", "content": updated}] + list(
                body.get("messages") or [])
        return body

    if patch.field == "body":
        return patch.apply(body) if patch.op == "=" else body

    body[patch.field] = patch.apply(body.get(patch.field))
    return body
