"""A sub-agent's read-only view of the user's shared AgentCore Memory.

Two jobs: work out WHICH actor a request belongs to, and retrieve that actor's
long-term memory so a specialist knows what the user has already said elsewhere.

The actor-id RULE lives in `shared/memory_actor.py`, which the orchestrator uses
too; this module only knows how to find that module from inside a rendered
sub-agent container and how to read a CallerIdentity.

`deploy.py` copies the repo's `shared/` next to the agent code, so the import
works at runtime; the sys.path insert covers running the tests straight out of the
repo. Both paths reach the same file, which is the point — a second copy of the
sanitizing regex would be a second chance for the sub-agent's namespace to drift
from the orchestrator's, and that drift is silent.

One Memory, shared, and the sub-agents READ ONLY
------------------------------------------------
All eight specialists retrieve from the same Memory the orchestrator writes,
under the same three actor-partitioned namespaces (`/users/{actor}/facts`,
`/users/{actor}/preferences`, `/summaries/{actor}/{session}`). No agent
dimension: "the user prefers warm light" is a fact about the user, not about
whichever specialist happened to hear it, and partitioning per agent would mean
the light-effect agent could not act on a preference the user stated to the
orchestrator. That was the whole point of sharing.

Writing stays the orchestrator's alone, and that asymmetry is deliberate:

  - Only the orchestrator holds the conversation. A specialist receives one
    self-contained delegated instruction and cannot see the session history (a
    Spec 3 decision), so what it could write is half a sentence out of context —
    which then comes back as a "memory" on every future retrieval.
  - Eight processes writing one session's events concurrently would hand the
    SUMMARIZATION strategy an interleaved transcript of a conversation none of
    them individually had.

So there is no session manager here and no CreateEvent call. `retrieve_memory`
takes a namespace list, returns text, and cannot write. The absence is enforced
by the IAM grant too — `deploy.py` grants `RetrieveMemoryRecords` and nothing
else, so a future edit that tried to write would fail rather than quietly
succeed.

Failure is soft throughout. A specialist with no memory still answers; losing
the retrieval degrades the answer, and refusing the delegation instead would
trade a slightly worse answer for none at all.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from concurrent import futures
from pathlib import Path

logger = logging.getLogger(__name__)

# Set by deploy.py from the orchestrator's own runtime env, so the specialists
# read the Memory the orchestrator writes rather than one of their own. Empty
# means "no shared memory configured", which is a supported state: every agent
# behaves exactly as it did before this existed.
MEMORY_ID = os.environ.get("MEMORY_SMARTHOMEMEMORY_ID", "")

# Per namespace. Small on purpose: this text is prepended to the system prompt of
# every delegated request, so it is paid for in input tokens on each one. Three
# facts and three preferences is enough to carry "warm light, ocean mode" into a
# specialist's reasoning without turning a 400-token prompt into a transcript.
TOP_K = 3

# How much of one record to keep. A SUMMARIZATION record can run to several
# hundred words, and a specialist needs the gist, not the essay.
MAX_RECORD_CHARS = 400


def _memory_actor_module():
    try:
        import memory_actor  # type: ignore

        return memory_actor
    except ImportError:
        pass
    for candidate in (
        # Rendered container: shared/ sits next to common/.
        Path(__file__).resolve().parent.parent / "shared",
        # Repo checkout: a2a-agent-registry/common/ -> <root>/shared.
        Path(__file__).resolve().parent.parent.parent / "shared",
    ):
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    import memory_actor  # type: ignore

    return memory_actor


def memory_actor_for(caller) -> str:
    """The Memory actor id for `caller`, or "" when one cannot be formed.

    Never raises. A sub-agent with no memory still answers the request — memory is
    an enhancement here, and failing the whole delegation because a namespace could
    not be computed would trade a better answer for no answer.
    """
    try:
        return _memory_actor_module().memory_actor_id(
            email=getattr(caller, "email", ""), sub=getattr(caller, "sub", ""))
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not derive the memory actor id: %s", exc)
        return ""


def namespaces_for(actor_id: str) -> list[str]:
    """The namespaces a sub-agent reads, for `actor_id`.

    Facts, preferences, and the running session summary.

    `/summaries/{actor}/{session}` was long documented here as unreadable, on the
    reasoning that AgentCore assigns a runtimeSessionId per runtime and the A2A hop
    propagates nothing, so a sub-agent could not name the session whose summary it
    wanted. **That reasoning was wrong, and measuring it is what showed it.** The
    session component of this namespace is not a runtime session id at all: the
    orchestrator writes Memory under `memory_session_id(actor)` — `mem-{actor}`,
    stable across logins, deliberately NOT per-login (see
    `agent/memory/session.py`). It is derivable from the actor, which a sub-agent
    already has, so this namespace has been addressable all along.

    Verified against the live Memory before this changed: `mem-{actor}` holds the
    running summary and the runtime-session variant is empty.

    The orchestrator's runtime session id IS now propagated, and it remains worth
    propagating — it joins a delegated turn to its parent in logs and spans, which
    is what makes per-turn token attribution across a delegation possible. It is
    just not what makes this namespace work.

    `/users/{actor}/episodes` (EPISODIC) is different: it IS actor-partitioned, so
    a sub-agent could read it. It is left out on cost, not correctness — a
    specialist is handed a self-contained instruction, and the ordered account of
    how the user got here is context the orchestrator already used to compose that
    instruction. Add it here only with a measurement showing a delegated answer
    improves.
    """
    if not actor_id:
        return []
    out = [f"/users/{actor_id}/facts", f"/users/{actor_id}/preferences"]
    try:
        # Sanitising is idempotent, so passing the already-sanitised actor id
        # yields the same string the orchestrator computed from the raw email.
        session = _memory_actor_module().memory_session_id(actor_id)
    except Exception as exc:  # noqa: BLE001
        # Soft, like everything else here. This module reaches `shared/` through a
        # path lookup, so an older rendered container — or a standalone copy of this
        # code that did not ship `memory_actor.py` — resolves nothing. Losing the
        # session summary degrades an answer; raising would cost the whole
        # delegation, and the two facts namespaces above still work.
        logger.info("no memory session id for %s (%s); skipping the summary "
                    "namespace", actor_id, exc)
        return out
    if session:
        out.append(f"/summaries/{actor_id}/{session}")
    return out


def _record_text(record: dict) -> str:
    """The human-readable text of one memory record.

    Shapes vary by strategy and are not worth trusting: SEMANTIC records arrive as
    plain text, while USER_PREFERENCE and SUMMARIZATION records arrive as a JSON
    document inside `content.text` with the useful part under `context` or
    `preference`. Handled here rather than at the call site because a wrong guess
    is invisible — the prompt would carry a JSON blob and the model would cope
    just well enough that nobody noticed.
    """
    text = (record.get("content") or {}).get("text") or ""
    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            doc = json.loads(stripped)
        except ValueError:
            return stripped[:MAX_RECORD_CHARS]
        if isinstance(doc, dict):
            for key in ("context", "preference", "summary", "text"):
                value = doc.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:MAX_RECORD_CHARS]
            return json.dumps(doc, ensure_ascii=False)[:MAX_RECORD_CHARS]
    return stripped[:MAX_RECORD_CHARS]


def retrieve_memory(caller, query: str, client=None) -> str:
    """What this user has told the system before, as prompt-ready text.

    Returns "" when there is nothing to say — no configured memory, no derivable
    actor, no matching records, or a failed call. The caller appends the result to
    the system prompt only if it is non-empty, so "" means "behave exactly as
    before".

    `query` is the delegated request itself. Retrieval is relevance-ranked, so
    passing the actual request is what makes three records useful rather than
    three arbitrary ones.

    The namespaces are fetched CONCURRENTLY. They were sequential when there were
    two, and adding the third would have put another serial RetrieveMemoryRecords
    on the critical path of every delegated turn — a latency cost paid on each
    delegation to read a namespace that is usually small. The calls are
    independent, so the wall clock is now one round trip rather than three.

    Read-only by construction: this module calls RetrieveMemoryRecords and holds
    no session manager, so there is no code path that could write an event. See
    the module docstring for why.
    """
    actor_id = memory_actor_for(caller)
    namespaces = namespaces_for(actor_id)
    if not MEMORY_ID or not namespaces or not (query or "").strip():
        return ""

    if client is None:
        import boto3

        client = boto3.client(
            "bedrock-agentcore",
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
        )

    def _one(namespace: str) -> list[dict]:
        try:
            resp = client.retrieve_memory_records(
                memoryId=MEMORY_ID,
                namespace=namespace,
                searchCriteria={"searchQuery": query, "topK": TOP_K},
                maxResults=TOP_K,
            )
        except Exception as exc:  # noqa: BLE001
            # Per namespace, so one unavailable strategy does not cost the other.
            # A summary namespace that does not exist yet lands here and is
            # indistinguishable from "nothing to add", which is correct.
            logger.info("memory retrieve failed for %s: %s", namespace, exc)
            return []
        return resp.get("memoryRecordSummaries") or []

    # Results are collected in the namespaces' own order, not completion order:
    # facts before preferences before the session summary reads as intended, and
    # a prompt whose lines reorder between turns is a prompt cache miss.
    with futures.ThreadPoolExecutor(max_workers=len(namespaces)) as pool:
        per_namespace = list(pool.map(_one, namespaces))

    lines: list[str] = []
    for records in per_namespace:
        for record in records:
            text = _record_text(record)
            if text and text not in lines:
                lines.append(text)

    if not lines:
        return ""
    logger.info("retrieved %d memory record(s) for actor %s", len(lines), actor_id)
    return "\n".join(f"- {line}" for line in lines)


def memory_prompt_section(caller, query: str, client=None) -> str:
    """`retrieve_memory` wrapped in the framing the model needs, or "".

    The framing matters as much as the content. Unlabelled, these lines read as
    instructions for THIS request, and a specialist asked to dim the bedroom
    would apply a remembered ocean effect because the prompt appeared to ask for
    it. They are context about the user, and the model is told so explicitly —
    including that they may be stale and that the current request wins.
    """
    body = retrieve_memory(caller, query, client=client)
    if not body:
        return ""
    return (
        "\n\n## What this user has told the system before\n"
        "Context only, gathered from earlier conversations with other agents. "
        "It is NOT part of the current request and may be out of date. Use it to "
        "fill in unstated preferences; if it conflicts with what is being asked "
        "now, the current request wins.\n"
        f"{body}\n"
    )
