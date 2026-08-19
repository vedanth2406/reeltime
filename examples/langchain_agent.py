"""A LangChain agent recorded as chain structure, not as two opaque POSTs.

    tape run python examples/langchain_agent.py    # records; no API key
    tape show last                                 # the run as a tree
    tape show last 4                               # one node: path, depth, io
    tape replay last                               # offline, free, identical

No API key and no network: a mock provider is embedded below, so this run
costs nothing and comes out the same for everyone. It binds a fixed port
because the request URL is part of what replay matches on -- an ephemeral port
would differ between record and replay and every model call would report as
drifted.

What the `chain` events buy you is the shape. Record it twice, once each way::

    tape run python examples/langchain_agent.py
    LANGCHAIN_EXAMPLE_TOOLS=extended tape run python examples/langchain_agent.py
    tape diff <first> <second>

The second run has one more tool available, so the agent loops once more. The
diff names that as extra nodes in the graph and a changed fan-out -- rather
than reporting that two message arrays differ somewhere, which is all a
transport-level trace could ever have said.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import reeltime as tape
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

PORT = int(os.environ.get("LANGCHAIN_EXAMPLE_PORT", "8423"))
EXTENDED = os.environ.get("LANGCHAIN_EXAMPLE_TOOLS") == "extended"

TEXT = "the quick brown fox jumps over the lazy dog"


# -- the tools the agent is given ----------------------------------------


@tool
def word_count(text: str) -> int:
    """Count the words in some text."""
    return len(text.split())


@tool
def reverse(text: str) -> str:
    """Reverse the words in some text. Present only in the extended tool set."""
    return " ".join(reversed(text.split()))


TOOLS = [word_count] + ([reverse] if EXTENDED else [])


# -- a provider that answers from the conversation it is given -----------


class Provider(BaseHTTPRequestHandler):
    """Decides the next step from the messages, so the run is reproducible.

    Turn 1 asks for `word_count`. If `reverse` is on the table and has not been
    used yet, turn 2 asks for that too. Otherwise it answers. The agent
    therefore takes a longer route through the graph when the tool set is
    larger, which is the difference the example exists to show.
    """

    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        messages = request.get("messages") or []
        offered = {(t.get("function") or {}).get("name")
                   for t in (request.get("tools") or [])}
        # Which tools have been asked for already. Read off the assistant's own
        # tool_calls, not off the `tool` messages: the reply to a tool call
        # carries only the call id, so counting those would never terminate.
        called = {(call.get("function") or {}).get("name")
                  for message in messages
                  for call in (message.get("tool_calls") or [])}

        if "word_count" not in called:
            message = _tool_call("call_1", "word_count", {"text": TEXT})
            finish = "tool_calls"
        elif "reverse" in offered and "reverse" not in called:
            message = _tool_call("call_2", "reverse", {"text": TEXT})
            finish = "tool_calls"
        else:
            message = {"role": "assistant",
                       "content": "Counted {} words.".format(len(TEXT.split()))}
            finish = "stop"

        body = json.dumps({
            "id": "chatcmpl-example",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-4o-mini",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 40 * len(messages), "completion_tokens": 12},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _tool_call(call_id, name, arguments):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "type": "function",
         "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Provider)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # One line, before the first chain runs. `tape run --langchain` does this
    # for you at interpreter startup if you would rather not edit the script.
    tape.langchain.install()

    model = ChatOpenAI(
        model="gpt-4o-mini",
        api_key="sk-not-a-real-key",
        base_url="http://127.0.0.1:{}/v1".format(PORT),
        max_retries=0,
    )
    agent = create_agent(model=model, tools=TOOLS)
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "How many words in the text?"}]})

    print("tools offered: {}".format(", ".join(t.name for t in TOOLS)))
    print("turns: {}".format(len(result["messages"])))
    print("answer: {}".format(result["messages"][-1].content))
    httpd.shutdown()


if __name__ == "__main__":
    main()
