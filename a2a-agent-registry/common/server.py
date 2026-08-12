"""A2A server entrypoint for the sample agents (Strands A2AServer on port 9000).

Usage — the generated ``main.py`` calls this:

    from common.server import run_agent
    run_agent(
        system_prompt_path="energy-optimization/system_prompt.md",
        card_json_path="energy-optimization/card.json",
    )

An agent that needs tools passes a factory instead of a list:

    from common.server import run_agent
    from device_control.tools import build_tools
    run_agent(..., tools_factory=build_tools)

AgentCore Runtime A2A contract (from AWS docs):
  - Port 9000
  - Mounted at ``/`` (not ``/invocations``)
  - Exposes AgentCard at ``/.well-known/agent-card.json``
  - Reads ``AGENTCORE_RUNTIME_URL`` env var for the card.url

Why tools come from a factory and not a list
--------------------------------------------
A tool that reaches a per-user backend has to carry that user's identity, and the
identity is per REQUEST. Building the tools once at startup and reusing them
would pin whichever user happened to arrive first, and every later request would
act as them — a silent cross-user escalation that looks like a working agent. So
the tools (and the Strands Agent holding them) are rebuilt per request from the
verified caller identity. Agent construction measures at well under a
millisecond, so this costs nothing next to an LLM call.

Authorization comes from a signed claim
--------------------------------------
A grant is a Cognito group (``a2a-<agent>.<skill>``, see ``common/a2a_groups.py``)
on the end user's own token. Two checks, in this order:

  1. This Runtime's ``customJWTAuthorizer.customClaims`` matches ``cognito:groups``
     with ``CONTAINS_ANY`` over every group of this agent, so AgentCore refuses a
     caller with no grant on this agent *before* the container is reached.
  2. This module derives the skill subset from the same claim, after verifying the
     token itself.

The previous design read ``X-A2A-Allowed-Skills``, a header the *client* set. That
made the grant client-asserted: the server could only refuse a skill the caller had
already declined to claim, so anything holding the shared m2m token could widen its
own access simply by sending a longer header. A claim signed by Cognito cannot be
widened by the caller, and it is checked by the platform rather than by us.

Rollout note (remove once all eight agents run the claim path)
-------------------------------------------------------------
``resolve_caller`` still accepts the old two-token shape, because the orchestrator
switches to sending the user token in ``Authorization`` in one step for all agents,
so every agent has to accept both before any of them can rely on the new one. The
legacy branch logs at warning so the migration's tail is visible rather than
becoming permanent. Deleting it is a three-line change plus the m2m client id
leaving ``allowedClients``.
"""

from __future__ import annotations

import json
import logging
import os
import uvicorn
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Import via the package so a stale copy on sys.path can't shadow these.
from common.agents import ALLOWED_SKILLS_HEADER, USER_TOKEN_HEADER
from common.governed_prompt import resolve_system_prompt

# The skill ids this agent publishes, read from card.json at startup. Module-level
# because the request path reads it and there is no way to thread an argument
# through the Strands/A2A executor internals.
_SKILL_IDS: frozenset[str] = frozenset()

# This agent's AgentCard name, which grant group names are keyed on. Set from
# card.json at startup alongside _SKILL_IDS, for the same reason: the request path
# needs it and there is no way to thread an argument through the executor.
_AGENT_NAME: str = ""


