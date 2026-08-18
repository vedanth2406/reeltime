"""What the model actually read.

Most agent bugs are context bugs. A message got truncated, a stale turn stayed
in history, a framework quietly injected a system prompt or reordered the array
-- and none of that is visible from the outside, because all you see is a bad
answer. This module reconstructs the exact message array that crossed the wire
at a given step, and diffs the array between two steps so an injection or a
truncation is impossible to miss.

Provider shapes are normalised on the way in (:func:`from_event`, via the
decoders), so an OpenAI run and an Anthropic run render and diff identically.
Anthropic's top-level ``system`` field is hoisted to position 0: it is part of
what the model read, and the system prompt is the field people most often get
wrong.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .decoders import context_of
from .trace import Event

#: Collapse a message once it exceeds either of these.
MAX_LINES = 14
MAX_CHARS = 900
HEAD_LINES = 7
TAIL_LINES = 3

#: A message that loses this much of itself is called out as truncated rather
#: than merely changed -- silent truncation is the bug this view exists to find.
TRUNCATION_RATIO = 0.6


class Glyphs:
    """Box-drawing and marker characters, with an ASCII fallback.

    A debugger that renders as mojibake on a cp1252 console is a debugger
    someone stops using, and this is the one command whose whole value is being
    readable.
    """

    def __init__(self, unicode_ok: bool = True) -> None:
        if unicode_ok:
            self.rule, self.dot, self.ellipsis = "─", "·", "⋯"
            self.arrow, self.same, self.added = "→", "=", "+"
            self.dropped, self.changed = "−", "~"
        else:
            self.rule, self.dot, self.ellipsis = "-", "|", "..."
            self.arrow, self.same, self.added = "->", "=", "+"
            self.dropped, self.changed = "-", "~"

    @classmethod
    def detect(cls, stream: Any = None) -> "Glyphs":
        stream = stream if stream is not None else sys.stdout
        encoding = (getattr(stream, "encoding", None) or "").lower()
        return cls("utf" in encoding)


@dataclass
class ToolCall:
    name: str
    args: Any

    def render(self, limit: int = 60) -> str:
        try:
            text = json.dumps(self.args, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - exotic args
            text = str(self.args)
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        return "{}({})".format(self.name, text)


@dataclass
class Message:
    """One entry of the assembled message array."""

    index: int
    role: str
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_name: Optional[str] = None
    images: int = 0
    hoisted: bool = False

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def lines(self) -> List[str]:
        return self.text.splitlines() or ([""] if self.text else [])

    @property
    def shape(self) -> str:
        """A short description of what this message is made of."""
        parts: List[str] = []
        if self.tool_name:
            parts.append(self.tool_name)
        if self.tool_calls:
            parts.append("{} tool call{}".format(
                len(self.tool_calls), "" if len(self.tool_calls) == 1 else "s"))
        if self.images:
            parts.append("{} image{}".format(
                self.images, "" if self.images == 1 else "s"))
        if self.text or not parts:
            parts.append("{:,} chars".format(self.chars))
        if self.hoisted:
            parts.append("hoisted from `system`")
        return ", ".join(parts)

    @property
    def signature(self) -> str:
        """Alignment key: same role and same content means the same message."""
        payload = json.dumps(
            [self.role, self.text, [(c.name, c.args) for c in self.tool_calls],
             self.tool_name, self.images],
            sort_keys=True, default=str,
        )
        return "{}:{}".format(self.role, hashlib.sha256(
            payload.encode("utf-8")).hexdigest()[:16])


@dataclass
class Context:
    """Everything the model was given at one step, plus what it gave back."""

    event_index: int
    provider: str
    model: Optional[str]
    messages: List[Message]
    tools: List[str] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict)
    completion: Optional[str] = None
    tokens: Dict[str, Any] = field(default_factory=dict)
    cost_usd: Optional[float] = None
    site: str = ""
    qual: Optional[str] = None
    streamed: bool = False

    @property
    def total_chars(self) -> int:
        return sum(m.chars for m in self.messages)


# -- extraction ----------------------------------------------------------


def _blocks_of(content: Any) -> Tuple[str, List[ToolCall], int, Optional[str]]:
    """Flatten a provider content field to (text, tool calls, images, tool name)."""
    if content is None:
        return "", [], 0, None
    if isinstance(content, str):
        return content, [], 0, None
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False, default=str), [], 0, None

    texts: List[str] = []
    calls: List[ToolCall] = []
    images = 0
    tool_name: Optional[str] = None
    for block in content:
        if isinstance(block, str):
            texts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in ("text", "input_text", "output_text"):
            texts.append(str(block.get("text", "")))
        elif kind in ("image", "image_url", "input_image"):
            images += 1
        elif kind == "tool_use":
            calls.append(ToolCall(str(block.get("name", "?")), block.get("input")))
            tool_name = tool_name or str(block.get("name", ""))
        elif kind == "tool_result":
            inner, _, _, _ = _blocks_of(block.get("content"))
            texts.append(inner)
        elif "text" in block:
            texts.append(str(block["text"]))
    return "\n".join(t for t in texts if t), calls, images, tool_name


def _message_from(raw: Any, index: int) -> Message:
    if not isinstance(raw, dict):
        return Message(index=index, role="?", text=str(raw))

    role = str(raw.get("role") or raw.get("type") or "?")
    text, calls, images, tool_name = _blocks_of(raw.get("content"))

    # OpenAI puts an assistant's tool calls beside the content, not inside it.
    for call in raw.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                pass
        calls.append(ToolCall(str(function.get("name", "?")), arguments))

    if role == "tool":
        tool_name = raw.get("name") or tool_name or raw.get("tool_call_id")

    return Message(
        index=index,
        role=role,
        text=text,
        tool_calls=calls,
        tool_name=str(tool_name) if tool_name else None,
        images=images,
        hoisted=bool(raw.get("_hoisted")),
    )


def from_event(event: Event, blobs: Any = None) -> Optional[Context]:
    """Reconstruct the context for an LLM event, or None if it is not one."""
    resolved = event
    if blobs is not None:
        resolved = Event(
            i=event.i, kind=event.kind, site=event.site, span=event.span,
            t_rel=event.t_rel, dur_ms=event.dur_ms, qual=event.qual,
            req=blobs.resolve(event.req),
            res=blobs.resolve(event.res) if event.res is not None else None,
            meta=dict(event.meta),
        )

    raw = context_of(resolved)
    if raw is None:
        return None

    res = resolved.res or {}
    return Context(
        event_index=event.i,
        provider=raw.get("provider", "?"),
        model=raw.get("model") or resolved.req.get("model"),
        messages=[_message_from(m, i) for i, m in enumerate(raw.get("messages") or [])],
        tools=[t for t in raw.get("tools") or [] if t],
        params=raw.get("params") or {},
        completion=res.get("preview"),
        tokens=res.get("tokens") or {},
        cost_usd=resolved.meta.get("cost_usd"),
        site=resolved.site,
        qual=resolved.qual,
        streamed=bool(res.get("streamed")),
    )


# -- rendering -----------------------------------------------------------


def _kb(chars: int) -> str:
    if chars < 1024:
        return "{:,} chars".format(chars)
    return "{:.1f} KB".format(chars / 1024)


def _rule(label: str, width: int, glyphs: Glyphs) -> str:
    prefix = "{}{} ".format(glyphs.rule * 2, " " + label if label else "")
    return prefix + glyphs.rule * max(3, width - len(prefix))


def _body(message: Message, glyphs: Glyphs, collapse: bool, indent: str = "  ") -> List[str]:
    """Message content, collapsed in the middle when it is long.

    Head and tail rather than head alone: the end of a long message is where a
    truncation shows itself, and cutting it off would hide the thing being
    looked for.
    """
    out: List[str] = []
    for call in message.tool_calls:
        out.append("{}{} {}".format(indent, glyphs.arrow, call.render()))
    if message.images:
        out.append("{}[{} image{} not shown]".format(
            indent, message.images, "" if message.images == 1 else "s"))

    lines = message.lines
    if not lines:
        return out

    if not collapse or (len(lines) <= MAX_LINES and message.chars <= MAX_CHARS):
        out.extend(indent + line for line in lines)
        return out

    head, tail = lines[:HEAD_LINES], lines[-TAIL_LINES:]
    hidden = lines[HEAD_LINES:len(lines) - TAIL_LINES]
    elided_chars = sum(len(line) + 1 for line in hidden)
    out.extend(indent + line for line in head)
    out.append("{}{} elided {:,} chars {} lines {}-{} of {} {}".format(
        indent, glyphs.ellipsis, elided_chars, glyphs.dot,
        HEAD_LINES + 1, len(lines) - TAIL_LINES, len(lines), glyphs.ellipsis))
    out.extend(indent + line for line in tail)
    return out


def render(
    context: Context,
    *,
    width: int = 78,
    collapse: bool = True,
    glyphs: Optional[Glyphs] = None,
) -> str:
    """The whole assembled context, as text."""
    glyphs = glyphs or Glyphs.detect()
    dot = " {} ".format(glyphs.dot)

    head = ["event {}{}llm{}{}{}{}".format(
        context.event_index, dot, dot, context.model or "?", dot, context.site)]
    if context.qual:
        head[-1] += " ({})".format(context.qual.split("::")[-1])

    facts = ["{} message{}".format(
        len(context.messages), "" if len(context.messages) == 1 else "s"),
        _kb(context.total_chars) + " of context"]
    if context.tokens.get("in") is not None:
        facts.append("{:,} in / {:,} out tokens".format(
            context.tokens.get("in") or 0, context.tokens.get("out") or 0))
    if context.cost_usd is not None:
        from .fmt import usd

        facts.append(usd(context.cost_usd))
    if context.streamed:
        facts.append("streamed")
    head.append(dot.join(facts))

    extras = ["{} {}".format(key, value) for key, value in sorted(context.params.items())]
    if context.tools:
        extras.append("tools: " + ", ".join(context.tools))
    if extras:
        head.append(dot.join(extras))

    out = list(head) + [""]
    for message in context.messages:
        label = "[{}] {} {} {}".format(message.index, message.role, glyphs.dot,
                                       message.shape)
        out.append(_rule(label, width, glyphs))
        out.extend(_body(message, glyphs, collapse))
        out.append("")

    if context.completion is not None:
        out.append(_rule("completion", width, glyphs))
        out.extend("  " + line for line in str(context.completion).splitlines())
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# -- diffing -------------------------------------------------------------


@dataclass
class Change:
    """One difference between two message arrays."""

    kind: str  # same | added | dropped | changed
    before: Optional[Message] = None
    after: Optional[Message] = None

    @property
    def truncated(self) -> bool:
        """True when the message survived but lost most of itself."""
        if self.kind != "changed" or not (self.before and self.after):
            return False
        if not self.before.chars:
            return False
        return self.after.chars < self.before.chars * TRUNCATION_RATIO

    @property
    def kept_prefix(self) -> bool:
        """True when the new text is the start of the old one, cut short."""
        if not (self.before and self.after and self.after.text):
            return False
        return (self.before.text.startswith(self.after.text)
                and len(self.after.text) < len(self.before.text))


def diff(before: Context, after: Context) -> List[Change]:
    """Align two message arrays and describe what changed between them.

    Sequence alignment rather than index comparison: a framework that injects
    one message at the front shifts everything after it, and an index-wise
    comparison would report every message as changed.
    """
    left = [m.signature for m in before.messages]
    right = [m.signature for m in after.messages]
    changes: List[Change] = []

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        a=left, b=right, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                changes.append(Change("same", before.messages[i1 + offset],
                                      after.messages[j1 + offset]))
        elif tag == "delete":
            changes.extend(Change("dropped", before=m)
                           for m in before.messages[i1:i2])
        elif tag == "insert":
            changes.extend(Change("added", after=m) for m in after.messages[j1:j2])
        else:  # replace
            old, new = before.messages[i1:i2], after.messages[j1:j2]
            paired = 0
            # Same position, same role: one message was edited in place. Any
            # surplus on either side is a genuine insertion or removal.
            while paired < min(len(old), len(new)) and \
                    old[paired].role == new[paired].role:
                changes.append(Change("changed", old[paired], new[paired]))
                paired += 1
            changes.extend(Change("dropped", before=m) for m in old[paired:])
            changes.extend(Change("added", after=m) for m in new[paired:])
    return changes


def _text_diff(
    before: Message, after: Message, limit: int = 6, glyphs: Optional[Glyphs] = None
) -> List[str]:
    """Line diff of one message, collapsed from the middle when long.

    Head *and* tail, for the same reason messages collapse that way: when a
    thousand lines vanish, the useful information is where the cut starts and
    where it ends, and six lines from the top shows only one of those.
    """
    glyphs = glyphs or Glyphs.detect()
    lines = list(difflib.unified_diff(
        before.text.splitlines(), after.text.splitlines(), n=0, lineterm=""))
    body = [line for line in lines[2:] if not line.startswith("@@")]
    if len(body) <= limit:
        return body
    head = max(1, limit - 2)
    return (
        body[:head]
        + ["{} {} more diff lines {}".format(
            glyphs.ellipsis, len(body) - head - 2, glyphs.ellipsis)]
        + body[-2:]
    )


def render_diff(
    before: Context,
    after: Context,
    *,
    width: int = 78,
    glyphs: Optional[Glyphs] = None,
) -> str:
    """The context diff between two LLM calls."""
    glyphs = glyphs or Glyphs.detect()
    dot = " {} ".format(glyphs.dot)
    changes = diff(before, after)

    delta_messages = len(after.messages) - len(before.messages)
    delta_chars = after.total_chars - before.total_chars
    out = [
        "context diff{}event {} {} event {}{}{}".format(
            dot, before.event_index, glyphs.arrow, after.event_index, dot,
            after.model or "?"),
        "  {} messages, {}  {}  {} messages, {}   ({:+d} message{}, {:+,} chars)".format(
            len(before.messages), _kb(before.total_chars), glyphs.arrow,
            len(after.messages), _kb(after.total_chars),
            delta_messages, "" if abs(delta_messages) == 1 else "s", delta_chars),
        "",
    ]

    marker = {"same": glyphs.same, "added": glyphs.added,
              "dropped": glyphs.dropped, "changed": glyphs.changed}
    for change in changes:
        message = change.after or change.before
        assert message is not None
        if change.kind == "same":
            out.append("  {} [{}] {:<10} unchanged {} {}".format(
                marker["same"], message.index, message.role, glyphs.dot,
                message.shape))
            continue

        if change.kind == "added":
            out.append("  {} [{}] {:<10} INJECTED {} {}".format(
                marker["added"], message.index, message.role, glyphs.dot,
                message.shape))
            out.extend(_body(message, glyphs, collapse=True, indent="        "))
        elif change.kind == "dropped":
            out.append("  {} [{}] {:<10} DROPPED {} {}".format(
                marker["dropped"], message.index, message.role, glyphs.dot,
                message.shape))
            out.extend(_body(message, glyphs, collapse=True, indent="        "))
        else:
            note = ""
            if change.truncated:
                note = ", TRUNCATED"
                if change.kept_prefix:
                    note = ", TRUNCATED (kept the first {:,} chars)".format(
                        change.after.chars)
            out.append("  {} [{}] {:<10} CHANGED {} {:,} {} {:,} chars ({:+,}{})".format(
                marker["changed"], message.index, message.role, glyphs.dot,
                change.before.chars, glyphs.arrow, change.after.chars,
                change.after.chars - change.before.chars, note))
            out.extend("        " + line
                       for line in _text_diff(change.before, change.after,
                                              glyphs=glyphs))
        out.append("")

    counts = {kind: sum(1 for c in changes if c.kind == kind)
              for kind in ("added", "dropped", "changed", "same")}
    summary = ", ".join(
        "{} {}".format(counts[kind], word)
        for kind, word in (("added", "injected"), ("changed", "changed"),
                           ("dropped", "dropped"))
        if counts[kind]
    ) or "no changes"
    if counts["same"]:
        summary = "{} {} {} unchanged".format(summary, glyphs.dot, counts["same"])
    out.append(summary)
    return "\n".join(out).rstrip() + "\n"
