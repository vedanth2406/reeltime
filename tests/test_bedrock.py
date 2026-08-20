"""The Bedrock decoder (M10): one endpoint, five model families, two framings.

Bedrock is a single API in front of model families that agree on almost
nothing. Each one puts its token counts somewhere different, the model id is
not in the body at all, and streaming is not SSE but
`application/vnd.amazon.eventstream` -- a binary framing with a length
prelude, typed headers, and two CRC32s per message.

So the fixture frames here are built by hand, and then **checked against
botocore's own parser** before anything else uses them. A decoder tested only
against bytes this file produced would agree with its own mistakes; running
the same bytes through the reference implementation, which validates both
CRCs, is what makes the rest of the file mean something.
"""

import base64
import binascii
import json
import struct

import pytest

import reeltime as tape
from reeltime.core.decoders import bedrock, pricing
from reeltime.core.http import common as http_common
from reeltime.core.trace import Event

try:
    import botocore.eventstream
except ImportError:  # pragma: no cover - boto3 is a dev dependency
    botocore = None

HOST = "https://bedrock-runtime.us-east-1.amazonaws.com"
CLAUDE = "anthropic.claude-3-5-sonnet-20241022-v2:0"
NOVA = "amazon.nova-lite-v1:0"


# -- the framing, and the fixture that has to be right first -------------


def frame(headers, payload):
    """One event-stream message: prelude, headers, payload, two CRC32s.

    The same construction the mock in `examples/bedrock_agent.py` uses. If it
    is wrong, `test_the_fixture_frames_are_what_bedrock_actually_sends` fails
    before any decoder test gets to draw a conclusion from it.
    """
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


def chunk_frame(obj):
    """A model-produced object, wrapped the two layers deep Bedrock wraps it."""
    payload = json.dumps({"bytes": base64.b64encode(
        json.dumps(obj).encode()).decode()}).encode()
    return frame([(":event-type", "chunk"), (":content-type", "application/json"),
                  (":message-type", "event")], payload)


METRICS = {"inputTokenCount": 137, "outputTokenCount": 6,
           "invocationLatency": 412, "firstByteLatency": 98}


def claude_stream():
    """Anthropic-on-Bedrock deltas, then the metrics event every family sends."""
    return [
        chunk_frame({"type": "content_block_delta",
                     "delta": {"type": "text_delta", "text": "A tape "}}),
        chunk_frame({"type": "content_block_delta",
                     "delta": {"type": "text_delta", "text": "you can rewind."}}),
        chunk_frame({"type": "message_stop",
                     "amazon-bedrock-invocationMetrics": METRICS}),
    ]


def test_botocore_is_installed():
    assert botocore is not None, (
        "boto3 is a dev dependency; without it the framing fixture below is "
        "checked against nothing but itself"
    )


def test_the_fixture_frames_are_what_bedrock_actually_sends():
    """The reference implementation validates both CRC32s. This is the anchor.

    Everything else in this file reasons about frames built by `frame()`
    above. Running them through botocore's parser first is what stops this
    suite agreeing with its own encoder -- a wrong prelude length or a
    miscomputed checksum fails here, loudly, instead of silently teaching
    `iter_frames` the wrong shape.
    """
    buffer = botocore.eventstream.EventStreamBuffer()
    buffer.add_data(b"".join(claude_stream()))
    parsed = list(buffer)

    assert len(parsed) == 3
    assert parsed[0].headers[":event-type"] == "chunk"
    assert parsed[0].headers[":message-type"] == "event"
    inner = json.loads(base64.b64decode(json.loads(parsed[0].payload)["bytes"]))
    assert inner["delta"]["text"] == "A tape "


def test_a_corrupted_frame_is_rejected_by_the_reference_parser():
    """Which is why byte-exact capture is the only thing worth having here.

    Nothing about this framing degrades gracefully: one flipped byte and
    botocore rejects the whole stream rather than returning slightly wrong
    text. A recording that re-chunks or re-encodes is not a recording of it.
    """
    frames = bytearray(b"".join(claude_stream()))
    frames[40] ^= 0xFF
    buffer = botocore.eventstream.EventStreamBuffer()
    with pytest.raises(botocore.eventstream.ChecksumMismatch):
        buffer.add_data(bytes(frames))
        list(buffer)