class CallerIdentity:
    """What this request proved about its caller.

    ``sub`` and ``email`` come from a fully verified idToken, so a tool can use
    them to scope a backend call. ``raw_token`` is the token itself, which the
    Gateway needs as a Bearer credential so Cedar sees the real end user.

    Deliberately not a dataclass with a generated repr: ``raw_token`` is a live
    user credential, and a default repr would put it into any log line that
    formats the object.
    """

    __slots__ = ("sub", "email", "raw_token", "allowed_skills")

    def __init__(self, sub: str, email: str, raw_token: str,
                 allowed_skills: frozenset[str]):
        self.sub = sub
        self.email = email
        self.raw_token = raw_token
        self.allowed_skills = allowed_skills

    @property
    def memory_actor(self) -> str:
        """This caller's AgentCore Memory actor id, or "" if it cannot be formed.

        Delegates to `shared/memory_actor.py` rather than sanitizing here. The
        orchestrator has always keyed memory by email, so a sub-agent that used
        `sub` would read a different namespace and quietly retrieve nothing — see
        that module. `sub` stays the key for scene rows and the runner's
        per-user credential; those are records, not a shared namespace.
        """
        from common.memory import memory_actor_for

        return memory_actor_for(self)

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return (f"CallerIdentity(sub={self.sub[:8] + '...' if self.sub else None!r}, "
                f"email={self.email!r}, raw_token=<redacted>, "
                f"allowed_skills={sorted(self.allowed_skills)})")


