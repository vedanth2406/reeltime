"""The urllib3 shim (M10): the seam underneath botocore, and underneath `requests`.

Three layers, deliberately, the same shape as the MCP tests:

* the shim itself, driven with a plain urllib3 pool against a real socket --
  where the recording and replay mechanics live, and where they are cheap to
  cover exhaustively;
* the layering claims, which are about *not* recording: a `requests` call and a
  redirect both pass through this seam a second time, and each must still be
  one event;
* boto3 end to end, because **botocore signs before it sends**. That is the
  whole reason `core/aws.py` exists, and a fake client would skip exactly the
  layer that made this milestone hard.

The mock provider is a real socket rather than a patched transport for the same
reason the httpx tests use one: the shim's entire job is to sit underneath
somebody else's connection machinery, and a fake pool would skip the layer
under test.
"""

import json
import os
import threading

import pytest
import requests
import urllib3

import reeltime as tape
from reeltime.core import aws
from reeltime.core.http import common, urllib3_shim
from reeltime.core.trace import Event

try:
    import boto3
    import botocore.exceptions
except ImportError:  # pragma: no cover - boto3 is a dev dependency
    boto3 = botocore = None

SECRET = "sk-" + "A1b2C3d4E5f6G7h8I9j0" * 2

#: Distinct, equal-length, and written with a gap between them by the fixture
#: server. The gap is not decoration: without it the kernel coalesces the
#: writes and every boundary assertion below becomes vacuous.
STREAM = ["alpha-", "beta--", "gamma-", "delta-"]

TITAN_MODEL = "amazon.titan-text-lite-v1"
TITAN_PATH = "/model/{}/invoke".format(TITAN_MODEL)
TITAN_BODY = {
    "inputTextTokenCount": 11,
    "results": [{"tokenCount": 4, "outputText": "A tape you can rewind.",
                 "completionReason": "FINISH"}],
}


def events(run):
    return tape.read_trace(run.path).events


def http_events(run):
    return [e for e in events(run) if e.kind in ("http", "llm")]


def pool_request(url, **kwargs):
    """One request through a plain urllib3 pool manager."""
    return urllib3.PoolManager().request("GET", url, **kwargs)


def record(tape_dir, fn, run_id="01REC"):
    with tape.session(tape_dir=tape_dir, collect_git=False, run_id=run_id):
        return fn()


def replay(tape_dir, fn, run_id="01REC", **kwargs):
    with tape.session("replay", tape_dir=tape_dir, replay=run_id, **kwargs) as run:
        result = fn()
    return result, run.summary


# -- building the URL a pool only knows the target of ---------------------


class _Pool:
    def __init__(self, scheme="https", host="example.com", port=None):
        self.scheme, self.host, self.port = scheme, host, port


def test_a_request_target_becomes_an_absolute_url():
    assert urllib3_shim.absolute_url(_Pool(), "/v1/models") == (
        "https://example.com/v1/models")


def test_a_default_port_is_left_out_of_the_recorded_url():
    # The URL is part of the match key, so `https://h:443/x` and `https://h/x`
    # have to be the same request rather than two spellings of it.
    assert urllib3_shim.absolute_url(_Pool(port=443), "/x") == "https://example.com/x"
    assert urllib3_shim.absolute_url(_Pool("http", port=80), "/x") == (
        "http://example.com/x")


def test_a_non_default_port_is_kept():
    assert urllib3_shim.absolute_url(_Pool(port=8443), "/x") == (
        "https://example.com:8443/x")


def test_an_absolute_target_is_passed_through():
    # botocore sends an absolute URL when it is going through a proxy.
    assert urllib3_shim.absolute_url(_Pool(), "http://other.test/x") == (
        "http://other.test/x")


# -- recording -----------------------------------------------------------


def test_a_urllib3_call_is_recorded(recording, server):
    url = server.route("/api", json={"ok": True})
    pool_request(url)
    tape.uninstall()

    event = http_events(recording)[0]
    assert event.kind == "http"
    assert event.req["method"] == "GET"
    assert event.req["url"] == url
    assert event.res["status"] == 200
    assert event.res["body"]["json"] == {"ok": True}
    assert event.dur_ms > 0


