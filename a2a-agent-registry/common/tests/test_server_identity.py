"""Tests for the A2A server's identity and skill enforcement.

Three things here are load-bearing, and each was broken before:

  - `X-A2A-Allowed-Skills` was parsed and then ignored, so per-skill
    authorisation was purely client-side. Anything holding the shared m2m token
    could call any skill on any agent.
  - The forwarded user token has to be VERIFIED, not decoded. Trusting the
    payload would let a caller name any `sub` it liked.
  - Tools must be rebuilt per request. A tools list built once at startup pins
    the first caller's identity onto every later request.
"""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from common import server
from common.agents import ALLOWED_SKILLS_HEADER, USER_TOKEN_HEADER


def _context(headers: dict | None = None):
    """A stand-in for an a2a RequestContext carrying HTTP headers.

    Mirrors the real shape: a2a-sdk's DefaultCallContextBuilder puts the request
    headers into call_context.state['headers'].
    """
    ctx = MagicMock()
    ctx.call_context = MagicMock()
    ctx.call_context.state = {"headers": dict(headers or {})}
    return ctx


# --------------------------------------------------------------------------
# Skill enforcement
# --------------------------------------------------------------------------

def test_granted_skill_is_accepted():
    server.enforce_allowed_skills(frozenset({"estimate_savings"}),
                                  frozenset({"estimate_savings", "tariff_analysis"}))


def test_a_skill_this_agent_does_not_publish_is_refused():
    """Calling agent B's skill on agent A must not work just because the caller
    holds the shared m2m token."""
    with pytest.raises(PermissionError, match="none of the granted skills"):
        server.enforce_allowed_skills(frozenset({"risk_assessment"}),
                                      frozenset({"estimate_savings"}))


def test_a_missing_header_is_refused_not_waved_through():
    """Omission must not be more permissive than an explicit grant."""
    with pytest.raises(PermissionError, match="missing or empty"):
        server.enforce_allowed_skills(frozenset(), frozenset({"estimate_savings"}))


def test_an_agent_with_no_published_skills_is_not_gated():
    server.enforce_allowed_skills(frozenset(), frozenset())


def test_header_is_parsed_case_insensitively_and_trimmed():
    ctx = _context({ALLOWED_SKILLS_HEADER.upper(): " a , b ,, c "})
    headers = server._headers_from_context(ctx)
    assert server._parse_allowed_skills(headers) == frozenset({"a", "b", "c"})


def test_missing_call_context_yields_no_headers_and_so_fails_closed():
    ctx = MagicMock()
    ctx.call_context = None
    assert server._headers_from_context(ctx) == {}


# --------------------------------------------------------------------------
# Caller resolution
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _skills(monkeypatch):
    monkeypatch.setattr(server, "_SKILL_IDS", frozenset({"estimate_savings"}))


def test_prompt_only_agent_needs_no_user_token():
    """The three original agents are advisors — they touch no user data, so
    requiring an identity they never had would break them."""
    ctx = _context({ALLOWED_SKILLS_HEADER: "estimate_savings"})
    caller = server.resolve_caller(ctx, require_user_identity=False)
    assert caller.sub == "" and caller.raw_token == ""
    assert caller.allowed_skills == frozenset({"estimate_savings"})


def test_tool_using_agent_refuses_a_request_with_no_user_token():
    ctx = _context({ALLOWED_SKILLS_HEADER: "estimate_savings"})
    with pytest.raises(PermissionError, match=USER_TOKEN_HEADER):
        server.resolve_caller(ctx, require_user_identity=True)


def test_a_verified_token_yields_sub_and_email():
    ctx = _context({
        ALLOWED_SKILLS_HEADER: "estimate_savings",
        USER_TOKEN_HEADER: "Bearer header.payload.signature",
    })
    fake = types.ModuleType("common.user_identity")
    fake.UserTokenError = type("UserTokenError", (Exception,), {})
    fake.verify_user_token = lambda tok: {"sub": "sub-alice",
                                          "email": "alice@example.com"}
    with patch.dict(sys.modules, {"common.user_identity": fake}):
        caller = server.resolve_caller(ctx, require_user_identity=True)
    assert caller.sub == "sub-alice"
    assert caller.email == "alice@example.com"
    # Bearer prefix stripped, because the Gateway call re-adds it.
    assert caller.raw_token == "header.payload.signature"


