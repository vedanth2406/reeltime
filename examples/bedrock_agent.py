"""A Bedrock agent recorded through urllib3, streaming included.

    tape run python examples/bedrock_agent.py    # records; no AWS account
    tape show last                               # two llm events, with tokens
    tape replay last                             # offline, free, byte-identical

No AWS credentials and no network: a mock Bedrock endpoint is embedded below,
and the client is pointed at it with `endpoint_url`. The credentials are
obviously fake and are here only because **botocore signs before it sends** --
a request has to be signed before it can reach the layer reeltime records at.
On replay reeltime supplies its own dummy credentials when a machine has none,
so this replays on a laptop that has never seen an AWS config file.

`boto3` is built on `botocore`, which is built on `urllib3` -- not on httpx or
requests. That is the whole reason this example exists: until reeltime
intercepted urllib3, a Bedrock agent recorded *nothing at all*, and a replay of
one went silently to the real API.

The second call is the interesting one. Bedrock does not stream SSE; it streams
`application/vnd.amazon.eventstream`, a binary framing with a length prelude
and two CRC32s per message. Nothing about it degrades gracefully -- a recording
one byte out is rejected outright by the parser -- so the ordered chunk list is
recorded and replayed byte for byte.
"""

import binascii
import base64
import json
import os
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3

PORT = int(os.environ.get("BEDROCK_EXAMPLE_PORT", "8424"))
MODEL = "anthropic.claude-3-5-sonnet-20241022-v2:0"

#: Real streams arrive as separate reads. Without a gap the writes
#: coalesce and the recorded chunk list collapses to one entry.
FRAME_GAP_S = float(os.environ.get("BEDROCK_EXAMPLE_GAP", "0.02"))

ANSWER = "A tape you can rewind."
TOKENS = {"input": 137, "output": 6}


# -- AWS event-stream framing, so the mock streams what Bedrock streams ---


def _frame(headers, payload):
    """One event-stream message: prelude, headers, payload, two CRC32s."""
    encoded = b""
    for name, value in headers:
        name_b, value_b = name.encode(), value.encode()
        encoded += (struct.pack("!B", len(name_b)) + name_b + b"\x07"
                    + struct.pack("!H", len(value_b)) + value_b)
    total = 12 + len(encoded) + len(payload) + 4
    prelude = struct.pack("!II", total, len(encoded))
    prelude += struct.pack("!I", binascii.crc32(prelude) & 0xFFFFFFFF)
    body = prelude + encoded + payload
    return body + struct.pack("!I", binascii.crc32(body) & 0xFFFFFFFF)


def _chunk(obj):
    payload = json.dumps({"bytes": base64.b64encode(
        json.dumps(obj).encode()).decode()}).encode()
    return _frame([(":event-type", "chunk"), (":content-type", "application/json"),
                   (":message-type", "event")], payload)


# -- the mock endpoint ----------------------------------------------------


class Bedrock(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        if self.path.endswith("/invoke-with-response-stream"):
            return self._stream()
        return self._json()

    def _json(self):
        body = json.dumps({
            "id": "msg_example",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": ANSWER}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": TOKENS["input"],
                      "output_tokens": TOKENS["output"]},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self):
        words = ANSWER.split(" ")
        frames = [_chunk({"type": "content_block_delta",
                          "delta": {"type": "text_delta",
                                    "text": word + (" " if i < len(words) - 1 else "")}})
                  for i, word in enumerate(words)]
        frames.append(_chunk({
            "type": "message_stop",
            "amazon-bedrock-invocationMetrics": {
                "inputTokenCount": TOKENS["input"],
                "outputTokenCount": TOKENS["output"],
                "invocationLatency": 412, "firstByteLatency": 98},
        }))
        self.send_response(200)
        self.send_header("content-type", "application/vnd.amazon.eventstream")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        for frame in frames:
            # One at a time, with a pause. Flushing alone is not enough: without
            # a gap the kernel coalesces the writes and every frame arrives in a
            # single read, which would make the chunk-boundary claim vacuous.
            self.wfile.write(b"%x\r\n%s\r\n" % (len(frame), frame))
            self.wfile.flush()
            time.sleep(FRAME_GAP_S)
        self.wfile.write(b"0\r\n\r\n")

    def log_message(self, *args):
        pass


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Bedrock)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    client = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        endpoint_url="http://127.0.0.1:{}".format(PORT),
        # Fake, and only needed because botocore signs before it sends.
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    )
    prompt = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "What is reeltime, in six words?"}],
    })

    answer = json.loads(client.invoke_model(modelId=MODEL, body=prompt)["body"].read())
    print("invoke_model: {}".format(answer["content"][0]["text"]))

    streamed = client.invoke_model_with_response_stream(modelId=MODEL, body=prompt)
    pieces = []
    for event in streamed["body"]:
        payload = json.loads(event["chunk"]["bytes"])
        delta = payload.get("delta") or {}
        if delta.get("text"):
            pieces.append(delta["text"])
    print("streamed:     {}".format("".join(pieces)))
    print("frames:       {}".format(len(ANSWER.split(" ")) + 1))
    httpd.shutdown()


if __name__ == "__main__":
    main()
