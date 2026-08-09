"""A2A client tools for the smarthome text agent.

``build_a2a_tools(grants, registry_id, token_provider)`` resolves each granted
A2A record's AgentCard from AgentCore Registry, then returns one Strands tool
per (recordId, grantedSkillId) pair.

Each tool:
  - Has a name like ``a2a_energy_optimization_agent_estimate_savings``.
  - Description = AgentCard skill description + examples (AI-readable).
  - Closure pins endpoint_url + allowed_skill_ids + token_provider + the user's
    idToken so the LLM cannot forge any of them. The LLM-facing signature is
    ``_invoke(message: str)`` and nothing else — identity is never a parameter.
  - Sends an A2A JSON-RPC ``message/send`` with
      Authorization: Bearer <m2m JWT>        (service identity; Runtime checks it)
      X-A2A-Allowed-Skills: <csv>            (ENFORCED downstream, not a hint)
      X-SuperApp-User-Token: <user idToken>  (who this is actually for)

Why the user token rides in its own header: the m2m token is a
client_credentials token with no ``sub``, so it identifies the calling service
and nothing else. A sub-agent that touches a user's devices needs the end user,
and ``Authorization`` is already taken by the token the Runtime's CUSTOM_JWT
authorizer validates — the same split the main runtime uses when the chatbot
sends its idToken in X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken.

The sub-agent re-verifies that token (signature, issuer, audience, expiry) rather
than trusting it, and uses the resulting sub to call the same Gateway the main
agent does, so Cedar evaluates the real end user. See
a2a-agent-registry/common/user_identity.py.

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
import time
import uuid
from functools import lru_cache
from typing import Any, Callable

import boto3

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def _slug(raw: str) -> str:
    return _SLUG_RE.sub("_", raw.lower()).strip("_") or "x"


# ----------------------------------------------------------------------------
# AgentCard resolution (LRU-cached; ~60s TTL via time-bucketed key)
# ----------------------------------------------------------------------------

def _registry_client():
    return boto3.client(
        "bedrock-agentcore-control",
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
    a2a = (detail.get("descriptors") or {}).get("a2a", {}) or {}
    raw = (a2a.get("agentCard") or {}).get("inlineContent", "")
    if not raw:
        raise ValueError(f"record {record_id}: empty agentCard.inlineContent")
    return json.loads(raw)


def fetch_agent_card(registry_id: str, record_id: str) -> dict:
    bucket = int(time.time() // 60)
    return _fetch_agent_card_cached(registry_id, record_id, bucket)


# ----------------------------------------------------------------------------
# Tool construction
# ----------------------------------------------------------------------------

def build_a2a_tools(
    grants: dict[str, list[str]],
    registry_id: str,
    token_provider: Callable[[], str],
    user_token: str | None = None,
) -> list[Any]:
    """Return a list of Strands tools — one per granted (record, skill) pair.

    ``user_token`` is the caller's idToken, already validated by the Runtime
    before ``invoke_agent`` ran. It is forwarded so a sub-agent can act as that
    user against the same Gateway, under the same Cedar policies. Optional so an
    unauthenticated path still builds tools that work for prompt-only agents;
    tool-using sub-agents refuse a request that arrives without it.

    Soft-fails on per-record AgentCard resolution: logs a warning and skips
    that record. Returns an empty list if ``grants`` is empty.
    """
    if not grants:
        return []

    # Import lazily so test code that patches strands works consistently.
    from strands import tool as strands_tool

    tools: list[Any] = []
    for record_id, skill_ids in grants.items():
        if not skill_ids:
            continue
        try:
            card = fetch_agent_card(registry_id, record_id)
        except Exception as e:
            logger.warning("A2A AgentCard fetch failed for %s: %s", record_id, e)
            continue
        endpoint_url = card.get("url", "")
        agent_name = card.get("name", "")
        if not endpoint_url or not agent_name:
            logger.warning("A2A card %s missing url/name; skipped", record_id)
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
                allowed_skill_ids=list(skill_ids),
                token_provider=token_provider,
                user_token=user_token,
            )
            if tool is not None:
                tools.append(tool)
    return tools


def _make_skill_tool(
    strands_tool,
    agent_name: str,
    endpoint_url: str,
    skill: dict,
    allowed_skill_ids: list[str],
    token_provider: Callable[[], str],
    user_token: str | None = None,
):
    tool_name = f"a2a_{_slug(agent_name)}_{_slug(skill.get('id', 'x'))}"
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
    _allowed = list(allowed_skill_ids)
    _token = token_provider
    _user_token = user_token

    @strands_tool(name=tool_name, description=doc)
    def _invoke(message: str) -> str:
        """Send a natural-language request to the remote A2A agent skill."""
        try:
            return _send_a2a_message(
                endpoint_url=_endpoint,
                message=message,
                allowed_skill_ids=_allowed,
                token_provider=_token,
                user_token=_user_token,
            )
        except Exception as e:
            logger.warning("A2A call %s failed: %s", tool_name, e)
            return f"A2A agent call failed: {e}"

    return _invoke


# ----------------------------------------------------------------------------
# JSON-RPC message/send transport
# ----------------------------------------------------------------------------

def _send_a2a_message(
    endpoint_url: str,
    message: str,
    allowed_skill_ids: list[str],
    token_provider: Callable[[], str],
    user_token: str | None = None,
) -> str:
    """Send one A2A ``message/send`` and collect the reply text.

    Uses a2a-sdk's ClientFactory with streaming=False so the server emits a
    single Task with artifacts — we extract the agent_response text from
    artifacts[0].parts and return it to the LLM.
    """
    import httpx
    from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
    from a2a.types import Message, Part, Role, TextPart

    async def _run() -> str:
        headers = {
            "Authorization": f"Bearer {token_provider()}",
            "X-A2A-Allowed-Skills": ",".join(allowed_skill_ids),
        }
        if user_token:
            # Forward the end user so the sub-agent can act as them. It arrives
            # here already validated by the Runtime, and the sub-agent verifies
            # it again independently rather than trusting this hop.
            token = user_token
            if token.lower().startswith("bearer "):
                token = token.split(" ", 1)[1].strip()
            headers["X-SuperApp-User-Token"] = token
        async with httpx.AsyncClient(headers=headers, timeout=60) as http:
            resolver = A2ACardResolver(http, endpoint_url)
            card = await resolver.get_agent_card()
            # Server card.url may be different from the invocation URL we got
            # from the Registry; pin the URL we were granted so SigV4-style
            # edges (AgentCore Runtime /invocations/) receive the right path.
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

    return asyncio.run(_run())
