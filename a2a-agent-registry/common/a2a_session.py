"""How the orchestrator's runtime session id travels across the A2A hop.

COPY of shared/a2a_session.py, because each sub-agent container is rendered
from `a2a-agent-registry/` and does not import the repo's shared/ package.
shared/tests/test_a2a_session_parity.py holds the copies byte-identical
below this header.

A delegated turn used to be unjoinable to the turn that caused it. AgentCore
assigns a `runtimeSessionId` per runtime, and the A2A hop propagated nothing, so a
sub-agent stamped its own bare UUID on its spans and logs. The measured cost, which
`cdk/lambda/admin-api/index.py` documented as a limit of its own dashboard: per-agent
token totals were correct, but per-TURN totals across a delegation were impossible,
because the delegated tokens were recorded under a session id no orchestrator row
named.

Sending one string fixes that. Verified live on 2026-08-15: one delegated turn now
reports 55,056 orchestrator tokens and 3,188 specialist tokens under a single
`session.id`.

This module is where the convention lives, because two deployment units have to
agree on it and they cannot import each other.

What this is NOT for
--------------------
Reading the user's session SUMMARY. That was listed as a second motivation while
this was being designed, and it was a misdiagnosis: `/summaries/{actor}/{session}`
is keyed on the MEMORY session id (`mem-{actor}`, stable across logins — see
`shared/memory_actor.memory_session_id`), not on a runtime session id. A sub-agent
derives it from the actor it already has, and always could have. Recorded here
because the wrong version of this reasoning sat in
`a2a-agent-registry/common/memory.py` for months and read as convincing.

Two channels, because one header cannot do both jobs
----------------------------------------------------
There are two consumers of the id and they are reached differently.

`RUNTIME_SESSION_ID_HEADER` is the platform's own header — the same one
`scripts/measure-baseline.py` uses against a runtime URL. Sending it makes
AgentCore adopt the id as the sub-agent's `runtimeSessionId`, which is what puts
it on the sub-agent's spans and closes the token-attribution gap. That is the
whole benefit of this channel, and it happens without the container's
involvement.

The container itself can NEVER read that header. AgentCore's request-header
allowlist restricts every `x-amzn-` header except those prefixed
`X-Amzn-Bedrock-AgentCore-Runtime-Custom-`, so a runtime cannot opt into
receiving it — and the A2A server has no `RequestContext` to read a session id
from either, unlike the orchestrator's `@app.entrypoint`.

`SESSION_ID_METADATA_KEY` is therefore not a fallback: it is the only channel the
sub-agent's own code can see, and it is what makes `/summaries/{actor}/{session}`
addressable. It rides in the A2A JSON-RPC body, so nothing in the allowlist
applies to it and no hop that rewrites headers can remove it.

The header is still read on the way in, and still wins when it is present. Not
because it is expected — on the direct runtime path it never arrives — but because
if some future hop ever does surface it under an allowlistable name, that value is
by definition the one the spans were stamped with, and the log line should agree
with the spans rather than with the body.

Why the value is validated on both sides
----------------------------------------
AgentCore rejects a `runtimeSessionId` shorter than 33 characters, so sending a
short one turns a healthy delegation into a 400. The orchestrator's own ids are
`user-session-{sub}-{epoch_ms}` (~63 chars), but a warmup invocation's is the
literal `"default"` — hence a client-side check rather than an assumption.

The server checks too, for a different reason: the id is interpolated into a
memory namespace, and a sub-agent is reachable by any caller holding a grant, so
the value is caller-controlled input. Cross-user reads are already impossible —
the namespace's actor segment comes from the VERIFIED token, not from the request,
so a forged session id can only address the caller's own actor — but a charset
check keeps a hand-crafted value from forming a namespace nobody intended.
"""

from __future__ import annotations

import re

# The platform's own session header. Named in full rather than assembled, because
# a typo here degrades silently: the call still succeeds and the id is simply
# absent, which looks exactly like the state this module was written to fix.
#
# Deliberately NOT added to any runtime's `requestHeaderAllowlist` — it cannot be.
# `X-Amzn-Bedrock-AgentCore-Runtime-Custom-SessionId` would be allowlistable and is
# the escape hatch if the metadata channel ever becomes unavailable, but it would
# duplicate a value the body already carries and spend one of the 20 allowlist
# slots, so it is not used.
RUNTIME_SESSION_ID_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

# Our key inside A2A `Message.metadata`. camelCase to match the surrounding A2A
# payload rather than the Python around it.
SESSION_ID_METADATA_KEY = "orchestratorSessionId"

# AgentCore's own bounds for a runtimeSessionId. The lower bound is enforced by
# the service ("Member must have length greater than or equal to 33"); the upper
# one is documented alongside it.
MIN_SESSION_ID_LEN = 33
MAX_SESSION_ID_LEN = 128

# Deliberately narrower than "any string of a legal length". This covers the
# orchestrator's `user-session-{uuid}-{epoch_ms}` shape and plain UUIDs, and
# excludes the separators that would let a value reshape a memory namespace path.
_SESSION_ID_RE = re.compile(
    rf"^[A-Za-z0-9._-]{{{MIN_SESSION_ID_LEN},{MAX_SESSION_ID_LEN}}}$")


def usable_session_id(value: object) -> str:
    """`value` as a session id safe to send and to trust, or "".

    One function for both directions on purpose. If the client's idea of "sendable"
    were looser than the server's idea of "trustworthy", the orchestrator would
    send ids the sub-agent silently dropped, and the symptom — a summary namespace
    that never resolves — would look like a memory problem rather than a
    validation mismatch.
    """
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    return candidate if _SESSION_ID_RE.match(candidate) else ""


def session_id_from(headers: dict | None, metadata: dict | None) -> str:
    """The orchestrator's session id out of one inbound request, or "".

    `headers` must already be lower-cased, which is what
    `common.server._headers_from_context` produces.

    The header wins when both arrive — see the module docstring for why it is
    checked at all given that the platform never forwards it to a container.
    In practice this returns the metadata value.
    """
    for source, key in (
        (headers, RUNTIME_SESSION_ID_HEADER.lower()),
        (metadata, SESSION_ID_METADATA_KEY),
    ):
        if not source:
            continue
        try:
            found = usable_session_id(dict(source).get(key))
        except Exception:  # noqa: BLE001 - a malformed mapping is "not present"
            continue
        if found:
            return found
    return ""