def test_a_rejected_token_is_a_refusal_not_a_fallthrough():
    """If verification fails the request must stop — not continue unscoped."""
    ctx = _context({
        ALLOWED_SKILLS_HEADER: "estimate_savings",
        USER_TOKEN_HEADER: "aaa.bbb.ccc",
    })
    err = type("UserTokenError", (Exception,), {})

    def boom(tok):
        raise err("signature verification failed")

    fake = types.ModuleType("common.user_identity")
    fake.UserTokenError = err
    fake.verify_user_token = boom
    with patch.dict(sys.modules, {"common.user_identity": fake}):
        with pytest.raises(PermissionError, match="user token rejected"):
            server.resolve_caller(ctx, require_user_identity=True)


def test_skills_are_enforced_before_the_token_is_even_looked_at():
    """Cheapest check first, and it must not be skippable by omitting the token."""
    ctx = _context({USER_TOKEN_HEADER: "Bearer x.y.z"})
    with pytest.raises(PermissionError, match=ALLOWED_SKILLS_HEADER):
        server.resolve_caller(ctx, require_user_identity=True)


def test_identity_repr_never_exposes_the_token():
    """The sub-agent holds a live user credential; a repr in a log line would
    outlive the request by the log retention period."""
    ident = server.CallerIdentity(sub="sub-alice", email="a@b.c",
                                 raw_token="super.secret.token",
                                 allowed_skills=frozenset({"x"}))
    text = repr(ident)
    assert "super.secret.token" not in text
    assert "redacted" in text


# --------------------------------------------------------------------------
# Per-request tool construction
# --------------------------------------------------------------------------

def test_tools_factory_is_called_per_request_with_that_caller():
    """The core guarantee of the factory design: two callers in sequence must
    each get their own tools, never the first caller's."""
    seen = []

    class FakeBase:
        def __init__(self, agent):
            self.agent = agent
            self.agents_used = []

        async def execute(self, context, event_queue):
            self.agents_used.append(self.agent)

    def factory(caller):
        seen.append(caller.sub)
        return [f"tool-for-{caller.sub}"]

    cls = server._make_per_request_executor(
        FakeBase, dict(system_prompt="p", model_id="m", name="n", description="d"),
        factory, require_user_identity=True)
    ex = cls("startup-agent")

    fake = types.ModuleType("common.user_identity")
    fake.UserTokenError = type("UserTokenError", (Exception,), {})

    import asyncio
    for sub in ("sub-alice", "sub-bob"):
        fake.verify_user_token = lambda tok, _s=sub: {"sub": _s, "email": ""}
        ctx = _context({ALLOWED_SKILLS_HEADER: "estimate_savings",
                        USER_TOKEN_HEADER: "Bearer a.b.c"})
        with patch.dict(sys.modules, {"common.user_identity": fake}), \
             patch.object(server, "_build_strands_agent",
                          side_effect=lambda **kw: {"tools": kw["tools"]}):
            asyncio.run(ex.execute(ctx, MagicMock()))

    assert seen == ["sub-alice", "sub-bob"]
    # Each request ran against its OWN tools, and the startup agent is restored.
    assert ex.agents_used == [{"tools": ["tool-for-sub-alice"]},
                              {"tools": ["tool-for-sub-bob"]}]
    assert ex.agent == "startup-agent"


def test_a_refused_request_never_builds_tools_or_runs_the_agent():
    ran = []

    class FakeBase:
        def __init__(self, agent):
            self.agent = agent

        async def execute(self, context, event_queue):
            ran.append("executed")

    def factory(caller):
        ran.append("factory")
        return []

    cls = server._make_per_request_executor(
        FakeBase, dict(system_prompt="p", model_id="m", name="n", description="d"),
        factory, require_user_identity=True)
    ex = cls("startup-agent")

    import asyncio
    queue = MagicMock()
    sent = []

    async def enqueue(ev):
        sent.append(ev)

    queue.enqueue_event = enqueue
    # No skills header at all -> refused before anything else happens.
    asyncio.run(ex.execute(_context({}), queue))

    assert ran == []
    assert len(sent) == 1  # the caller gets a readable refusal, not a 500


def test_the_startup_agent_is_restored_even_when_execution_raises():
    """Otherwise one failing request leaves a later caller running against the
    previous caller's tools."""
    class FakeBase:
        def __init__(self, agent):
            self.agent = agent

        async def execute(self, context, event_queue):
            raise RuntimeError("model blew up")

    cls = server._make_per_request_executor(
        FakeBase, dict(system_prompt="p", model_id="m", name="n", description="d"),
        lambda caller: [], require_user_identity=False)
    ex = cls("startup-agent")

    import asyncio
    with patch.object(server, "_build_strands_agent",
                      side_effect=lambda **kw: "request-agent"):
        with pytest.raises(RuntimeError):
            asyncio.run(ex.execute(
                _context({ALLOWED_SKILLS_HEADER: "estimate_savings"}), MagicMock()))
    assert ex.agent == "startup-agent"