def test_urllib3_is_named_in_the_footer(recording, server):
    # The footer answers "why was my call not recorded?", so a backend that is
    # patched has to say so -- and a test that asserts one event below has to
    # be able to tell "recorded once" from "never installed".
    server.route("/api", json={"ok": True})
    pool_request(server.base_url + "/api")
    tape.uninstall()
    assert "urllib3" in tape.read_trace(recording.path).footer["intercepted"]


def test_the_call_site_is_the_users_line_not_urllib3s(recording, server):
    import inspect

    url = server.route("/api", json={"ok": True})
    expected = inspect.currentframe().f_lineno + 1
    urllib3.PoolManager().request("GET", url)
    tape.uninstall()

    event = http_events(recording)[0]
    assert event.site.endswith("test_urllib3.py:{}".format(expected))
    # Not a line inside urllib3's own package, which is where the frame walk
    # lands if the shim's frames stop counting as internal.
    assert "connectionpool" not in event.site
    assert "site-packages" not in event.site


def test_a_request_body_is_recorded(recording, server):
    url = server.route("/api", json={"ok": True})
    urllib3.PoolManager().request(
        "POST", url, body=json.dumps({"q": "hello"}).encode(),
        headers={"content-type": "application/json"})
    tape.uninstall()

    event = http_events(recording)[0]
    assert event.req["method"] == "POST"
    assert event.req["body"]["json"] == {"q": "hello"}


def test_error_statuses_are_recorded(recording, server):
    url = server.route("/boom", status=500, json={"error": "nope"})
    pool_request(url)
    tape.uninstall()

    event = http_events(recording)[0]
    assert event.res["status"] == 500
    assert event.res["body"]["json"] == {"error": "nope"}


def test_binary_responses_round_trip(recording, server):
    payload = bytes(range(256))
    url = server.route("/blob", raw=payload)
    got = pool_request(url).data
    tape.uninstall()

    assert got == payload
    assert common.decode_body(http_events(recording)[0].res["body"]) == payload


def test_headers_are_recorded_and_scrubbed(recording, server):
    url = server.route("/api", json={"ok": True})
    pool_request(url, headers={"authorization": "Bearer " + SECRET,
                               "x-custom": "keep-me"})
    tape.uninstall()

    headers = dict(http_events(recording)[0].req["headers"])
    assert headers["authorization"] == "<redacted>"
    assert headers["x-custom"] == "keep-me"
    assert SECRET not in recording.path.read_text()


def test_a_connection_failure_is_recorded_not_lost(recording):
    # The agent saw this error and reacted to it, so a replay has to be able to
    # raise it again rather than quietly succeeding.
    with pytest.raises(urllib3.exceptions.HTTPError):
        urllib3.PoolManager(retries=False, timeout=0.3).request(
            "GET", "http://127.0.0.1:9/nope")
    tape.uninstall()

    event = http_events(recording)[0]
    assert event.res is None
    assert event.meta["error"]["type"]
    assert event.req["url"] == "http://127.0.0.1:9/nope"


def test_uninstall_restores_urlopen(tape_dir):
    original = urllib3.connectionpool.HTTPConnectionPool.urlopen
    tape.install(tape_dir=tape_dir, collect_git=False)
    assert urllib3.connectionpool.HTTPConnectionPool.urlopen is not original
    tape.uninstall()
    assert urllib3.connectionpool.HTTPConnectionPool.urlopen is original


# -- streaming: the chunk boundaries are the whole point ------------------


def test_a_stream_records_its_chunk_list_not_the_assembled_body(recording, server):
    url = server.route("/stream", sse=STREAM)
    response = pool_request(url, preload_content=False)
    received = []
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        received.append(chunk)
    tape.uninstall()

    event = http_events(recording)[0]
    assert "body" not in event.res
    assert event.res["stream"]["chunks"] == STREAM
    # What the caller saw and what was recorded are the same boundaries.
    assert received == [chunk.encode() for chunk in STREAM]


