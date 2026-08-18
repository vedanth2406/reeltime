import json

import pytest

import reeltime as tape
from reeltime.core.redact import Redactor
from reeltime.core.trace import collect_env

FAKE_OPENAI = "sk-" + "A1b2C3d4E5f6G7h8I9j0" * 2
FAKE_ANTHROPIC = "sk-ant-api03-" + "Zz9" * 12
FAKE_GITHUB = "ghp_" + "a1B2c3D4e5" * 4
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g"


@pytest.mark.parametrize(
    "secret,label",
    [
        (FAKE_OPENAI, "sk"),
        (FAKE_ANTHROPIC, "sk-ant"),
        (FAKE_GITHUB, "gh"),
        (FAKE_JWT, "jwt"),
        ("AKIAIOSFODNN7EXAMPLE", "aws"),
        ("AIza" + "b" * 35, "gcp"),
        ("xoxb-123456789012-abcdefghijkl", "slack"),
    ],
)
def test_key_formats_are_caught(secret, label):
    redactor = Redactor()
    out = redactor.scrub_text("token is {} ok".format(secret))
    assert secret not in out
    assert "<redacted:{}>".format(label) in out
    assert redactor.hits[label] == 1


def test_ordinary_text_is_left_alone():
    redactor = Redactor()
    text = "The model said: delete b.txt, then ask the user to confirm."
    assert redactor.scrub_text(text) == text
    assert redactor.total_hits == 0


def test_sensitive_headers_are_dropped_entirely():
    redactor = Redactor()
    out = redactor.scrub_headers(
        {"Authorization": "Bearer " + FAKE_OPENAI, "Content-Type": "application/json"}
    )
    assert out["Authorization"] == "<redacted>"
    assert out["Content-Type"] == "application/json"


def test_secret_shaped_field_names_are_redacted_by_name():
    redactor = Redactor()
    out = redactor.scrub({"api_key": "not-key-shaped-but-still-a-key", "model": "gpt-4o"})
    assert out["api_key"] == "<redacted:named>"
    assert out["model"] == "gpt-4o"


def test_nested_structures_are_scrubbed():
    redactor = Redactor()
    out = redactor.scrub({"messages": [{"content": "use " + FAKE_OPENAI}]})
    assert FAKE_OPENAI not in json.dumps(out)


def test_custom_patterns():
    redactor = Redactor()
    redactor.add(r"ACME-[A-Z0-9]{8}", "acme")
    assert redactor.scrub_text("ACME-ABCD1234") == "<redacted:acme>"


def test_summary_counts_every_label():
    redactor = Redactor()
    redactor.scrub_text(FAKE_OPENAI + " " + FAKE_OPENAI + " " + FAKE_GITHUB)
    assert redactor.summary() == "2 sk, 1 gh"


def test_env_snapshot_is_an_allowlist_without_secrets():
    environ = {
        "MODEL": "gpt-4o-mini",
        "OPENAI_API_KEY": FAKE_OPENAI,
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "AWS_SECRET_ACCESS_KEY": "x" * 40,
        "HOME": "/home/v",
    }
    snapshot = collect_env(environ=environ)
    assert snapshot == {
        "MODEL": "gpt-4o-mini",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
    }


def test_secrets_never_reach_disk(recording):
    tape.record_event(
        "llm",
        {"model": "gpt-4o", "headers": {"authorization": "Bearer " + FAKE_OPENAI}},
        {"content": "my key is " + FAKE_ANTHROPIC},
    )
    # Large enough to be pushed into the blob store: the redactor has to run
    # before externalisation or the secret lands in .tape/blobs instead.
    tape.record_event("tool", {"name": "dump", "args": {"body": FAKE_GITHUB * 400}})
    summary = tape.uninstall()

    on_disk = recording.path.read_text()
    blobs = "".join(
        p.read_text() for p in (recording.config.tape_dir / "blobs").glob("*")
    )
    for secret in (FAKE_OPENAI, FAKE_ANTHROPIC, FAKE_GITHUB):
        assert secret not in on_disk
        assert secret not in blobs
    assert summary.redacted
    assert "redacted" in summary.redaction_line()


@pytest.mark.parametrize(
    "name", ["api_key", "apiKey", "API_KEY", "password", "client_secret",
             "openai_api_key", "refresh_token", "AccessKey"]
)
def test_credential_field_names_are_redacted(name):
    assert Redactor().scrub({name: "value"})[name] == "<redacted:named>"


@pytest.mark.parametrize(
    "name", ["key", "token", "auth", "max_tokens", "sort_key", "keyword",
             "session", "authors", "monkey"]
)
def test_ordinary_field_names_survive(name):
    # These are real tool arguments and request fields. Redacting them would
    # destroy exactly the data a trace exists to show.
    assert Redactor().scrub({name: "value"})[name] == "value"


def test_env_var_names_use_the_broader_rule():
    # An env var called TOKEN is a credential essentially always, so the
    # header snapshot stays aggressive where a payload field is not.
    from reeltime.core.redact import looks_secret, looks_secret_field

    assert looks_secret("GITHUB_TOKEN") and not looks_secret_field("token")
    assert looks_secret("SESSION_KEY") and not looks_secret_field("key")


def test_no_encoding_of_a_secret_reaches_disk(recording, server):
    """Search the trace for the secret in every form it could be stored in.

    A previous version stored a base64 copy of each JSON body next to the
    scrubbed one, so the secret was on disk in full while a literal-string
    search over the trace still came up clean. Redaction tests have to decode
    what they search.
    """
    import base64
    import json as _json

    import httpx

    url = server.route("/v1/chat", json={"echo": "token " + FAKE_OPENAI})
    # A spaced encoder, like most SDKs use -- the case that used to keep a raw copy.
    httpx.post(url, content=_json.dumps({"prompt": "my key is " + FAKE_OPENAI}),
               headers={"content-type": "application/json"})
    tape.uninstall()

    raw_text = recording.path.read_text()
    assert FAKE_OPENAI not in raw_text

    event = _json.loads(raw_text.splitlines()[1])
    for side in ("req", "res"):
        body = event[side]["body"]
        assert "raw" not in body, "a JSON body must not keep an unscrubbable copy"
        assert FAKE_OPENAI not in _json.dumps(body)

    # And nothing anywhere in the file decodes back to it.
    for token in raw_text.replace('"', " ").split():
        if len(token) > 24:
            try:
                decoded = base64.b64decode(token + "===", validate=True).decode("utf-8", "ignore")
            except Exception:
                continue
            assert FAKE_OPENAI not in decoded
