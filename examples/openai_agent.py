"""A plain OpenAI SDK agent. No reeltime import anywhere in this file.

    export OPENAI_API_KEY=sk-...
    tape run python examples/openai_agent.py
    tape replay <run>
    tape show <run> 1 --context

Two turns plus a streamed third, so the trace has a growing message array to
inspect and a stream to replay chunk by chunk.

Point it at a local mock instead of the real API with OPENAI_BASE_URL -- which
is what the integration test does.
"""

import os

from openai import OpenAI

MODEL = os.environ.get("MODEL", "gpt-4o-mini")

client = OpenAI(
    base_url=os.environ.get("OPENAI_BASE_URL") or None,
    max_retries=0,
)

SYSTEM = "You are a terse assistant. Answer in one short sentence."


def ask(history):
    reply = client.chat.completions.create(
        model=MODEL, messages=history, temperature=0.2
    )
    return reply.choices[0].message.content


def ask_streaming(history):
    pieces = []
    for chunk in client.chat.completions.create(
        model=MODEL, messages=history, temperature=0.2, stream=True,
        stream_options={"include_usage": True},
    ):
        if chunk.choices and chunk.choices[0].delta.content:
            pieces.append(chunk.choices[0].delta.content)
    return "".join(pieces)


def main():
    history = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "Name one planet."},
    ]

    first = ask(history)
    print("turn 1:", first)

    history += [
        {"role": "assistant", "content": first},
        {"role": "user", "content": "Now name its largest moon."},
    ]
    second = ask(history)
    print("turn 2:", second)

    history += [
        {"role": "assistant", "content": second},
        {"role": "user", "content": "Summarise both answers."},
    ]
    print("turn 3 (streamed):", ask_streaming(history))


if __name__ == "__main__":
    main()
