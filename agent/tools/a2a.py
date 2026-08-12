"""A2A client tools for the smarthome text agent.

``build_a2a_tools(grants, registry_id, user_token)`` resolves each granted agent's
AgentCard from AgentCore Registry, then returns one Strands tool per
(agentCardName, grantedSkillId) pair.

``grants`` is keyed on the AgentCard **name**, not a Registry recordId, because a
grant is a Cognito group named ``a2a-<agent>.<skill>`` and the name is what the
sub-agent knows itself as. See ``a2a_groups.py``.

Each tool:
  - Has a name like ``a2a_energy_optimization_agent_estimate_savings``.
  - Description = AgentCard skill description + examples (AI-readable).
  - Closure pins endpoint_url and the user's token so the LLM cannot forge either.
    The LLM-facing signature is ``_invoke(message: str)`` and nothing else —
    identity is never a parameter.
  - Sends an A2A JSON-RPC ``message/send`` with a single header:
      Authorization: Bearer <the end user's own idToken>

One token, because the authorization now lives in that token's claims. Each
sub-agent Runtime's ``customJWTAuthorizer.customClaims`` matches ``cognito:groups``
against its own grant groups, so a caller with no grant is refused by AgentCore
before the container is reached, and the sub-agent derives *which* skills from the
same verified claim.

That replaced three separate mechanisms: a ``client_credentials`` m2m token in
``Authorization`` (which had no ``sub``, hence a second header carrying the user),
and ``X-A2A-Allowed-Skills``, which the client set itself and could therefore widen.
A signed claim cannot be widened by the caller, and it is checked by the platform
rather than by us. The sub-agent still re-verifies the token independently rather
than trusting this hop — see a2a-agent-registry/common/user_identity.py.

The call goes straight to the sub-agent's Runtime rather than through the Gateway.
A Gateway passthrough hop was designed in to unify auth; unifying it on the user's
token achieved that without the hop, so adding one would now cost a round trip and
eight targets to keep in step for centralised egress alone.

All failures are soft: the tool returns a string beginning with
``"A2A agent call failed: ..."`` so the LLM can apologise / fall back rather
than crashing the whole agent turn.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FuturesTimeout
from functools import lru_cache
from typing import Any, Callable

import boto3

import a2a_prompt

logger = logging.getLogger(__name__)

# AWS Agent Registry GA namespace. Duplicated from shared/agent_registry.py rather
# than imported: only `agent/` is packaged into this runtime's CodeZip, and adding
# a build-time copy step for two constants and one accessor would be more moving
# parts than the duplication costs. shared/tests/test_registry_namespace.py
# asserts the two stay in agreement.
REGISTRY_CLIENT = "agent-registry-control"


def read_agent_card(record: dict) -> str:
    """The AgentCard JSON string from a Registry record.

    GA flattened the descriptor and renamed the field; the preview path is tried
    as a fallback so a record written before the migration still resolves.
    Mirrors shared/agent_registry.read_agent_card.
    """
    descriptors = record.get("descriptors") or {}
    ga = (descriptors.get("a2aAgentCard") or {}).get("data")
    if ga:
        return ga
    preview = ((descriptors.get("a2a") or {}).get("agentCard") or {}).get("inlineContent")
    return preview or ""

_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def _slug(raw: str) -> str:
    """Kept as a thin alias so this module reads unchanged.

    The definition lives in a2a_prompt because the routing table and the tool builder
    must derive identical names: if they disagreed, the prompt would confidently name
    a tool that does not exist and the model would fall back to its own knowledge
    with nothing logged.
    """
    return a2a_prompt.slug(raw)


# ----------------------------------------------------------------------------
# Context trimming (spec 5 S2)
# ----------------------------------------------------------------------------
#
# A specialist's FIRST event-loop cycle exists only to call `discover_devices`.
# The call is cheap (~0.2s); the cycle around it is a whole LLM turn, measured at
# 1.0-1.3s of the ~7s the specialist takes. Delegation is already two serial agent
# turns, and the catalog is static — so the devices can be named up front.
#
# Attached HERE rather than left to the orchestrator's prompt, deliberately. The
# alternative was to instruct the model to include device details in the message
# it composes, which makes a latency optimisation depend on the model complying
# every time, and it complies unevenly. This is a mechanical append: the tool has
# the request text, so it can do it on every call without being asked.
#
# `shared/device_brief.py` decides what is relevant and keeps it to ~100-200
# tokens; the full discovery payload is ~1,800, and pasting that in would move the
# cost from a round trip into the prompt rather than removing it.


def _device_context(message: str) -> str:
    """The relevant-devices brief for `message`, or "".

    Soft-fails to "" on any error, including the module simply not being present:
    the brief is an optimisation, and a delegation that failed because a hint could
    not be built would trade seconds for the whole answer. The specialist still
    has `discover_devices`, which is exactly the path this skips when it works.
    """
    try:
        from device_brief import delegation_context

        return delegation_context(message)
    except Exception as exc:  # noqa: BLE001
        logger.info("no device brief for this delegation (%s)", exc)
        return ""


# ----------------------------------------------------------------------------
# AgentCard resolution (LRU-cached; ~60s TTL via time-bucketed key)
# ----------------------------------------------------------------------------

def _registry_client():
    """AWS Agent Registry control plane (GA namespace).

    Registry moved to its own namespace at GA; the old `bedrock-agentcore`
    namespace stops serving it on 2026-09-17. This is a Registry-only client — the
    Gateway MCP calls elsewhere in this runtime stay where they are.

    The runtime's execution role needs `agent-registry:GetRegistryRecord`
    explicitly: the broad `bedrock-agentcore:*` grant it used to rely on does NOT
    cover Registry after GA. That failure mode is silent — `build_a2a_tools`
    warns and continues on a card it cannot fetch, so the A2A tools simply stop
    appearing rather than erroring.
    """
    return boto3.client(
        REGISTRY_CLIENT,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


@lru_cache(maxsize=32)
def _fetch_agent_card_cached(registry_id: str, record_id: str, _time_bucket: int) -> dict:
    """Fetch a Registry record's AgentCard as a plain dict.

    The ``_time_bucket`` arg is the invocation time / 60s, forcing lru_cache
    to miss after at most ~60s so we don't serve stale AgentCards forever.
    """
    ac = _registry_client()
    detail = ac.get_registry_record(registryId=registry_id, recordId=record_id)
    # GA flattened the descriptor and renamed the field:
    #   preview  descriptors.a2a.agentCard.inlineContent
    #   GA       descriptors.a2aAgentCard.data
    # `read_agent_card` tries GA first and falls back, so a record written before
    # the migration still resolves.
    raw = read_agent_card(detail)
    if not raw:
        raise ValueError(
            f"record {record_id}: no AgentCard in descriptors "
            f"(looked for a2aAgentCard.data and the preview path)")
    return json.loads(raw)


def fetch_agent_card(registry_id: str, record_id: str) -> dict:
    bucket = int(time.time() // 60)
    return _fetch_agent_card_cached(registry_id, record_id, bucket)


# ----------------------------------------------------------------------------
# Tool construction
# ----------------------------------------------------------------------------

@lru_cache(maxsize=4)
def _cards_by_name_cached(registry_id: str, _time_bucket: int) -> dict[str, dict]:
    """{AgentCard name: card} for every APPROVED agent record in the registry.

    Grants arrive keyed on the card NAME, because that is what a Cognito grant group
    encodes and what the sub-agent knows itself as. Records are still where the card
    lives, so this is the name -> card lookup.

    One paginated list per minute per container rather than a get per grant: a user
    with five grants used to cost five sequential round trips on the request path.
    Listing is also what makes a grant for a deprecated record resolve to "no such
    agent" instead of a 404 mid-turn.
    """
    client = _registry_client()
    out: dict[str, dict] = {}
    token = None
    while True:
        kwargs: dict[str, Any] = {
            "registryId": registry_id,
            "filters": [
                {"name": "recordType", "values": ["AGENT"]},
                {"name": "status", "values": ["APPROVED"]},
            ],
        }
        if token:
            kwargs["nextToken"] = token
        resp = client.list_registry_records(**kwargs)
        for summary in (resp.get("registryRecords") or resp.get("records") or []):
            record_id = summary.get("recordId")
            if not record_id:
                continue
            # The list response carries no descriptors, so the card still needs a
            # get. Cached with the whole map, so this is once a minute, not per turn.
            try:
                detail = client.get_registry_record(
                    registryId=registry_id, recordId=record_id)
                raw = read_agent_card(detail)
                if not raw:
                    continue
                card = json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not read the card for %s: %s", record_id, exc)
                continue
            name = card.get("name", "")
            if name:
                out[name] = card
        token = resp.get("nextToken")
        if not token:
            return out


def cards_by_name(registry_id: str) -> dict[str, dict]:
    return _cards_by_name_cached(registry_id, int(time.time() // 60))


def build_a2a_tools(
    grants: dict[str, list[str]],
    registry_id: str,
    user_token: str | None = None,
) -> list[Any]:
    """Return a list of Strands tools — one per granted (agent, skill) pair.

    ``grants`` is keyed on AgentCard name, as a Cognito grant group encodes it.

    ``user_token`` is the caller's own token and is now the ONLY credential: the
    sub-agent's Runtime authorizer validates it and checks its `cognito:groups` claim
    for a grant, so there is no separate service token. Without it no tool can be
    built, because there is nothing to authenticate with.

    Soft-fails per agent: logs a warning and skips one whose card cannot be
    resolved. Returns an empty list if ``grants`` is empty.
    """
    if not grants:
        return []

    # Import lazily so test code that patches strands works consistently.
    from strands import tool as strands_tool

    try:
        catalog = cards_by_name(registry_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("A2A card catalog fetch failed: %s", e)
        return []

    tools: list[Any] = []
    for agent_name, skill_ids in grants.items():
        if not skill_ids:
            continue
        card = catalog.get(agent_name)
        if card is None:
            # A grant group for an agent with no approved record. Skipped rather
            # than guessed: the group may predate a record being deprecated.
            logger.warning(
                "grant names agent %r, which has no approved Registry record; "
                "skipped", agent_name)
            continue
        # Straight to the sub-agent's Runtime, not through the Gateway. Routing A2A
        # through a Gateway passthrough target was designed in to unify auth, and
        # that reason evaporated once the user's own token became the credential:
        # the sub-agent's authorizer validates it and checks the grant claim
        # directly. A gateway hop would now buy centralised egress and its own
        # observability at the price of a round trip and eight targets to keep in
        # step, so it is deliberately not in this path. See docs §9.13.
        endpoint_url = card.get("url", "")
        if not endpoint_url:
            logger.warning("A2A card %s has no endpoint; skipped", agent_name)
            continue

        card_skill_ids = {s.get("id") for s in (card.get("skills") or [])}
        for skill in card.get("skills") or []:
            sid = skill.get("id", "")
            if sid not in skill_ids:
                continue
            if sid not in card_skill_ids:
                continue
            tool = _make_skill_tool(
                strands_tool=strands_tool,
                agent_name=agent_name,
                endpoint_url=endpoint_url,
                skill=skill,
                user_token=user_token,
                # The card we already have. Passing it removes the per-call
                # GET /.well-known/agent-card.json whose only used field was
                # overwritten on the next line anyway.
                card_dict=card,
            )
            if tool is not None:
                tools.append(tool)
    return tools


def _make_skill_tool(
    strands_tool,
    agent_name: str,
    endpoint_url: str,
    skill: dict,
    user_token: str | None = None,
    card_dict: dict | None = None,
):
    tool_name = a2a_prompt.tool_name(agent_name, skill.get("id", "x"))
    desc_parts = [skill.get("description", "").strip() or skill.get("name", "")]
    examples = skill.get("examples") or []
    if examples:
        desc_parts.append("Examples:")
        desc_parts.extend(f"- {e}" for e in examples)
    doc = "\n".join(desc_parts).strip() or "Invoke the remote A2A agent skill."

    # Bind loop-local copies so every tool closure captures its own values.
    # `_user_token` is pinned here for the same reason the MCP wrappers pin the
    # sub: it must not be reachable from the LLM-facing signature.
    _endpoint = endpoint_url
    _user_token = user_token
    _card = dict(card_dict or {})

    @strands_tool(name=tool_name, description=doc)
    def _invoke(message: str) -> str:
        """Send a natural-language request to the remote A2A agent skill."""
        try:
            return _send_a2a_message(
                endpoint_url=_endpoint,
                # The model's message plus a mechanical device brief. Appended
                # rather than prepended so the request stays the first thing the
                # specialist reads.
                message=message + _device_context(message),
                user_token=_user_token,
                card_dict=_card,
            )
        except A2AUnavailable as e:
            # Distinguished from a failure: the call was never attempted, so the
            # model should say the specialist is unavailable rather than imply it
            # was asked and gave a bad answer.
            logger.info("A2A call %s skipped by breaker: %s", tool_name, e)
            return f"A2A agent unavailable: {e}"
        except Exception as e:
            logger.warning("A2A call %s failed: %s", tool_name, e)
            return f"A2A agent call failed: {e}"

    return _invoke


# ----------------------------------------------------------------------------
# JSON-RPC message/send transport
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# Latency: one event loop, one connection pool, a local card, and a breaker
#
# Three measured costs on the delegation path, all avoidable (spec 3 §2.5, §4.4):
#
#  1. `asyncio.run` per call built a NEW event loop every time, so two
#     delegations in one turn could not overlap and neither could reuse a
#     connection. Three delegations stacked to 15-45s.
#  2. `A2ACardResolver.get_agent_card()` was a whole extra round trip to the
#     sub-agent whose only used field — `card.url` — was overwritten on the very
#     next line. The Registry record already carries the full card.
#  3. A bare 60s timeout with no retry and no breaker: one sick sub-agent cost
#     every turn a full minute before the model could say anything.
# ----------------------------------------------------------------------------

# Connect fast, read slow. A sub-agent that cannot be reached fails in a second;
# one that is thinking gets time to answer, because an LLM turn behind it is
# genuinely slow. A single 60s number could not express both.
_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 55.0

# Circuit breaker. After this many consecutive failures an endpoint is skipped
# outright until the cooldown passes, so a dead specialist costs one timeout
# rather than one per turn. Deliberately small: the point is to stop repeating a
# known failure inside a single conversation, not to model uptime.
_BREAKER_THRESHOLD = 3
_BREAKER_COOLDOWN_SECONDS = 60.0

# endpoint -> [consecutive_failures, opened_at]
_breaker: dict[str, list[float]] = {}


class A2AUnavailable(RuntimeError):
    """Raised when the breaker is open, so the caller reports it as a refusal."""


def _breaker_open(endpoint: str) -> bool:
    state = _breaker.get(endpoint)
    if not state:
        return False
    failures, opened_at = state
    if failures < _BREAKER_THRESHOLD:
        return False
    if time.time() - opened_at >= _BREAKER_COOLDOWN_SECONDS:
        # Cooldown elapsed: allow one probe through rather than resetting
        # outright, so a still-broken endpoint re-opens on its next failure.
        _breaker[endpoint] = [_BREAKER_THRESHOLD - 1, opened_at]
        return False
    return True


def _record_failure(endpoint: str) -> None:
    state = _breaker.setdefault(endpoint, [0.0, 0.0])
    state[0] += 1
    if state[0] >= _BREAKER_THRESHOLD:
        state[1] = time.time()


def _record_success(endpoint: str) -> None:
    _breaker.pop(endpoint, None)


# ----------------------------------------------------------------------------
# Parallel delegation (spec 5 S4)
#
# Strands ALREADY issues independent tool calls concurrently — measured: two tools
# in one turn both started within 0.00s of each other, in separate threads. So a
# multi-domain request ("my security gap and my energy usage") is already two
# overlapping delegations and needs no prompt change to become parallel.
#
# What did not work was this module's transport. It kept one module-level loop and
# drove it with `run_until_complete`, which a loop can only do from one thread at a
# time. Measured with three concurrent delegations: two ran, and the third raised
#
#     RuntimeError: This event loop is already running
#
# which `_send_a2a_message` catches, counts as an endpoint FAILURE, and returns to
# the model as "A2A agent call failed: ...". So a three-domain request showed the
# user an internal asyncio error attributed to a perfectly healthy specialist, and
# three of those in a turn would trip the circuit breaker against it.
#
# The loop now runs on its own daemon thread and work is submitted with
# `run_coroutine_threadsafe`, which is the thread-safe entry point. Any number of
# delegations can overlap, they share one connection pool as before, and no caller
# ever drives the loop itself.
# ----------------------------------------------------------------------------

_loop: Any = None
_loop_thread: Any = None
# Guards loop creation. Without it, two tool threads arriving together could each
# see `_loop is None` and start a second loop and thread — which would work, and
# would quietly halve the connection reuse this design exists for.
_loop_lock = threading.Lock()


def _get_loop():
    """The shared event loop, running on its own thread.

    Started on demand rather than at import so a fork-based worker does not
    inherit a thread bound to the parent. The thread is a daemon: the loop holds
    no state worth draining at shutdown, and a non-daemon thread would keep the
    container alive after the runtime asked it to stop.
    """
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is not None and not _loop.is_closed() and _loop.is_running():
            return _loop
        _loop = asyncio.new_event_loop()
        _loop_thread = threading.Thread(
            target=_loop.run_forever, name="a2a-loop", daemon=True)
        _loop_thread.start()
        return _loop


def _run_on_loop(coro, timeout: float):
    """Run `coro` on the shared loop from any thread and return its result.

    `run_coroutine_threadsafe` rather than `run_until_complete`: the latter can
    only be called from the thread that owns the loop, and Strands calls tools
    from a pool of threads. The timeout is a backstop only — httpx already applies
    its own connect/read timeouts inside the coroutine — so it is set above them,
    and exists so a hung coroutine cannot pin a tool thread forever.
    """
    future = asyncio.run_coroutine_threadsafe(coro, _get_loop())
    try:
        return future.result(timeout=timeout)
    except FuturesTimeout:
        # Stop the coroutine rather than leaving it running on the shared loop,
        # where it would keep a connection open for a request nobody is waiting on.
        future.cancel()
        raise TimeoutError(
            f"the specialist did not respond within {timeout:.0f}s") from None


def _local_agent_card(card_dict: dict, endpoint_url: str):
    """Build an AgentCard from the Registry record instead of fetching it.

    `ClientFactory.create()` needs a card object, which is the only reason the
    HTTP fetch existed — its `url` was overwritten with the Registry's
    invocation URL immediately afterwards, so the round trip bought nothing. The
    Registry record already holds every required field.

    Returns None if the record cannot satisfy the model, in which case the caller
    falls back to fetching, because a wrong card is worse than a slow one.
    """
    from a2a.types import AgentCapabilities, AgentCard, AgentSkill

    # AgentCard validates TYPES, not emptiness — `AgentCard(name="", url="")`
    # constructs happily and would be sent, to be rejected at the far end as a
    # malformed request. Check the two fields that must be real before building.
    if not card_dict.get("name") or not endpoint_url:
        return None

    try:
        skills = [
            AgentSkill(
                id=s.get("id", ""),
                name=s.get("name", s.get("id", "")),
                description=s.get("description", ""),
                tags=list(s.get("tags") or []),
                examples=list(s.get("examples") or []),
            )
            for s in (card_dict.get("skills") or [])
        ]
        return AgentCard(
            name=card_dict.get("name", ""),
            description=card_dict.get("description", ""),
            version=card_dict.get("version", "1.0.0"),
            # The URL we were GRANTED, not one the sub-agent reports about
            # itself. This was already the behaviour; now it is the only source.
            url=endpoint_url,
            capabilities=AgentCapabilities(streaming=False),
            default_input_modes=card_dict.get("defaultInputModes") or ["text"],
            default_output_modes=card_dict.get("defaultOutputModes") or ["text"],
            skills=skills,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not build a local AgentCard (%s); will fetch", exc)
        return None


def _send_a2a_message(
    endpoint_url: str,
    message: str,
    user_token: str | None = None,
    card_dict: dict | None = None,
) -> str:
    """Send one A2A ``message/send`` and collect the reply text.

    streaming=False so the server emits a single Task with artifacts; the reply
    text comes out of artifacts[0].parts. Left non-streaming deliberately — the
    change is wide and the sub-agent's marker is applied to the RESULT, not the
    stream (see a2a-agent-registry/common/server.py). The cost is that a
    delegated turn is silent until the specialist answers, which raises TTFT;
    that is documented on the Overview dashboard rather than hidden.
    """
    import httpx
    from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
    from a2a.types import Message, Part, Role, TextPart

    if _breaker_open(endpoint_url):
        raise A2AUnavailable(
            "this specialist has failed repeatedly and is being skipped for a "
            "short cooldown; report it as unavailable rather than retrying")

    async def _run() -> str:
        # One token, and it is the end user's own. The sub-agent's Runtime authorizer
        # validates it and checks its `cognito:groups` claim for a grant on itself,
        # so there is no service token to mint and no second header to allowlist.
        # The sub-agent verifies it again independently rather than trusting this hop.
        token = (user_token or "").strip()
        if token.lower().startswith("bearer "):
            token = token.split(" ", 1)[1].strip()
        if not token:
            raise A2AUnavailable(
                "no user token on this turn, and the end user's token is the only "
                "credential a specialist accepts")
        headers = {"Authorization": f"Bearer {token}"}
        timeout = httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT)
        async with httpx.AsyncClient(headers=headers, timeout=timeout) as http:
            card = _local_agent_card(card_dict or {}, endpoint_url)
            if card is None:
                # Fallback: the record could not produce a valid card, so pay for
                # the round trip rather than sending a malformed one.
                card = await A2ACardResolver(http, endpoint_url).get_agent_card()
                card.url = endpoint_url
            factory = ClientFactory(
                ClientConfig(httpx_client=http, streaming=False)
            )
            client = factory.create(card)
            msg = Message(
                message_id=str(uuid.uuid4()),
                role=Role.user,
                parts=[Part(root=TextPart(text=message))],
            )
            reply_text: str | None = None
            async for event in client.send_message(msg):
                items = event if isinstance(event, tuple) else (event,)
                for item in items:
                    if item is None:
                        continue
                    for art in getattr(item, "artifacts", None) or []:
                        for p in getattr(art, "parts", None) or []:
                            r = getattr(p, "root", p)
                            if getattr(r, "kind", "") == "text":
                                reply_text = r.text
                    if reply_text:
                        continue
                    for p in getattr(item, "parts", None) or []:
                        r = getattr(p, "root", p)
                        if getattr(r, "kind", "") == "text":
                            reply_text = r.text
            return reply_text or "(no response from A2A agent)"

    # Submitted to the shared loop from whichever thread Strands called this tool
    # on. `run_until_complete` used to be called here directly, which failed on the
    # THIRD concurrent delegation with "This event loop is already running" — see
    # the note above _get_loop.
    try:
        result = _run_on_loop(_run(), timeout=_READ_TIMEOUT + _CONNECT_TIMEOUT + 5)
    except Exception:
        _record_failure(endpoint_url)
        raise
    _record_success(endpoint_url)
    return result
