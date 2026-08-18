"""An agent with local tools, one of which is destructive.

    export OPENAI_API_KEY=sk-...
    tape run python examples/multi_tool_agent.py
    tape replay <run>          # delete_file does NOT run again

This is the example that shows why a replayed tool body must not execute. The
first run really deletes a file in a scratch directory; the replay reproduces
the agent's decisions without deleting anything, because the recorded result is
served from the tape and the body is skipped.

The `@tape.tool` decorators are the only reeltime code here, and outside a
recording session they are transparent -- the file runs normally on its own.
"""

import os
import random
import shutil
import tempfile

from openai import OpenAI

import reeltime as tape

MODEL = os.environ.get("MODEL", "gpt-4o-mini")

client = OpenAI(base_url=os.environ.get("OPENAI_BASE_URL") or None, max_retries=0)

SCRATCH = tempfile.mkdtemp(prefix="reeltime-example-")


@tape.tool
def list_files():
    return sorted(os.listdir(SCRATCH))


@tape.tool
def read_file(path):
    with open(os.path.join(SCRATCH, path)) as handle:
        return handle.read()


@tape.tool
def delete_file(path):
    """Destructive on purpose: replay must not do this twice."""
    os.remove(os.path.join(SCRATCH, path))
    return "deleted {}".format(path)


TOOLS = [
    {"type": "function", "function": {"name": "list_files", "parameters": {
        "type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "read_file", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"]}}},
    {"type": "function", "function": {"name": "delete_file", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"]}}},
]


def seed_scratch():
    for name, body in (("keep.txt", "important"), ("temp.log", "noise " * 200)):
        with open(os.path.join(SCRATCH, name), "w") as handle:
            handle.write(body)


def main():
    seed_scratch()
    # A recorded random draw, so replay picks the same "strategy".
    strategy = random.choice(["cautious", "thorough"])

    with tape.span("survey"):
        files = list_files()
        contents = {name: read_file(name)[:40] for name in files}

    with tape.span("plan"):
        reply = client.chat.completions.create(
            model=MODEL,
            temperature=0.2,
            tools=TOOLS,
            messages=[
                {"role": "system", "content":
                    "You tidy directories. Strategy: {}.".format(strategy)},
                {"role": "user", "content":
                    "Files: {}\nContents: {}\nWhich single file should be deleted?"
                    .format(files, contents)},
            ],
        )
        answer = reply.choices[0].message.content or ""

    with tape.span("act"):
        target = "temp.log" if "temp.log" in files else files[-1]
        print("strategy:", strategy)
        print("model said:", answer.strip()[:80])
        print(delete_file(target))
        print("remaining:", list_files())


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
