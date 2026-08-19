"""Making a replay work on a machine with no AWS credentials.

A replay is meant to need nothing but the tape. For every other stack that is
automatically true, because reeltime intercepts below the point where a client
would ask for a credential. AWS is the exception: **botocore signs the request
before it sends it**, so a missing credential raises ``NoCredentialsError``
during signing and the shim underneath is never reached at all. Measured, not
assumed -- with the environment scrubbed, ``urlopen`` sees zero calls.

So a replay that touches AWS supplies its own credentials. Not real ones and
not the recorded ones -- the recorded ones were redacted before they reached
disk, which is the point of the redactor. Obviously-fake constants, whose only
job is to let the signer finish so the request can reach a shim that answers it
from the tape. The signature they produce is never checked by anything, because
nothing receives it.

Three limits keep this from being a surprise:

* **Only during replay.** A recording signs with the user's real credentials
  and must never do anything else -- injecting here would make a recorded run
  talk to the wrong account, or fail confusingly.
* **Only when the tape needs it.** The trace is checked for an AWS host first,
  so a run that never touched AWS has nothing done to its environment.
* **Only when nothing is configured already.** A machine with real credentials
  keeps them; they are used to sign a request that is then served from the
  tape, which is harmless and stays closer to the recorded run.

And it is reported. ``tape replay`` prints that it happened, because a replay
that works on a laptop with no AWS config at all is otherwise a small mystery,
and an unexplained success is the same kind of problem as an unexplained
failure.
"""

from __future__ import annotations

import os
from typing import Any, List, Optional, Sequence
from urllib.parse import urlsplit

#: Recognisably fake, so that a signature built from them is never mistaken for
#: an attempt at a real one. The access key uses AWS's own documentation
#: example, which is public and has never been valid.
DUMMY = {
    "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "AWS_SESSION_TOKEN": "reeltime-replay-not-a-real-session-token",
}

#: Set when credentials come from somewhere other than these variables -- a
#: config file, an instance profile, SSO. Pointing them at a path that does not
#: exist is what stops botocore going looking during a replay.
DISABLE = {
    "AWS_EC2_METADATA_DISABLED": "true",
}

#: What makes a recorded URL an AWS one.
AWS_SUFFIX = ".amazonaws.com"

#: The variables whose presence means the user has configured credentials.
CONFIGURED = ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
              "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN")


def touches_aws(events: Sequence[Any]) -> bool:
    """Whether any recorded event went to an AWS endpoint."""
    for event in events:
        if event.kind not in ("http", "llm"):
            continue
        url = (event.req or {}).get("url")
        if not isinstance(url, str):
            continue
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:  # pragma: no cover - malformed URL
            continue
        if host.endswith(AWS_SUFFIX):
            return True
    return False


def already_configured(environ: Optional[dict] = None) -> bool:
    source = os.environ if environ is None else environ
    return any(source.get(name) for name in CONFIGURED)


def inject_for_replay(events: Sequence[Any],
                      environ: Optional[dict] = None) -> Optional[str]:
    """Supply dummy AWS credentials if this replay will need them.

    Returns a line for the replay summary, or None when nothing was done.
    """
    source = os.environ if environ is None else environ
    if not touches_aws(events):
        return None
    if already_configured(source):
        return None
    source.update(DUMMY)
    source.update(DISABLE)
    return ("supplied dummy AWS credentials: botocore signs before it sends, so "
            "a replay of an AWS call needs a credential to exist even though "
            "the answer comes from the tape")


def note_lines(note: Optional[str]) -> List[str]:
    return [note] if note else []