def test_read_with_a_size_does_not_collapse_the_chunk_boundaries(recording, server):
    """The bug that made the chunk-boundary claim quietly untrue.

    The obvious recording wrapper passes ``read(n)`` through to the inner
    response. That is wrong and it is wrong *silently*: ``read(n)`` blocks
    until it has n bytes or the connection ends, so a four-frame stream comes
    back as one read and every boundary is gone before anything can record it.

    The joined bytes are identical either way, so a round-trip assertion passes
    against the broken version -- **the count is what fails**. Verified by
    swapping the naive implementation back in: the same stream recorded as one
    chunk instead of four, and only this assertion noticed.
    """
    url = server.route("/stream", sse=STREAM)
    response = pool_request(url, preload_content=False)
    while response.read(65536):
        pass
    tape.uninstall()

    recorded = common.decode_chunks(http_events(recording)[0].res["stream"])
    assert recorded == [chunk.encode() for chunk in STREAM]
    assert len(recorded) == len(STREAM)          # not one coalesced blob
    assert b"".join(recorded) == b"".join(chunk.encode() for chunk in STREAM)


def test_read_none_returns_the_whole_body_but_records_the_chunks(recording, server):
    # Asking for everything means everything -- botocore reads a non-streaming
    # body exactly this way -- but the chunks are still recorded individually
    # and only joined on the way out.
    url = server.route("/stream", sse=STREAM)
    whole = pool_request(url, preload_content=False).read()
    tape.uninstall()

    assert whole == b"".join(chunk.encode() for chunk in STREAM)
    assert http_events(recording)[0].res["stream"]["chunks"] == STREAM


def test_an_abandoned_stream_still_records_what_arrived(recording, server):
    url = server.route("/stream", sse=STREAM)
    response = pool_request(url, preload_content=False)
    first = response.read(65536)
    response.release_conn()
    response.close()
    tape.uninstall()

    event = http_events(recording)[0]
    assert first == STREAM[0].encode()
    assert event.res["stream"]["chunks"][0] == STREAM[0]


def test_a_preloaded_response_records_the_body_it_already_read(recording, server):
    # urllib3's own default drains the body inside `urlopen`, so there is
    # nothing left to wrap by the time the shim sees it.
    url = server.route("/api", json={"ok": True})
    response = urllib3.PoolManager().request("GET", url)   # preload_content=True
    tape.uninstall()

    assert response.data == json.dumps({"ok": True}).encode()
    assert http_events(recording)[0].res["body"]["json"] == {"ok": True}


# -- replay --------------------------------------------------------------


def test_a_recorded_call_replays_from_the_tape_with_no_socket(tape_dir, server):
    url = server.route("/api", json={"answer": 42})

    def agent():
        return json.loads(pool_request(url).data)

    recorded = record(tape_dir, agent)
    server.received.clear()

    replayed, summary = replay(tape_dir, agent)
    assert replayed == recorded == {"answer": 42}
    assert server.received == []             # no network at all
    assert summary.events == 1


def test_a_replayed_stream_re_emits_the_recorded_boundaries(tape_dir, server):
    url = server.route("/stream", sse=STREAM)

    def agent():
        response = pool_request(url, preload_content=False)
        out = []
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            out.append(chunk)
        return out

    recorded = record(tape_dir, agent)
    server.received.clear()

    replayed, _ = replay(tape_dir, agent)
    assert replayed == recorded == [chunk.encode() for chunk in STREAM]
    assert server.received == []


def test_a_recorded_connection_error_is_raised_again(tape_dir):
    def agent():
        try:
            urllib3.PoolManager(retries=False, timeout=0.3).request(
                "GET", "http://127.0.0.1:9/nope")
        except urllib3.exceptions.HTTPError as exc:
            return type(exc).__name__

    recorded = record(tape_dir, agent)
    replayed, _ = replay(tape_dir, agent)
    # A replay in which a call that failed now succeeds is not a replay.
    assert replayed == recorded


