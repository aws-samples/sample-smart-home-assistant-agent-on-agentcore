"""The one function that decides which AgentCore Memory actor a request belongs to.

Shared because it has to be. The orchestrator and the eight A2A sub-agents are
separate containers reading the same Memory, and an actor id is a namespace path
component: `/users/{actorId}/preferences`. Two containers that sanitize the same
user differently do not fail — they each get a working, private, half-empty
memory, and the symptom is "the sub-agent never remembers what I told the main
agent", which looks like a retrieval problem rather than a naming one.

    orchestrator  actor "user_example_com"   writes the preference
    sub-agent     actor "88c1a3e0-b041-..."  retrieves nothing, silently

So: one function, one definition, copied rather than reimplemented.

  - `a2a-agent-registry/deploy.py` copies all of `shared/` into each sub-agent's
    container image.
  - `scripts/01-install-deps.sh` copies this file to `agent/memory_actor.py` for
    the orchestrator, whose container is built from `agent/` alone. That copy is a
    gitignored build output; this file is the source of truth.

Why EMAIL is the actor and not the Cognito sub
----------------------------------------------
The orchestrator has keyed memory by email since before the sub-agents existed,
and there are live memories under those ids. Moving to `sub` would be the tidier
identifier and would abandon every existing memory, so the sub-agents come to the
orchestrator's convention instead.

`sub` remains the right key elsewhere and this does not change that: scene rows
and the runner's per-user scheduling credential are keyed by sub, because those
are per-identity records rather than a shared namespace.
"""

from __future__ import annotations

import re

# AgentCore Memory accepts `[a-zA-Z0-9][a-zA-Z0-9-_/]*` for an actor id. An email
# fails on both counts: `@` and `.` are not in the set, and an address may not
# begin with an alphanumeric.
_DISALLOWED = re.compile(r"[^a-zA-Z0-9_/-]")


def sanitize_actor_id(actor_id: str) -> str:
    """Coerce an identifier into a valid AgentCore Memory actor id.

    Not reversible and not meant to be: it is a namespace component, never
    something read back and parsed. `user@example.com` -> `user_example_com`.
    """
    sanitized = _DISALLOWED.sub("_", actor_id or "")
    if not sanitized or not sanitized[0].isalnum():
        sanitized = "u" + sanitized
    return sanitized


def memory_actor_id(email: str = "", sub: str = "") -> str:
    """The Memory actor id for a verified caller.

    Email first, sub as the fallback for a user whose token carries no email
    claim. Returns "" when neither is present, and the caller must treat that as
    "no memory for this request" rather than substituting a shared default — one
    shared actor would pool unrelated users' preferences into a single namespace.
    """
    chosen = (email or "").strip() or (sub or "").strip()
    return sanitize_actor_id(chosen) if chosen else ""


# The `mem-` prefix distinguishes a Memory session id from a RUNTIME session id at
# a glance, which matters because the two are different strings for the same
# conversation and both get logged.
MEMORY_SESSION_PREFIX = "mem-"


def memory_session_id(actor_id: str) -> str:
    """The Memory session id for one user's conversation — STABLE across logins.

    Deliberately NOT the runtime session id (`user-session-{sub}-{epoch_ms}`,
    minted per login). AgentCore Memory's short-term memory is scoped to
    `(memoryId, actorId, sessionId)`, so keying it per login means every login
    starts with an empty transcript. `agent/memory/session.py` owns the full
    reasoning and the three writers that must agree on it.

    It lives HERE, next to the actor rule, because there is a fourth reader: the
    A2A sub-agents retrieve `/summaries/{actor}/{session}`, and that session
    component is this value. They can derive it from the actor alone — which is why
    reading the session summary never actually needed the orchestrator's runtime
    session id, even though it looked like it did. Propagating that id across the
    A2A hop is worth doing for observability (see shared/a2a_session.py) and is not
    what makes this namespace addressable.

    Accepts either a raw identifier or an already-sanitised actor id: sanitising is
    idempotent, so `memory_session_id(memory_actor_id(email=e))` and
    `memory_session_id(e)` agree. That matters because the orchestrator holds the
    raw email and a sub-agent holds the sanitised actor.
    """
    return f"{MEMORY_SESSION_PREFIX}{sanitize_actor_id(actor_id)}"
