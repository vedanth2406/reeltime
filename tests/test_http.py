import asyncio
import base64

import httpx
import pytest
import requests

import reeltime as tape
from reeltime.core.http import common

SECRET = "sk-" + "A1b2C3d4E5f6G7h8I9j0" * 2


def http_events(run):
    return [e for e in tape.read_trace(run.path).events if e.kind in ("http", "llm")]


def test_a_sync_httpx_call_is_recorded(recording, server):
    url = server.route("/api", json={"ok": True})
    httpx.post(url, json={"q": "hello"})
    tape.uninstall()

    event = http_events(recording)[0]
    assert event.kind == "http"
    assert event.req["method"] == "POST"
    assert event.req["url"] == url
    assert event.req["body"]["json"] == {"q": "hello"}
    assert event.res["status"] == 200
    assert event.res["body"]["json"] == {"ok": True}
    assert event.dur_ms > 0


def test_an_async_httpx_call_is_recorded(recording, server):
    url = server.route("/api", json={"ok": True})

    async def main():
        async with httpx.AsyncClient() as client:
            return await client.post(url, json={"q": "hello"})

    asyncio.run(main())
    tape.uninstall()

    event = http_events(recording)[0]
    assert event.req["body"]["json"] == {"q": "hello"}
    assert event.res["body"]["json"] == {"ok": True}


def test_a_requests_call_is_recorded(recording, server):
    url = server.route("/api", json={"ok": True})
    requests.post(url, json={"q": "hello"})
    tape.uninstall()

    event = http_events(recording)[0]
    assert event.req["method"] == "POST"
    assert event.req["body"]["json"] == {"q": "hello"}
    assert event.res["body"]["json"] == {"ok": True}


def test_clients_made_before_install_are_still_caught(tape_dir, server):
    # The transport is resolved per request, so a client constructed earlier
    # is patched too -- which matters because agents build clients at import.
    url = server.route("/api", json={"ok": True})
    client = httpx.Client()
    run = tape.install(tape_dir=tape_dir, collect_git=False)
    client.post(url, json={})
    tape.uninstall()
    assert len(http_events(run)) == 1


def test_the_call_site_is_the_users_line_not_httpxs(recording, server):
    import inspect

    url = server.route("/api", json={"ok": True})
    expected = inspect.currentframe().f_lineno + 1
    httpx.post(url, json={})
    tape.uninstall()

    event = http_events(recording)[0]
    assert event.site.endswith("test_http.py:{}".format(expected))
    assert "httpx" not in event.site


def test_error_statuses_are_recorded(recording, server):
    url = server.route("/boom", status=500, json={"error": "nope"})
    httpx.post(url, json={})
    tape.uninstall()

    event = http_events(recording)[0]
    assert event.res["status"] == 500
    assert event.res["body"]["json"] == {"error": "nope"}


def test_binary_responses_round_trip(recording, server):
    payload = bytes(range(256))
    url = server.route("/blob", raw=payload)
    got = httpx.get(url).content
    tape.uninstall()

    assert got == payload
    event = http_events(recording)[0]
    assert base64.b64decode(event.res["body"]["raw"]) == payload


def test_non_json_text_is_kept_as_text(recording, server):
    url = server.route("/plain", text="just words")
    httpx.get(url)
    tape.uninstall()
    assert http_events(recording)[0].res["body"]["text"] == "just words"


def test_headers_are_recorded_and_scrubbed(recording, server):
    url = server.route("/api", json={"ok": True})
    httpx.post(url, json={}, headers={"authorization": "Bearer " + SECRET,
                                      "x-custom": "keep-me"})
    tape.uninstall()

    headers = dict(http_events(recording)[0].req["headers"])
    assert headers["authorization"] == "<redacted>"
    assert headers["x-custom"] == "keep-me"
    assert SECRET not in recording.path.read_text()


def test_http_interception_can_be_disabled(tape_dir, server):
    url = server.route("/api", json={"ok": True})
    run = tape.install(tape_dir=tape_dir, collect_git=False, http=False)
    httpx.post(url, json={})
    tape.uninstall()
    assert http_events(run) == []
    assert tape.read_trace(run.path).footer["intercepted"] == []


def test_uninstall_restores_httpx(tape_dir, server):
    original = httpx.Client._transport_for_url
    tape.install(tape_dir=tape_dir, collect_git=False)
    assert httpx.Client._transport_for_url is not original
    tape.uninstall()
    assert httpx.Client._transport_for_url is original