def test_a_replayed_response_carries_its_recorded_status_and_headers(tape_dir, server):
    url = server.route("/boom", status=503, json={"error": "busy"},
                       headers={"x-request-id": "abc123"})

    def agent():
        response = pool_request(url)
        return response.status, response.headers.get("x-request-id")

    recorded = record(tape_dir, agent)
    replayed, _ = replay(tape_dir, agent)
    assert replayed == recorded == (503, "abc123")


# -- one event, not two --------------------------------------------------


def test_a_requests_call_through_the_seam_is_one_event_not_two(recording, server):
    """`requests` is built on urllib3, so both shims see one call.

    Nothing in the urllib3 shim handles this: the M1 boundary rule already
    does, because the outer shim wraps its inner call in `boundary()` and this
    one declines to record while `in_boundary()` is true. M10 budgeted for the
    problem and found it solved, which is the argument recorded in STATUS --
    so this test pins the behaviour rather than any mechanism.
    """
    url = server.route("/api", json={"ok": True})
    requests.get(url)
    tape.uninstall()

    trace = tape.read_trace(recording.path)
    # Both shims really were installed, or "one event" would mean nothing.
    assert {"requests", "urllib3"} <= set(trace.footer["intercepted"])
    assert len(http_events(recording)) == 1
    assert http_events(recording)[0].res["body"]["json"] == {"ok": True}


def test_a_redirect_followed_inside_the_pool_is_one_event(recording, server):
    """`HTTPConnectionPool.urlopen` follows a redirect by calling itself.

    That recursion happens underneath the shim, inside the boundary the
    outer call already opened, so the hops collapse into the one crossing the
    caller asked for. Same mechanism that makes a retry one event.
    """
    server.route("/final", json={"ok": True})
    start = server.route("/go", status=302, headers={"location": "/final"})
    pool = urllib3.HTTPConnectionPool("127.0.0.1", server.port)
    response = pool.urlopen("GET", "/go", redirect=True)
    tape.uninstall()

    assert json.loads(response.data) == {"ok": True}
    recorded = http_events(recording)
    assert len(recorded) == 1
    assert recorded[0].req["url"] == start


def test_a_redirect_followed_by_the_pool_manager_is_one_event_per_request(
    recording, server
):
    """And when the *manager* follows it, both hops are recorded.

    `PoolManager.urlopen` passes `redirect=False` down and follows the
    redirect itself, so each hop arrives at the seam as a separate crossing
    outside any boundary. Two events is the right answer: the client really
    did issue two requests, and a replay has to answer both.

    This is not a urllib3 quirk -- `requests` and `httpx` record a followed
    redirect the same way, for the same reason. Measured across all three
    rather than assumed, because "one event per redirect chain" is the
    plausible-sounding rule that would have been wrong.
    """
    server.route("/final", json={"ok": True})
    start = server.route("/go", status=302, headers={"location": "/final"})
    response = urllib3.PoolManager().request("GET", start)
    tape.uninstall()

    assert json.loads(response.data) == {"ok": True}
    recorded = http_events(recording)
    assert [event.req["url"] for event in recorded] == [
        start, server.base_url + "/final"]


def test_a_urllib3_call_inside_a_tool_is_one_event(recording, server):
    url = server.route("/api", json={"ok": True})

    @tape.tool
    def fetch():
        return json.loads(pool_request(url).data)

    assert fetch() == {"ok": True}
    tape.uninstall()

    recorded = events(recording)
    assert [e.kind for e in recorded] == ["tool"]


# -- AWS credentials, without touching the real environment --------------


def make_event(url):
    return Event(i=0, kind="http", site="agent.py:1",
                 req={"method": "POST", "url": url, "headers": [], "body": {}},
                 res={"status": 200, "headers": [], "body": {}})