def test_iter_frames_agrees_with_botocores_parser():
    data = b"".join(claude_stream())

    buffer = botocore.eventstream.EventStreamBuffer()
    buffer.add_data(data)
    reference = [(dict(message.headers), message.payload) for message in buffer]

    assert list(bedrock.iter_frames(data)) == reference


def test_a_truncated_tail_yields_the_complete_frames_and_stops():
    """A killed run's trace ends mid-frame, and enrichment must survive it.

    A decoder that raised on a partial tail would take the whole enrichment
    with it -- including the token counts sitting in the frames that *did*
    arrive.
    """
    data = b"".join(claude_stream())
    truncated = data[:-12]

    frames = list(bedrock.iter_frames(truncated))
    assert len(frames) == 2                       # the third is incomplete
    assert frames[0][0][":event-type"] == "chunk"


def test_garbage_is_not_a_frame():
    assert list(bedrock.iter_frames(b"")) == []
    assert list(bedrock.iter_frames(b"not an event stream at all")) == []
    # A plausible prelude claiming more bytes than are present.
    assert list(bedrock.iter_frames(struct.pack("!III", 4096, 0, 0))) == []


# -- unwrapping the two layers Bedrock wraps a payload in ----------------


def test_frame_payloads_unwraps_the_base64_envelope():
    payloads = bedrock.frame_payloads(claude_stream())
    assert [p.get("type") for p in payloads] == [
        "content_block_delta", "content_block_delta", "message_stop"]


def test_a_frame_whose_payload_is_not_json_is_skipped():
    frames = [frame([(":event-type", "chunk")], b"not json"),
              chunk_frame({"type": "message_stop"})]
    assert [p.get("type") for p in bedrock.frame_payloads(frames)] == ["message_stop"]


def test_a_frame_whose_envelope_is_not_base64_is_skipped():
    frames = [frame([(":event-type", "chunk")], json.dumps({"bytes": "!!!"}).encode()),
              chunk_frame({"type": "message_stop"})]
    assert [p.get("type") for p in bedrock.frame_payloads(frames)] == ["message_stop"]


# -- reading the model id off the URL ------------------------------------


def test_the_model_id_comes_from_the_path_not_the_body():
    assert bedrock.model_from_path("/model/{}/invoke".format(CLAUDE)) == CLAUDE


def test_a_percent_encoded_model_id_is_decoded():
    # The id contains a colon, so it arrives encoded on the wire.
    assert bedrock.model_from_path(
        "/model/anthropic.claude-v2%3A1/invoke") == "anthropic.claude-v2:1"


@pytest.mark.parametrize("operation", list(bedrock.OPERATIONS))
def test_every_known_operation_is_stripped_off_the_id(operation):
    assert bedrock.model_from_path(
        "/model/{}/{}".format(NOVA, operation)) == NOVA


def test_a_path_that_names_no_model_has_no_id():
    assert bedrock.model_from_path("/v1/chat/completions") is None
    assert bedrock.model_from_path("/") is None


# -- recognising a Bedrock call ------------------------------------------


def event_for(model, body=None, frames=None, host=HOST,
              operation="invoke"):
    """A recorded event, as the urllib3 shim would have written it."""
    res = {"status": 200, "headers": []}
    if frames is not None:
        res["headers"] = [["content-type", "application/vnd.amazon.eventstream"]]
        res["stream"] = {"encoding": "base64",
                         "chunks": [base64.b64encode(f).decode() for f in frames]}
    else:
        res["body"] = {"json": body}
    return Event(
        i=0, kind="http", site="agent.py:1",
        req={"method": "POST",
             "url": "{}/model/{}/{}".format(host, model, operation),
             "headers": [], "body": {"json": {"inputText": "hi"}}},
        res=res,
    )


def test_bedrock_is_recognised_by_its_host():
    assert bedrock.matches(event_for(CLAUDE)) is True


def test_bedrock_is_recognised_behind_an_endpoint_override():
    """A VPC endpoint, a gateway, LocalStack, or this suite's own mock.

    Recognition that depends on the hostname loses the token counts for
    everyone who sets `endpoint_url=`, which is not an exotic configuration.
    """
    assert bedrock.matches(event_for(CLAUDE, host="http://127.0.0.1:8424")) is True