def _headers_from_context(a2a_context) -> dict[str, str]:
    """Pull the HTTP headers out of an A2A RequestContext, lower-cased.

    a2a-sdk's DefaultCallContextBuilder copies the Starlette request headers into
    ``call_context.state['headers']``, which is the only route from the transport
    to the executor. Returns {} rather than raising if the shape ever changes —
    the caller treats "no headers" as "no identity", which fails closed.
    """
    try:
        call_context = getattr(a2a_context, "call_context", None)
        state = getattr(call_context, "state", None) or {}
        headers = state.get("headers") or {}
        return {str(k).lower(): v for k, v in dict(headers).items()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read request headers from A2A context: %s", exc)
        return {}


def _parse_allowed_skills(headers: dict[str, str]) -> frozenset[str]:
    raw = headers.get(ALLOWED_SKILLS_HEADER.lower(), "")
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def _request_text(a2a_context) -> str:
    """The delegated request as plain text, or "".

    Used as the search query for memory retrieval, so relevance ranking has
    something to rank against. `get_user_input` is the a2a-sdk's own accessor and
    already joins multiple text parts; returns "" rather than raising if the
    request carries no text at all, which the caller treats as "no memory lookup".
    """
    try:
        return a2a_context.get_user_input() or ""
    except Exception as exc:  # noqa: BLE001
        logger.info("could not read the request text: %s", exc)
        return ""


def enforce_allowed_skills(allowed: frozenset[str], skill_ids: frozenset[str]) -> None:
    """Refuse a request that was not granted any of this agent's skills.

    Coarser than per-invocation skill matching, because an A2A message is natural
    language and carries no skill id — there is nothing to match it against. What
    this does establish is that the caller holds a grant for this agent at all,
    which is what turns the Admin Console's per-skill checkboxes from decoration
    into a server-side control.

    A caller that sends no header is refused rather than waved through: an
    unauthenticated omission must not be more permissive than an explicit grant.
    """
    if not skill_ids:
        return  # agent publishes no skills; nothing to gate
    if not allowed:
        raise PermissionError(
            f"{ALLOWED_SKILLS_HEADER} is missing or empty — this agent requires an "
            f"explicit skill grant. Expected one of: {sorted(skill_ids)}"
        )
    overlap = allowed & skill_ids
    if not overlap:
        raise PermissionError(
            f"none of the granted skills {sorted(allowed)} are published by this "
            f"agent (which publishes {sorted(skill_ids)})"
        )


GROUPS_CLAIM = "cognito:groups"


def _agent_name() -> str:
    """This agent's AgentCard name, which is what group names are keyed on."""
    return _AGENT_NAME


def _strip_bearer(raw: str) -> str:
    raw = (raw or "").strip()
    return raw.split(" ", 1)[1].strip() if raw.lower().startswith("bearer ") else raw


def skills_from_claims(claims: dict, agent_name: str) -> frozenset[str]:
    """The skills this token grants on this agent, from ``cognito:groups``.

    Empty means no grant, which the caller turns into a refusal. Deliberately not
    "empty means unrestricted": that inversion is the one bug in an authorization
    path that nobody notices, because everything keeps working.
    """
    from common import a2a_groups

    if not agent_name:
        # Without a name there is nothing to match groups against, and treating
        # that as "all skills" would turn a startup problem into an open door.
        # `serve` refuses to start without it, so reaching this means the request
        # path is running against a module that was never initialised.
        raise PermissionError(
            "this agent does not know its own card name, so it cannot tell which "
            "grants apply to it and refuses rather than guessing")
    return a2a_groups.skills_for_agent(claims.get(GROUPS_CLAIM) or [], agent_name)


def resolve_caller(a2a_context, require_user_identity: bool) -> CallerIdentity:
    """Verify the request's identity and skill grant. Raises on refusal.

    Primary path: ``Authorization`` carries the end user's own token. It has already
    been validated by this Runtime's authorizer, including the ``cognito:groups``
    match that proves a grant on this agent exists, but it is verified again here —
    the same reasoning that has always applied to a forwarded token, and it is how
    we get the claims to derive the skill subset from.

    Legacy path (temporary, see the module docstring): the old two-token shape,
    where ``Authorization`` was an m2m token with no ``sub`` and the grant arrived
    in a client-set header.
    """
    headers = _headers_from_context(a2a_context)
    from common.user_identity import UserTokenError, verify_user_token

    bearer = _strip_bearer(headers.get("authorization", ""))
    if bearer:
        try:
            claims = verify_user_token(bearer)
        except UserTokenError as exc:
            claims = None
            # Not fatal by itself: during the migration this is what an m2m token
            # looks like here, and the legacy branch below handles it. Logged
            # without the token.
            logger.info("Authorization did not verify as a user token: %s", exc)
        if claims is not None:
            allowed = skills_from_claims(claims, _agent_name())
            enforce_allowed_skills(allowed, _SKILL_IDS)
            return CallerIdentity(
                sub=claims["sub"],
                email=claims.get("email", ""),
                raw_token=bearer,
                allowed_skills=allowed,
            )

    # ---- legacy two-token path; delete with the m2m client id ----
    legacy_allowed = _parse_allowed_skills(headers)
    raw = headers.get(USER_TOKEN_HEADER.lower(), "")
    if legacy_allowed or raw:
        logger.warning(
            "legacy A2A auth path used (client-asserted %s + %s). The caller has "
            "not been migrated to Cognito group claims.",
            ALLOWED_SKILLS_HEADER, USER_TOKEN_HEADER)
    enforce_allowed_skills(legacy_allowed, _SKILL_IDS)

    if not raw:
        if require_user_identity:
            raise PermissionError(
                f"{USER_TOKEN_HEADER} is missing — this agent acts on a user's "
                f"devices and cannot do so without a verified user identity"
            )
        return CallerIdentity(sub="", email="", raw_token="",
                              allowed_skills=legacy_allowed)

    try:
        claims = verify_user_token(raw)
    except UserTokenError as exc:
        # Log the reason, never the token.
        logger.info("forwarded user token rejected: %s", exc)
        raise PermissionError(f"user token rejected: {exc}") from exc

    return CallerIdentity(
        sub=claims["sub"],
        email=claims.get("email", ""),
        raw_token=_strip_bearer(raw),
        allowed_skills=legacy_allowed,
    )


def _build_strands_agent(system_prompt: str, model_id: str, name: str,
                         description: str, tools: list | None = None):
    from strands import Agent
    from strands.models.bedrock import BedrockModel

    # No prompt caching here, deliberately — see agent/agent.py, where the
    # orchestrator DOES enable it. Two measurements decided it:
    #
    #   - A specialist's prefix is small. Measured across four deployed agents:
    #     362 to 4,211 mean input tokens per call, with minima as low as 71. Cache
    #     checkpoints have model-specific minimums (1,024 tokens for Claude 3.7
    #     Sonnet, per the AWS docs), and a prefix under the minimum caches
    #     nothing: a 2,817-token Haiku call with a cache point returned
    #     cacheRead=0, cacheWrite=0 — the checkpoint was silently ignored.
    #   - The prefix is not stable anyway. `execute` appends the admin's governed
    #     override and then the user's retrieved memory to this prompt, and the
    #     memory section differs per request. Cache hits need an EXACT prefix
    #     match, so each request would write a new entry and read none.
    #
    # Together that is the bad case: cache writes are billed at 1.25x the uncached
    # rate, so enabling it here would cost 25% MORE per delegation for zero hits.
    # The orchestrator is the opposite case — a ~10.5k byte-identical prefix — and
    # gets a 98% token reduction from the same feature.
    model = BedrockModel(
        model_id=model_id,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    return Agent(
        name=name,
        description=description,
        model=model,
        system_prompt=system_prompt,
        tools=tools or [],
    )


def _build_skills(card_dict: dict[str, Any]):
    from a2a.types import AgentSkill
    return [
        AgentSkill(
            id=s["id"],
            name=s.get("name", s["id"]),
            description=s.get("description", ""),
            tags=list(s.get("tags") or []),
            examples=list(s.get("examples") or []),
        )
        for s in card_dict.get("skills", [])
    ]


class _MarkedResult:
    """An AgentResult whose ``str()`` carries the routing marker."""

    __slots__ = ("_inner", "_marker")

    def __init__(self, inner, marker: str):
        self._inner = inner
        self._marker = marker

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __str__(self) -> str:
        text = str(self._inner) if self._inner is not None else ""
        if text.lstrip().startswith(self._marker):
            return text  # the model already emitted it; don't double it
        return f"{self._marker}\n\n{text}"


def _marker_prefixing_agent(agent, marker: str):
    """Wrap an Agent so its final result carries `marker`.

    The marker is a routing assertion — the caller reads it to confirm the request
    reached the intended specialist — so it should not depend on the model
    choosing to comply. Measured: the same model, same instruction, emits it
    reliably when answering from the prompt and drops it after a tool call, where
    the last thing in its context is a tool result to summarise rather than the
    system prompt. Instructing harder moved nothing.

    The marker goes on the RESULT, not the stream. With
    ``enable_a2a_compliant_streaming=False`` — what A2AServer defaults to, and what
    is in use here — the executor builds the client-visible artifact from
    ``str(result)`` and the streamed ``data`` events only drive interim status
    updates. Our client reads ``artifacts[0].parts``, so a prefix injected into the
    stream would never reach it.

    Only agents with tools are wrapped, so the three prompt-only agents' output is
    byte-for-byte what it was.
    """
    class _MarkerAgent:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def stream_async(self, *args, **kwargs):
            async for event in self._inner.stream_async(*args, **kwargs):
                if isinstance(event, dict) and "result" in event:
                    yield {**event, "result": _MarkedResult(event["result"], marker)}
                else:
                    yield event

    return _MarkerAgent(agent)


def _make_per_request_executor(base_executor_cls, agent_kwargs: dict,
                               tools_factory, require_user_identity: bool,
                               marker: str = ""):
    """Subclass the Strands executor so each request gets its own Agent.

    Strands builds one Agent at startup and the executor streams from it. That is
    right for a prompt-only agent and wrong for one with per-user tools, so this
    overrides the execution entry point to construct the Agent for the request
    from the caller's verified identity.
    """

    class PerRequestExecutor(base_executor_cls):  # type: ignore[misc, valid-type]
        async def execute(self, context, event_queue):
            try:
                caller = resolve_caller(context, require_user_identity)
            except PermissionError as exc:
                # Answer as the agent rather than raising: the orchestrator shows
                # the tool result to its own model, and a refusal it can read and
                # relay beats an opaque 500.
                await self._refuse(context, event_queue, str(exc))
                return

            try:
                tools = list(tools_factory(caller) or []) if tools_factory else []
            except Exception as exc:  # noqa: BLE001
                logger.exception("tools factory failed")
                await self._refuse(context, event_queue,
                                   f"could not prepare tools: {exc}")
                return

            # The admin-governed prompt, read per request. Not cached: the whole
            # point of governance is that an edit takes effect, and a TTL here
            # would make a saved prompt look like it had been ignored for as long
            # as the TTL lasted. A DynamoDB GetItem is nothing beside the LLM call
            # that follows, and the orchestrator reads its own prompt per request
            # for the same reason.
            kwargs = dict(agent_kwargs)
            kwargs["system_prompt"] = resolve_system_prompt(
                agent_kwargs["name"], agent_kwargs["system_prompt"],
                user_id=caller.sub,
            )

            # The user's long-term memory, shared with the orchestrator and read
            # only (common/memory.py explains why writing stays upstream).
            # Appended AFTER the governed prompt rather than folded into it: an
            # admin's global override replaces the shipped prompt entirely, and
            # the user's own remembered preferences should survive an operator
            # retuning this specialist's instructions.
            #
            # Retrieved with the request text as the query, so relevance ranking
            # has something to rank against; "" when there is nothing to add, in
            # which case the prompt is byte-for-byte what it was before.
            try:
                from common.memory import memory_prompt_section

                section = memory_prompt_section(caller, _request_text(context))
            except Exception as exc:  # noqa: BLE001
                # Memory is an enhancement. A specialist that refused to answer
                # because a retrieval failed would trade a good answer for none.
                logger.info("memory section unavailable: %s", exc)
                section = ""
            if section:
                kwargs["system_prompt"] += section

            # Swap in a request-scoped Agent for the duration of this call. The
            # executor instance is shared, so this must not outlive the request.
            previous = self.agent
            request_agent = _build_strands_agent(tools=tools, **kwargs)
            if marker:
                request_agent = _marker_prefixing_agent(request_agent, marker)
            self.agent = request_agent
            try:
                await super().execute(context, event_queue)
            finally:
                self.agent = previous

        async def _refuse(self, context, event_queue, reason: str) -> None:
            from a2a.utils import new_agent_text_message

            logger.info("A2A request refused: %s", reason)
            await event_queue.enqueue_event(
                new_agent_text_message(f"Request refused: {reason}")
            )

    return PerRequestExecutor


def _marker_for(card_dict: dict) -> str:
    """The routing marker for an agent, derived from its card name.

    `device-control-agent` -> `⟦A2A:device-control⟧`, matching the convention the
    prompt-only agents already emit themselves. Derived rather than configured so
    the marker cannot drift from the agent it identifies.
    """
    name = card_dict.get("name", "")
    if not name:
        return ""
    domain = name[: -len("-agent")] if name.endswith("-agent") else name
    return f"⟦A2A:{domain}⟧"


def run_agent(system_prompt_path: str, card_json_path: str, port: int = 9000,
              tools_factory: Callable[[CallerIdentity], list] | None = None,
              require_user_identity: bool | None = None) -> None:
    """Load config files and start the A2A server. Blocks until killed.

    ``tools_factory`` is called once per request with the verified
    ``CallerIdentity`` and returns that request's tools. Omit it for a
    prompt-only agent, which is exactly what the three original agents do — they
    keep the previous single-Agent path untouched.

    ``require_user_identity`` defaults to True whenever a tools factory is given:
    a tool-using agent reaches a per-user backend, and running one without a
    verified user is how cross-user access happens.
    """
    global _SKILL_IDS, _AGENT_NAME

    logging.basicConfig(level=logging.INFO)

    sp = Path(system_prompt_path)
    cj = Path(card_json_path)
    if not sp.is_absolute() or not cj.is_absolute():
        base = Path(__file__).resolve().parent.parent
        if not sp.is_absolute():
            sp = base / sp
        if not cj.is_absolute():
            cj = base / cj

    card_dict = json.loads(cj.read_text(encoding="utf-8"))
    system_prompt = sp.read_text(encoding="utf-8")
    model_id = (
        os.environ.get("MODEL_ID")
        or card_dict.get("defaultModelId")
        or "us.amazon.nova-lite-v1:0"
    )
    runtime_url = os.environ.get("AGENTCORE_RUNTIME_URL", f"http://127.0.0.1:{port}/")

    _SKILL_IDS = frozenset(
        s["id"] for s in (card_dict.get("skills") or []) if s.get("id")
    )
    # The card's `name` is what grant group names are keyed on, and the admin
    # console derives them from the same field on the Registry record. Read from
    # the card rather than an env var precisely because env vars get stripped by
    # deploys here (§9.2) — an agent that lost its name would refuse every request.
    _AGENT_NAME = card_dict.get("name", "")
    if not _AGENT_NAME:
        raise RuntimeError(
            f"{cj} has no `name`; grant groups are keyed on it and this agent "
            "would refuse every request")

    if require_user_identity is None:
        require_user_identity = tools_factory is not None

    agent_kwargs = dict(
        system_prompt=system_prompt,
        model_id=model_id,
        name=card_dict["name"],
        description=card_dict.get("description", ""),
    )

    # The startup Agent. With a tools factory it is a template the per-request
    # executor replaces; without one it serves every request, unchanged from
    # before this file grew tool support.
    strands_agent = _build_strands_agent(**agent_kwargs)

    from strands.multiagent.a2a import A2AServer

    a2a_server = A2AServer(
        agent=strands_agent,
        http_url=runtime_url,
        serve_at_root=True,
        skills=_build_skills(card_dict),
        version=card_dict.get("version", "1.0.0"),
    )

    # Wrap the executor for every agent, tools or not: skill enforcement has to
    # apply to all of them, because without it the shared m2m token is a master
    # key to every skill on every sub-agent. With no tools factory the wrapper
    # rebuilds an identical tool-free Agent, so the three original agents behave
    # as they did.
    from strands.multiagent.a2a.executor import StrandsA2AExecutor

    # Prefix the routing marker for EVERY agent. It used to be tool-using agents
    # only, on the grounds that a prompt-only agent emits its own reliably from
    # its system_prompt.md — which was true until that prompt became admin-editable.
    # A global override REPLACES the shipped prompt, so the instruction to emit the
    # marker goes with it, and the reply arrives unattributed: the orchestrator
    # cannot say which specialist answered, and nothing errors. Expecting an admin
    # to know they must reproduce a marker convention is not governance.
    #
    # Safe to apply unconditionally: _MarkedResult returns the text unchanged when
    # it already starts with the marker, so a model that still emits it is not
    # doubled.
    marker = _marker_for(card_dict)

    executor_cls = _make_per_request_executor(
        StrandsA2AExecutor, agent_kwargs, tools_factory, require_user_identity,
        marker=marker)
    a2a_server.request_handler.agent_executor = executor_cls(strands_agent)

    logger.info(
        "A2A agent %s ready — skills=%s tools=%s require_user_identity=%s marker=%s",
        card_dict["name"], sorted(_SKILL_IDS),
        "per-request factory" if tools_factory else "none", require_user_identity,
        marker or "(model-emitted)",
    )

    from fastapi import FastAPI
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"status": "healthy"}

    app.mount("/", a2a_server.to_fastapi_app())

    # Left exactly as it was. The CodeZip build has no /.dockerenv and sets no
    # DOCKER_CONTAINER, so this binds 127.0.0.1 in the deployed runtime — and the
    # deployed agents' own logs confirm "Uvicorn running on http://127.0.0.1:9000"
    # while serving A2A traffic normally, so the Runtime edge reaches them over
    # loopback. Widening the bind was proposed on the assumption it was broken; it
    # is not, and this phase changes authentication, which is not where an
    # unnecessary networking change belongs.
    host = "0.0.0.0" if (os.path.exists("/.dockerenv") or os.environ.get("DOCKER_CONTAINER")) else "127.0.0.1"
    uvicorn.run(app, host=host, port=port, log_level="info")
