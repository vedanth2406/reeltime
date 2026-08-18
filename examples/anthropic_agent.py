"""A plain Anthropic SDK agent. No reeltime import anywhere in this file.

    export ANTHROPIC_API_KEY=sk-ant-...
    tape run python examples/anthropic_agent.py
    tape replay <run>
    tape show <run> 0 --context

Uses a system prompt and a tool definition, so the context view has something
worth showing: Anthropic sends `system` as its own top-level field, and
`tape show --context` hoists it into position 0 where you can actually see it.

Point it at a local mock instead of the real API with ANTHROPIC_BASE_URL.
"""

import json
import os

from anthropic import Anthropic

MODEL = os.environ.get("MODEL", "claude-sonnet-4-5")

client = Anthropic(
    base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
    max_retries=0,
)

SYSTEM = "You are a terse assistant. Prefer using a tool over guessing."

TOOLS = [
    {
        "name": "get_weather",
        "description": "Current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]


def get_weather(city):
    """Stands in for a real service. Recorded as part of the tool result."""
    return {"city": city, "conditions": "sunny", "celsius": 21}


def main():
    messages = [{"role": "user", "content": "What is the weather in Austin?"}]

    first = client.messages.create(
        model=MODEL, max_tokens=256, system=SYSTEM, tools=TOOLS, messages=messages
    )
    print("stop reason:", first.stop_reason)

    calls = [block for block in first.content if getattr(block, "type", "") == "tool_use"]
    if not calls:
        print("answer:", "".join(getattr(b, "text", "") for b in first.content))
        return

    call = calls[0]
    result = get_weather(**call.input)
    print("tool:", call.name, call.input, "->", result)

    messages += [
        {"role": "assistant", "content": [block.model_dump() for block in first.content]},
        {"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": json.dumps(result),
        }]},
    ]

    final = client.messages.create(
        model=MODEL, max_tokens=256, system=SYSTEM, tools=TOOLS, messages=messages
    )
    print("answer:", "".join(getattr(b, "text", "") for b in final.content))


if __name__ == "__main__":
    main()
