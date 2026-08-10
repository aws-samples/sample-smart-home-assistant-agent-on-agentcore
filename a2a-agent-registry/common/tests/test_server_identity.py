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


# --------------------------------------------------------------------------
# Routing marker
#
# The caller reads a marker line to confirm the request reached the intended
# specialist. A prompt-only agent emits its own reliably; one that has just
# summarised a tool result drops it, so for tool-using agents the server adds it.
# --------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, text):
        self._text = text
        self.stop_reason = "end_turn"

    def __str__(self):
        return self._text


def _agent_yielding(text):
    class _A:
        async def stream_async(self, *a, **kw):
            yield {"data": text}
            yield {"result": _FakeResult(text)}
    return _A()


def _final_text(wrapped):
    import asyncio

    async def run():
        out = None
        async for event in wrapped.stream_async():
            if isinstance(event, dict) and "result" in event:
                out = str(event["result"])
        return out
    return asyncio.run(run())


def test_marker_is_added_when_the_model_omits_it():
    wrapped = server._marker_prefixing_agent(
        _agent_yielding("Temperature: 26.9C"), "⟦A2A:device-control⟧")
    assert _final_text(wrapped).startswith("⟦A2A:device-control⟧")


def test_marker_is_not_doubled_when_the_model_emits_it():
    wrapped = server._marker_prefixing_agent(
        _agent_yielding("⟦A2A:device-control⟧\n\nTemperature: 26.9C"),
        "⟦A2A:device-control⟧")
    assert _final_text(wrapped).count("⟦A2A:device-control⟧") == 1


def test_marker_goes_on_the_result_not_only_the_stream():
    """With enable_a2a_compliant_streaming=False the client-visible artifact is
    built from str(result); the streamed data events only drive status updates. A
    prefix injected into the stream alone would never reach the caller."""
    import asyncio

    wrapped = server._marker_prefixing_agent(
        _agent_yielding("plain"), "⟦A2A:device-control⟧")

    async def collect():
        data, result = [], None
        async for event in wrapped.stream_async():
            if "data" in event:
                data.append(event["data"])
            if "result" in event:
                result = str(event["result"])
        return data, result

    data, result = asyncio.run(collect())
    assert data == ["plain"]                       # stream left alone
    assert result.startswith("⟦A2A:device-control⟧")  # result carries it


def test_marked_result_passes_other_attributes_through():
    marked = server._MarkedResult(_FakeResult("x"), "M")
    assert marked.stop_reason == "end_turn"


def test_marker_is_derived_from_the_card_name():
    assert server._marker_for({"name": "device-control-agent"}) == "⟦A2A:device-control⟧"
    assert server._marker_for({"name": "home-security-agent"}) == "⟦A2A:home-security⟧"
    # Matches the convention the prompt-only agents already emit by hand.
    assert server._marker_for({"name": "nameless"}) == "⟦A2A:nameless⟧"
    assert server._marker_for({}) == ""


# ---------------------------------------------------------------------------
# The governed prompt reaches the request Agent
# ---------------------------------------------------------------------------

def test_the_request_agent_uses_the_governed_prompt():
    """The resolver is unit-tested separately; what this pins down is the WIRING.
    A resolver that is never called is indistinguishable from one that always
    returns the shipped prompt — the agent works, and every admin override is
    silently ignored."""
    class FakeBase:
        def __init__(self, agent):
            self.agent = agent

        async def execute(self, context, event_queue):
            pass

    cls = server._make_per_request_executor(
        FakeBase, dict(system_prompt="SHIPPED", model_id="m",
                       name="light-effect-agent", description="d"),
        lambda caller: [], require_user_identity=False)
    ex = cls("startup-agent")

    import asyncio
    built = {}
    with patch.object(server, "resolve_system_prompt",
                      side_effect=lambda name, shipped, user_id=None:
                          f"GOVERNED({name},{shipped},{user_id!r})") as resolver, \
         patch.object(server, "_build_strands_agent",
                      side_effect=lambda **kw: built.update(kw) or "agent"):
        # `estimate_savings` because _SKILL_IDS is module state that the earlier
        # tests set; the skill gate is not what this test is about.
        asyncio.run(ex.execute(_context({ALLOWED_SKILLS_HEADER: "estimate_savings"}),
                               MagicMock()))

    # Called with the card name and the shipped prompt, not with something derived
    # from the runtime name or the directory slug.
    resolver.assert_called_once()
    assert resolver.call_args.args == ("light-effect-agent", "SHIPPED")
    assert built["system_prompt"] == "GOVERNED(light-effect-agent,SHIPPED,'')"


def test_resolving_the_prompt_does_not_mutate_the_startup_kwargs():
    """agent_kwargs is shared across every request. Writing the resolved prompt
    into it would make request N+1 resolve against request N's result, so one
    user's addendum would leak into the next caller's prompt."""
    class FakeBase:
        def __init__(self, agent):
            self.agent = agent

        async def execute(self, context, event_queue):
            pass

    kwargs = dict(system_prompt="SHIPPED", model_id="m", name="n", description="d")
    cls = server._make_per_request_executor(
        FakeBase, kwargs, lambda caller: [], require_user_identity=False)
    ex = cls("startup-agent")

    import asyncio
    with patch.object(server, "resolve_system_prompt",
                      side_effect=lambda name, shipped, user_id=None: shipped + "+X"), \
         patch.object(server, "_build_strands_agent", side_effect=lambda **kw: "a"):
        for _ in range(3):
            asyncio.run(ex.execute(_context({ALLOWED_SKILLS_HEADER: "estimate_savings"}),
                                   MagicMock()))

    assert kwargs["system_prompt"] == "SHIPPED"


def test_the_marker_is_not_conditional_on_having_tools():
    """A prompt-only agent used to be trusted to emit its own marker, because the
    instruction to do so lived in its system_prompt.md. That prompt is now
    admin-editable, and a global override REPLACES it — so the instruction leaves
    with it and the reply arrives unattributed, with nothing raised. Verified
    against the deployed home-security agent before this was changed.

    Asserted on the source because `run_agent` binds uvicorn and cannot be called
    from a test; what matters is that the gate is gone, and _MarkedResult already
    makes unconditional prefixing safe (tested above).
    """
    import inspect

    src = inspect.getsource(server.run_agent)
    assert "marker = _marker_for(card_dict)" in src
    assert "_marker_for(card_dict) if tools_factory" not in src, (
        "the marker is gated on tools again — an admin who overrides a prompt-only "
        "agent's prompt will silently lose its routing marker")