def test_a_tape_that_touched_aws_is_recognised():
    assert aws.touches_aws([make_event(
        "https://bedrock-runtime.us-east-1.amazonaws.com/model/x/invoke")])


def test_a_tape_that_never_touched_aws_is_left_alone():
    environ = {}
    assert aws.touches_aws([make_event("https://api.openai.com/v1/chat")]) is False
    assert aws.inject_for_replay([make_event("https://api.openai.com/v1/chat")],
                                 environ) is None
    assert environ == {}


def test_a_machine_with_credentials_keeps_them():
    # Real credentials sign a request that is then served from the tape, which
    # is harmless and stays closer to the recorded run.
    environ = {"AWS_ACCESS_KEY_ID": "AKIAREAL", "AWS_SECRET_ACCESS_KEY": "real"}
    aws_event = make_event("https://bedrock-runtime.us-east-1.amazonaws.com/model/x/invoke")
    assert aws.inject_for_replay([aws_event], environ) is None
    assert environ["AWS_ACCESS_KEY_ID"] == "AKIAREAL"


def test_dummy_credentials_are_supplied_when_nothing_is_configured():
    environ = {}
    aws_event = make_event("https://bedrock-runtime.us-east-1.amazonaws.com/model/x/invoke")
    note = aws.inject_for_replay([aws_event], environ)

    assert note and "dummy AWS credentials" in note
    assert environ["AWS_ACCESS_KEY_ID"] == aws.DUMMY["AWS_ACCESS_KEY_ID"]
    # Recognisably fake, so a signature built from them is never mistaken for
    # an attempt at a real one.
    assert "EXAMPLE" in environ["AWS_ACCESS_KEY_ID"]
    assert environ["AWS_EC2_METADATA_DISABLED"] == "true"


def test_a_malformed_recorded_url_is_not_an_aws_host():
    assert aws.touches_aws([make_event("not-a-url")]) is False


# -- boto3, end to end ---------------------------------------------------


def test_boto3_is_installed():
    """A skipped section has to be a decision, not an accident.

    boto3 installs on every interpreter reeltime supports, so unlike the MCP
    and LangChain suites there is no version gate here -- if it is missing, the
    dev dependency is missing and the milestone's own example is untested.
    """
    assert boto3 is not None, (
        "boto3 is a dev dependency; without it the Bedrock example and every "
        "botocore test in this file are silently unexercised"
    )


@pytest.fixture
def no_aws_environment(tmp_path):
    """An environment with no AWS credentials and no config files to find.

    Restores by name rather than through monkeypatch: `inject_for_replay`
    writes variables nothing here ever set, and monkeypatch only reverses the
    keys it was told about -- so a leaked `AWS_SESSION_TOKEN` would follow the
    process into every test that ran afterwards.

    IMDS is disabled here, and that is not just for speed. A credential-less
    botocore probes `169.254.169.254` before giving up, which on a laptop is
    two slow timeouts and on an EC2 instance is a *successful* credential
    lookup -- so without this the same test means three different things on
    three machines. `aws.DISABLE` sets the same variable during a replay, for
    the replay-side version of the same reason.
    """
    saved = {key: value for key, value in os.environ.items()
             if key.startswith("AWS_")}
    for key in saved:
        os.environ.pop(key)
    os.environ["AWS_CONFIG_FILE"] = str(tmp_path / "no-such-config")
    os.environ["AWS_SHARED_CREDENTIALS_FILE"] = str(tmp_path / "no-such-credentials")
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    try:
        yield
    finally:
        for key in [k for k in os.environ if k.startswith("AWS_")]:
            os.environ.pop(key)
        os.environ.update(saved)


def bedrock_client(base_url, **credentials):
    """A client from a *fresh* session.

    `boto3.client()` goes through a cached default session, which resolves
    credentials once per process and reuses them. That cache silently makes a
    credential test pass: the recording phase populates it, and the replay
    phase then "works without credentials" because it never looked again.
    """
    return boto3.Session().client("bedrock-runtime", region_name="us-east-1",
                                  endpoint_url=base_url, **credentials)


