"""Tests for the opt-in progress stream (spec 5 S5).

What S5 turned out to be, and why
---------------------------------
The spec proposed streaming the A2A hop through to the user, expecting TTFT to fall
from ~30s to single digits. Measured against `Agent.stream_async`, that is not
reachable:

    +0.00s  init_event_loop
    +1.88s  messageStart        <- first token of the turn
    +1.88s  tool_use_stream     <- and it is a TOOL CALL, naming the specialist
    +2.16s  message (toolUse complete)
    +8.13s  first text delta    <- the first PROSE, after the tool returned

A model cannot write its answer before the tool it just called returns, so
time-to-first-prose is bounded below by the specialist's own latency however the
A2A hop is transported. What IS available at 1.88s is the specialist's NAME — so
this streams tool lifecycle, which turns 31 seconds of motionless "thinking…" into
"asking the Home Security specialist…" about 6s earlier than any text could exist.

The invariants worth protecting are all about not breaking the non-streaming path,
because every other caller (voice, evals, the A/B arms) depends on it.
"""
import importlib.util
import json
import os

import pytest

_AGENT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent.py")


@pytest.fixture(scope="module")
def agent_mod():
    """agent.py loaded by path.

    `import agent` would resolve to the agent/ PACKAGE, not the module — the same
    reason test_agent_images.py does it this way.
    """
    spec = importlib.util.spec_from_file_location("agent_script", _AGENT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Ctx:
    session_id = "s" * 40
    request_headers: dict = {}


def _frames(gen) -> list[dict]:
    """Parse the generator's output into event dicts.

    Each yield is BARE JSON. BedrockAgentCoreApp adds the `data: ...\n\n` SSE
    framing itself, so framing it here too produced `data: data: {...}` on the
    wire — which decodes to the STRING 'data: {...}' and made every frame,
    including the answer, unparseable. Measured against the live runtime.
    """
    out = []
    for chunk in gen:
        assert not chunk.startswith("data:"), (
            f"the handler must not add SSE framing; AgentCore does: {chunk[:60]!r}")
        out.append(json.loads(chunk))
    return out


# ---------------------------------------------------------------------------
# Opt-in: the default shape must not change
# ---------------------------------------------------------------------------

def test_without_the_flag_the_reply_is_still_a_json_dict(agent_mod, monkeypatch):
    """The response TYPE changes when streaming, so it has to be opt-in.

    A generator makes BedrockAgentCoreApp emit text/event-stream, and any existing
    caller doing `response.json()` on that gets a parse error instead of a reply.
    """

    monkeypatch.setattr(agent_mod, "invoke_agent",
                        lambda *a, **kw: "the bedroom light is off")
    monkeypatch.setattr(agent_mod, "_record_session", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "_extract_user_auth", lambda ctx: None)

    out = agent_mod.handle_invocation({"prompt": "hi", "userId": "u@e.com"}, _Ctx())
    assert isinstance(out, dict)
    assert out == {"response": "the bedroom light is off", "status": "success"}


def test_with_the_flag_the_reply_is_a_generator(agent_mod, monkeypatch):
    import inspect

    monkeypatch.setattr(agent_mod, "invoke_agent", lambda *a, **kw: "ok")
    monkeypatch.setattr(agent_mod, "_record_session", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "_extract_user_auth", lambda ctx: None)

    out = agent_mod.handle_invocation(
        {"prompt": "hi", "userId": "u@e.com", "stream": True}, _Ctx())
    assert inspect.isgenerator(out), "a generator is what triggers SSE"


# ---------------------------------------------------------------------------
# Frame content
# ---------------------------------------------------------------------------

def test_each_tool_is_reported_then_the_answer(agent_mod, monkeypatch):

    def _fake_invoke(prompt, session_id=None, actor_id=None, auth_header=None,
                     headers=None, on_event=None, json_output=False):
        on_event("tool", "a2a_home_security_agent_risk_assessment")
        on_event("tool", "query_device_state")
        return "here is your answer"

    monkeypatch.setattr(agent_mod, "invoke_agent", _fake_invoke)
    monkeypatch.setattr(agent_mod, "_record_session", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "_extract_user_auth", lambda ctx: None)

    events = _frames(agent_mod.handle_invocation(
        {"prompt": "hi", "userId": "u@e.com", "stream": True}, _Ctx()))
    assert [e["type"] for e in events] == ["progress", "progress", "answer"]
    assert events[0]["tool"] == "a2a_home_security_agent_risk_assessment"
    assert events[-1]["response"] == "here is your answer"


def test_a_failure_still_sends_an_answer_frame(agent_mod, monkeypatch):
    """A stream that just stops leaves the UI waiting on a turn that will never end.

    The error text arrives in the terminal frame so the user sees something they
    can read instead of a spinner that never resolves.
    """

    def _boom(*a, **kw):
        raise RuntimeError("gateway exploded")

    monkeypatch.setattr(agent_mod, "invoke_agent", _boom)
    monkeypatch.setattr(agent_mod, "_record_session", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "_extract_user_auth", lambda ctx: None)

    events = _frames(agent_mod.handle_invocation(
        {"prompt": "hi", "userId": "u@e.com", "stream": True}, _Ctx()))
    assert events[-1]["type"] == "answer"
    assert "gateway exploded" in events[-1]["error"]


def test_a_turn_with_no_tools_yields_only_the_answer(agent_mod, monkeypatch):
    # The fast path stays a single frame — no spurious progress line for a turn
    # that never delegated.

    monkeypatch.setattr(agent_mod, "invoke_agent",
                        lambda *a, on_event=None, **kw: "quick reply")
    monkeypatch.setattr(agent_mod, "_record_session", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "_extract_user_auth", lambda ctx: None)

    events = _frames(agent_mod.handle_invocation(
        {"prompt": "hi", "userId": "u@e.com", "stream": True}, _Ctx()))
    assert [e["type"] for e in events] == ["answer"]


# ---------------------------------------------------------------------------
# The progress callback must never cost the turn
# ---------------------------------------------------------------------------

def test_a_raising_callback_does_not_break_the_run(agent_mod):
    """Progress is a nicety; the reply is the product."""

    def _bad(kind, detail):
        raise ValueError("UI blew up")

    # Must not propagate.
    agent_mod._safe_emit(_bad, "tool", "query_device_state")


def test_streaming_reports_each_tool_once(agent_mod, monkeypatch):
    """`current_tool_use` repeats while the arguments stream in.

    Without de-duplication the UI would flicker through a dozen identical
    "asking the …" lines for one delegation.
    """

    class _FakeAgent:
        async def stream_async(self, prompt):
            for _ in range(3):
                yield {"current_tool_use": {"name": "a2a_x_agent_y"}}
            yield {"current_tool_use": {"name": "control_device"}}
            yield {"result": "done"}

        def __call__(self, prompt):  # pragma: no cover - fallback path
            return "blocking"

    seen = []
    out = agent_mod._run_streamed(_FakeAgent(), "hi",
                                  lambda kind, detail: seen.append(detail))
    assert out == "done"
    assert seen == ["a2a_x_agent_y", "control_device"]


def test_a_streaming_failure_falls_back_to_a_blocking_call(agent_mod, monkeypatch):
    """Losing the answer to improve the waiting experience is the wrong trade."""

    class _BrokenAgent:
        async def stream_async(self, prompt):
            raise RuntimeError("stream unsupported")
            yield  # pragma: no cover - makes this an async generator

        def __call__(self, prompt):
            return "blocking answer"

    assert agent_mod._run_streamed(_BrokenAgent(), "hi", lambda *a: None) == \
        "blocking answer"