def test_the_decoder_does_not_claim_a_first_party_call():
    first_party = Event(
        i=0, kind="http", site="agent.py:1",
        req={"method": "POST", "url": "https://api.anthropic.com/v1/messages",
             "headers": [], "body": {"json": {}}},
        res={"status": 200, "headers": [], "body": {"json": {}}},
    )
    assert bedrock.matches(first_party) is False


def test_an_unrelated_path_on_an_unrelated_host_is_not_bedrock():
    other = event_for(CLAUDE, host="https://example.test", operation="predict")
    assert bedrock.matches(other) is False


# -- each family's answer, non-streaming ---------------------------------


ANTHROPIC_BODY = {
    "id": "msg_1", "type": "message", "role": "assistant",
    "content": [{"type": "text", "text": "A tape you can rewind."}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 137, "output_tokens": 6},
}

NOVA_BODY = {
    "output": {"message": {"role": "assistant",
                           "content": [{"text": "A tape you can rewind."}]}},
    "stopReason": "end_turn",
    "usage": {"inputTokens": 137, "outputTokens": 6},
}

TITAN_BODY = {
    "inputTextTokenCount": 137,
    "results": [{"tokenCount": 6, "outputText": "A tape you can rewind.",
                 "completionReason": "FINISH"}],
}

META_BODY = {
    "generation": "A tape you can rewind.",
    "prompt_token_count": 137, "generation_token_count": 6,
    "stop_reason": "stop",
}


@pytest.mark.parametrize("body", [ANTHROPIC_BODY, NOVA_BODY, TITAN_BODY, META_BODY],
                         ids=["anthropic", "nova-converse", "titan", "meta"])
def test_every_family_reports_the_same_token_counts(body):
    """Four spellings of the same two numbers -- `input_tokens`, `inputTokens`,
    `inputTextTokenCount`, `prompt_token_count` -- and one event schema."""
    found = bedrock.read_body(body)
    assert found["tokens_in"] == 137
    assert found["tokens_out"] == 6
    assert found["preview"] == "A tape you can rewind."
    assert found["stop"]


def test_an_unrecognised_body_shape_yields_nothing_rather_than_guessing():
    assert bedrock.read_body({"something": "new"}) == {}


# -- each family's answer, streamed --------------------------------------


def test_the_metrics_event_carries_the_tokens_for_every_family():
    """The one thing uniform across the families, and what streaming leans on."""
    found = bedrock.read_stream(bedrock.frame_payloads(claude_stream()))
    assert found["tokens_in"] == 137
    assert found["tokens_out"] == 6
    assert found["preview"] == "A tape you can rewind."


def test_converse_content_block_deltas_are_assembled():
    frames = [
        chunk_frame({"contentBlockDelta": {"delta": {"text": "A tape "}}}),
        chunk_frame({"contentBlockDelta": {"delta": {"text": "you can rewind."}}}),
        chunk_frame({"stopReason": "end_turn",
                     "amazon-bedrock-invocationMetrics": METRICS}),
    ]
    found = bedrock.read_stream(bedrock.frame_payloads(frames))
    assert found["preview"] == "A tape you can rewind."
    assert found["stop"] == "end_turn"


def test_titan_and_meta_stream_whole_segments_rather_than_deltas():
    frames = [chunk_frame({"outputText": "A tape "}),
              chunk_frame({"generation": "you can rewind.",
                           "amazon-bedrock-invocationMetrics": METRICS})]
    found = bedrock.read_stream(bedrock.frame_payloads(frames))
    assert found["preview"] == "A tape you can rewind."


def test_a_stream_with_no_metrics_event_still_reports_its_text():
    found = bedrock.read_stream(bedrock.frame_payloads(
        [chunk_frame({"type": "content_block_delta",
                      "delta": {"type": "text_delta", "text": "half a "}})]))
    assert found["preview"] == "half a "
    assert "tokens_in" not in found


# -- the whole decoder, and what it refuses to guess ---------------------


def test_a_non_streaming_call_is_decoded_into_an_llm_event():
    out = bedrock.decode(event_for(CLAUDE, body=ANTHROPIC_BODY))
    assert out["kind"] == "llm"
    assert out["req"] == {"provider": "bedrock", "model": CLAUDE, "streamed": False}
    assert out["res"]["tokens"] == {"in": 137, "out": 6}
    assert out["res"]["stop"] == "end_turn"