FAKE_CREDENTIALS = {
    "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
}


def test_a_boto3_call_is_recorded_and_replays(tape_dir, server):
    """The milestone's reason to exist, in one test.

    Until this shim existed a Bedrock agent recorded *nothing at all* -- no
    event, no error, and a replay that went silently to the real API.
    """
    server.route(TITAN_PATH, json=TITAN_BODY)

    def agent():
        client = bedrock_client(server.base_url, **FAKE_CREDENTIALS)
        response = client.invoke_model(
            modelId=TITAN_MODEL, body=json.dumps({"inputText": "hi"}))
        return json.loads(response["body"].read())

    recorded = record(tape_dir, agent)
    assert recorded["results"][0]["outputText"] == "A tape you can rewind."
    server.received.clear()

    replayed, summary = replay(tape_dir, agent)
    assert replayed == recorded
    assert server.received == []             # offline, from the tape
    assert summary.events == 1


def test_a_boto3_call_is_decoded_into_an_llm_event(tape_dir, server):
    server.route(TITAN_PATH, json=TITAN_BODY)

    def agent():
        client = bedrock_client(server.base_url, **FAKE_CREDENTIALS)
        return client.invoke_model(modelId=TITAN_MODEL,
                                   body=json.dumps({"inputText": "hi"}))["body"].read()

    with tape.session(tape_dir=tape_dir, collect_git=False) as run:
        agent()

    event = tape.read_trace(run.path).events[0]
    assert event.kind == "llm"
    assert event.req["provider"] == "bedrock"
    assert event.req["model"] == TITAN_MODEL
    assert event.res["tokens"] == {"in": 11, "out": 4}


def test_a_boto3_call_inside_a_tool_is_one_event(tape_dir, server):
    """The outermost boundary is the one recorded, botocore included."""
    server.route(TITAN_PATH, json=TITAN_BODY)

    @tape.tool
    def ask_bedrock(prompt):
        client = bedrock_client(server.base_url, **FAKE_CREDENTIALS)
        response = client.invoke_model(
            modelId=TITAN_MODEL, body=json.dumps({"inputText": prompt}))
        return json.loads(response["body"].read())["results"][0]["outputText"]

    with tape.session(tape_dir=tape_dir, collect_git=False) as run:
        assert ask_bedrock("hi") == "A tape you can rewind."

    recorded = tape.read_trace(run.path).events
    assert [e.kind for e in recorded] == ["tool"]


def test_without_credentials_botocore_never_reaches_the_shim(
    tape_dir, server, no_aws_environment
):
    """Why `core/aws.py` has to exist at all, measured rather than argued.

    botocore signs before it sends, so with nothing configured the failure
    happens during signing and the provider is never contacted -- there is no
    request for a shim underneath to answer. That is what makes AWS different
    from every other stack reeltime intercepts, where replay needs nothing
    from the environment because interception sits below the credential.
    """
    server.route(TITAN_PATH, json=TITAN_BODY)

    with tape.session(tape_dir=tape_dir, collect_git=False) as run:
        with pytest.raises(botocore.exceptions.NoCredentialsError):
            bedrock_client(server.base_url).invoke_model(
                modelId=TITAN_MODEL, body=json.dumps({"inputText": "hi"}))

    assert server.received == []                       # nothing was sent
    assert tape.read_trace(run.path).events == []      # so nothing was recorded


def test_the_injected_credentials_are_enough_to_sign_with(
    tape_dir, server, no_aws_environment
):
    """And why the dummies work: signing only needs a credential to exist.

    The signature they produce is never checked, because nothing receives it
    -- the request is answered from the tape. So an obviously fake key is
    enough to get botocore past the step that was refusing to proceed.
    """
    server.route(TITAN_PATH, json=TITAN_BODY)
    os.environ.update(aws.DUMMY)

    with tape.session(tape_dir=tape_dir, collect_git=False) as run:
        response = bedrock_client(server.base_url).invoke_model(
            modelId=TITAN_MODEL, body=json.dumps({"inputText": "hi"}))
        assert json.loads(response["body"].read())["results"][0]["outputText"]

    assert len(tape.read_trace(run.path).events) == 1