def test_nothing_is_recorded_after_uninstall(recording, server):
    url = server.route("/api", json={"ok": True})
    httpx.post(url, json={})
    tape.uninstall()
    httpx.post(url, json={})
    assert len(http_events(recording)) == 1


# -- streaming -----------------------------------------------------------

CHUNKS = [
    'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n',
    'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
    "data: [DONE]\n\n",
]


def test_a_stream_records_its_chunk_list_not_the_assembled_body(recording, server):
    url = server.route("/stream", sse=CHUNKS)
    with httpx.stream("POST", url, json={}) as response:
        received = list(response.iter_bytes())
    tape.uninstall()

    event = http_events(recording)[0]
    assert "body" not in event.res
    assert event.res["stream"]["chunks"] == CHUNKS
    # What the caller saw and what was recorded are the same boundaries.
    assert received == [c.encode() for c in CHUNKS]


def test_chunk_boundaries_round_trip_exactly(recording, server):
    url = server.route("/stream", sse=CHUNKS)
    with httpx.stream("POST", url, json={}) as response:
        list(response.iter_bytes())
    tape.uninstall()

    recorded = common.decode_chunks(http_events(recording)[0].res["stream"])
    assert recorded == [c.encode("utf-8") for c in CHUNKS]
    assert len(recorded) == 3  # not one coalesced blob


def test_an_async_stream_records_its_chunks(recording, server):
    url = server.route("/stream", sse=CHUNKS)

    async def main():
        async with httpx.AsyncClient() as client:
            async with client.stream("POST", url, json={}) as response:
                return [chunk async for chunk in response.aiter_bytes()]

    received = asyncio.run(main())
    tape.uninstall()

    event = http_events(recording)[0]
    assert event.res["stream"]["chunks"] == CHUNKS
    assert received == [c.encode() for c in CHUNKS]


def test_an_abandoned_stream_still_records_what_arrived(recording, server):
    url = server.route("/stream", sse=CHUNKS)
    with httpx.stream("POST", url, json={}) as response:
        first = next(response.iter_bytes())
    tape.uninstall()

    event = http_events(recording)[0]
    assert first == CHUNKS[0].encode()
    assert event.res["stream"]["chunks"][0] == CHUNKS[0]


def test_a_streamed_requests_response_is_flagged_not_faked(recording, server):
    url = server.route("/api", json={"ok": True})
    response = requests.post(url, json={}, stream=True)
    response.close()
    tape.uninstall()

    event = http_events(recording)[0]
    # Reading it would consume the caller's stream, so the trace says so
    # rather than recording a body that was never seen.
    assert event.meta["stream_not_captured"] is True
    assert "body" not in event.res


def test_a_connection_failure_is_recorded_not_lost(recording):
    # The agent saw this error and reacted to it, so replay has to be able to
    # raise it again rather than quietly succeeding.
    with pytest.raises(httpx.ConnectError):
        httpx.get("http://127.0.0.1:9/nope", timeout=0.3)
    tape.uninstall()

    event = http_events(recording)[0]
    assert event.res is None
    assert event.meta["error"]["type"] == "ConnectError"
    assert event.req["url"] == "http://127.0.0.1:9/nope"


def test_a_requests_connection_failure_is_recorded(recording):
    with pytest.raises(requests.exceptions.ConnectionError):
        requests.get("http://127.0.0.1:9/nope", timeout=0.3)
    tape.uninstall()

    event = http_events(recording)[0]
    assert event.res is None
    assert event.meta["error"]["type"] == "ConnectionError"


def test_an_async_connection_failure_is_recorded(recording):
    async def main():
        async with httpx.AsyncClient() as client:
            await client.get("http://127.0.0.1:9/nope", timeout=0.3)

    with pytest.raises(httpx.ConnectError):
        asyncio.run(main())
    tape.uninstall()
    assert http_events(recording)[0].meta["error"]["type"] == "ConnectError"


def test_a_streamed_request_body_is_flagged_not_consumed(recording, server):
    url = server.route("/upload", json={"ok": True})

    def chunks():
        yield b"part-one"
        yield b"part-two"

    httpx.post(url, content=chunks())
    tape.uninstall()

    # Reading it to record it would consume the caller's generator.
    assert http_events(recording)[0].req["body"] == {"streamed": True}
    assert server.received[0]["body"] == b"part-onepart-two"