def test_a_streamed_call_is_decoded_from_its_recorded_chunks():
    out = bedrock.decode(event_for(CLAUDE, frames=claude_stream(),
                                   operation="invoke-with-response-stream"))
    assert out["req"]["streamed"] is True
    assert out["res"]["tokens"] == {"in": 137, "out": 6}
    assert out["res"]["preview"] == "A tape you can rewind."


def test_bedrock_is_priced_as_bedrock_not_as_the_first_party_model():
    """The shortcut this table exists to refuse.

    Claude 3.5 Sonnet is $3.00/$15.00 direct from Anthropic and $6.00/$30.00
    on Bedrock. Aliasing the Bedrock id to the first-party row is the obvious
    saving and produces a confidently wrong number in someone's cost report.
    """
    assert pricing.lookup("anthropic.claude-3-5-sonnet") == (6.00, 30.00)
    assert pricing.lookup("claude-3-5-sonnet") == (3.00, 15.00)

    out = bedrock.decode(event_for(CLAUDE, body=ANTHROPIC_BODY))
    # 137 in at $6/M, 6 out at $30/M.
    assert out["meta"]["cost_usd"] == pytest.approx(
        137 / 1e6 * 6.00 + 6 / 1e6 * 30.00)


def test_a_cross_region_inference_profile_resolves_to_the_same_price():
    # `us.anthropic.claude-…` is the same model in the same table, and the
    # geography is stripped rather than duplicated into a row per region.
    assert (pricing.lookup("us." + CLAUDE) == pricing.lookup(CLAUDE)
            == (6.00, 30.00))


def test_an_unpriced_model_reports_tokens_and_no_cost():
    """Deliberately incomplete, and honest about it.

    Only rows read off the pricing page are in the table -- the current-model
    tables render client-side, so Nova's could not be verified. Tokens
    populate, `cost_usd` stays absent, and nothing is inferred.
    """
    out = bedrock.decode(event_for(NOVA, body=NOVA_BODY))
    assert out["res"]["tokens"] == {"in": 137, "out": 6}
    assert "cost_usd" not in out["meta"]
    assert pricing.lookup(NOVA) is None


def test_a_body_the_decoder_cannot_read_still_produces_a_replayable_event():
    out = bedrock.decode(event_for(CLAUDE, body={"unexpected": "shape"}))
    assert out["kind"] == "llm"
    assert out["res"] == {}                  # no tokens invented
    assert out["meta"] == {}


# -- byte-exact, through a real recording --------------------------------


def test_a_recorded_event_stream_survives_frame_for_frame(recording, server):
    """The claim the whole streaming path exists to keep.

    Not just "the bytes come back": the *count* as well, because a recording
    that coalesced six frames into one blob still round-trips byte-identically
    and would pass an assertion that only joined them. And the prelude and both
    CRC32s per message, because botocore rejects the stream outright if any of
    them is a byte out -- checked here by handing the recorded bytes back to
    botocore's parser.
    """
    frames = claude_stream()
    url = server.route("/model/{}/invoke-with-response-stream".format(CLAUDE),
                       sse=frames,
                       headers={"content-type": "application/vnd.amazon.eventstream"})

    import urllib3

    response = urllib3.PoolManager().request("POST", url, preload_content=False)
    received = []
    while True:
        piece = response.read(65536)
        if not piece:
            break
        received.append(piece)
    tape.uninstall()

    event = tape.read_trace(recording.path).events[0]
    recorded = http_common.decode_chunks(event.res["stream"])

    assert len(recorded) == len(frames)            # frame for frame, not one blob
    assert recorded == frames                      # prelude and both CRC32s intact
    assert b"".join(received) == b"".join(frames)
    # Binary, so the chunk list is base64 rather than a mix of representations.
    assert event.res["stream"]["encoding"] == "base64"

    # And the reference parser still accepts what came off the tape.
    buffer = botocore.eventstream.EventStreamBuffer()
    buffer.add_data(b"".join(recorded))
    assert len(list(buffer)) == len(frames)

    # The decoder read the tokens out of exactly those recorded chunks.
    assert event.kind == "llm"
    assert event.res["tokens"] == {"in": 137, "out": 6}