def test_a_replay_of_an_aws_tape_supplies_credentials_and_says_so(
    tape_dir, server, no_aws_environment
):
    """The wiring, end to end: an AWS tape replayed on a machine with nothing.

    The recorded run below goes to the local mock, so its URLs are rewritten
    to a real Bedrock host afterwards -- that is what `touches_aws` reads, and
    faking it in the trace is safer than pointing a live client at
    `amazonaws.com` to get it. No request is replayed here; the claim under
    test is what installing a replay does to the environment, and that it is
    reported rather than left as a small mystery.
    """
    server.route(TITAN_PATH, json=TITAN_BODY)
    os.environ.update(aws.DUMMY)

    def agent():
        return bedrock_client(server.base_url).invoke_model(
            modelId=TITAN_MODEL, body=json.dumps({"inputText": "hi"}))["body"].read()

    record(tape_dir, agent)

    trace_file = next((tape_dir / "runs").glob("*.jsonl"))
    trace_file.write_text(trace_file.read_text().replace(
        server.base_url, "https://bedrock-runtime.us-east-1.amazonaws.com"))

    for key in list(aws.DUMMY):
        os.environ.pop(key, None)
    assert not aws.already_configured()

    with tape.session("replay", tape_dir=tape_dir, replay="01REC") as run:
        assert os.environ["AWS_ACCESS_KEY_ID"] == aws.DUMMY["AWS_ACCESS_KEY_ID"]

    assert any("dummy AWS credentials" in note for note in run.summary.notes())


def test_a_recording_never_supplies_credentials(tape_dir, server, no_aws_environment):
    """A recording signs with the user's real credentials or fails trying.

    Injecting here would make a recorded run talk to the wrong account, or
    fail confusingly -- so the recording path must not touch the environment
    even while it is writing a tape full of AWS calls.
    """
    server.route(TITAN_PATH, json=TITAN_BODY)
    os.environ.update(aws.DUMMY)
    os.environ["AWS_ACCESS_KEY_ID"] = "AKIAREALLOOKINGKEY"

    with tape.session(tape_dir=tape_dir, collect_git=False):
        bedrock_client(server.base_url).invoke_model(
            modelId=TITAN_MODEL, body=json.dumps({"inputText": "hi"}))
        assert os.environ["AWS_ACCESS_KEY_ID"] == "AKIAREALLOOKINGKEY"

    assert os.environ["AWS_ACCESS_KEY_ID"] == "AKIAREALLOOKINGKEY"


# -- degrading without boto3 ---------------------------------------------


def test_nothing_reeltime_ships_imports_boto3():
    """boto3 is a *dev* dependency, and the shipped code must not need it.

    The urllib3 shim, the Bedrock decoder and the credential injection are all
    reachable on a machine that has never installed boto3 -- a `requests` user
    hits the same seam. An import added to any of them would turn that into an
    ImportError at `tape run`, which is why this reads the source rather than
    trusting that the modules happen to import today.
    """
    import ast
    from pathlib import Path

    root = Path(tape.__file__).resolve().parent
    offenders = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name.split(".")[0] in ("boto3", "botocore") for name in names):
                offenders.append(path.name)
    assert offenders == [], offenders


def test_the_shim_declines_cleanly_when_urllib3_is_missing(monkeypatch, tape_dir):
    """`install()` returns False rather than raising when the module is absent.

    Every shim is independently optional -- reeltime has no runtime
    dependencies at all -- so a stack without urllib3 must install the rest and
    say so in the footer.
    """
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.split(".")[0] == "urllib3":
            raise ImportError("no urllib3 here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    shim = urllib3_shim.Urllib3Shim(engine=None)
    assert shim.install() is False
    shim.uninstall()          # and cleans up nothing, without complaining
