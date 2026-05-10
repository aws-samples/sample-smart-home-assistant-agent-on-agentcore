"""Tests for agent/tools/a2a.py — build_a2a_tools + AgentCard resolution.

Mock boundaries:
  * ``_fetch_agent_card_cached`` — monkeypatched to return a canned dict per
    recordId; the underlying boto3 client never runs.
  * ``_send_a2a_message`` — monkeypatched so we don't open httpx connections.
  * ``strands.tool`` — we use the real decorator so the Strands tool object
    shape (name, signature) matches production.
"""
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("REGISTRY_ID", "test-registry")


CARD_ENERGY = {
    "protocolVersion": "0.3.0",
    "name": "energy-optimization-agent",
    "url": "https://runtime/energy/invocations/",
    "version": "1.0.0",
    "skills": [
        {"id": "estimate_savings", "name": "Estimate", "description": "Estimate savings.",
         "examples": ["How much can I save dimming LEDs?"]},
        {"id": "tariff_analysis", "name": "Tariff", "description": "Tariff analysis.",
         "examples": []},
    ],
}

CARD_SECURITY = {
    "protocolVersion": "0.3.0",
    "name": "home-security-agent",
    "url": "https://runtime/security/invocations/",
    "version": "1.0.0",
    "skills": [
        {"id": "risk_assessment", "name": "Risk", "description": "Risk assessment.",
         "examples": []},
    ],
}


def _patch_card_fetch(monkeypatch, cards):
    """Make fetch_agent_card return the given dict for each known recordId.

    Raises ValueError otherwise — lets us verify soft-fail behaviour.
    """
    from tools import a2a as a2a_mod

    def _fake(registry_id, record_id):
        if record_id not in cards:
            raise ValueError(f"unknown recordId {record_id}")
        return cards[record_id]

    monkeypatch.setattr(a2a_mod, "fetch_agent_card", _fake)


def _patch_send(monkeypatch, replies):
    """replies: dict[endpoint_url, callable(message, allowed_skill_ids) -> str]"""
    from tools import a2a as a2a_mod

    def _fake_send(endpoint_url, message, allowed_skill_ids, token_provider):
        # Ensure token_provider is callable (matches production contract).
        assert callable(token_provider)
        handler = replies.get(endpoint_url)
        if handler is None:
            raise RuntimeError(f"no stub for endpoint {endpoint_url}")
        return handler(message, allowed_skill_ids)

    monkeypatch.setattr(a2a_mod, "_send_a2a_message", _fake_send)


def test_empty_grants_returns_empty_list():
    from tools.a2a import build_a2a_tools
    assert build_a2a_tools({}, "rid", lambda: "tok") == []


def test_build_tools_one_per_granted_skill(monkeypatch):
    from tools.a2a import build_a2a_tools

    _patch_card_fetch(monkeypatch, {
        "rec-energy": CARD_ENERGY,
        "rec-security": CARD_SECURITY,
    })

    tools = build_a2a_tools(
        grants={
            "rec-energy": ["estimate_savings", "tariff_analysis"],
            "rec-security": ["risk_assessment"],
        },
        registry_id="test-registry",
        token_provider=lambda: "tok",
    )
    assert len(tools) == 3
    names = sorted(getattr(t, "tool_name", None) or getattr(t, "__name__", "") for t in tools)
    assert names == [
        "a2a_energy_optimization_agent_estimate_savings",
        "a2a_energy_optimization_agent_tariff_analysis",
        "a2a_home_security_agent_risk_assessment",
    ]


def test_build_tools_filters_unauthorized_skills(monkeypatch):
    from tools.a2a import build_a2a_tools

    _patch_card_fetch(monkeypatch, {"rec-energy": CARD_ENERGY})

    tools = build_a2a_tools(
        grants={"rec-energy": ["estimate_savings"]},  # tariff_analysis omitted
        registry_id="test-registry",
        token_provider=lambda: "tok",
    )
    assert len(tools) == 1
    name = getattr(tools[0], "tool_name", None) or getattr(tools[0], "__name__", "")
    assert name == "a2a_energy_optimization_agent_estimate_savings"


def test_registry_failure_is_soft(monkeypatch):
    """If fetch_agent_card raises, the tool is skipped — no exception out."""
    from tools import a2a as a2a_mod
    from tools.a2a import build_a2a_tools

    # rec-energy resolves fine, rec-missing blows up — we still get one tool.
    def _fake(registry_id, record_id):
        if record_id == "rec-energy":
            return CARD_ENERGY
        raise RuntimeError(f"boom {record_id}")

    monkeypatch.setattr(a2a_mod, "fetch_agent_card", _fake)

    tools = build_a2a_tools(
        grants={"rec-energy": ["estimate_savings"], "rec-missing": ["anything"]},
        registry_id="test-registry",
        token_provider=lambda: "tok",
    )
    assert len(tools) == 1


def test_tool_description_carries_skill_doc_and_examples(monkeypatch):
    from tools.a2a import build_a2a_tools

    _patch_card_fetch(monkeypatch, {"rec-energy": CARD_ENERGY})

    tools = build_a2a_tools(
        grants={"rec-energy": ["estimate_savings"]},
        registry_id="test-registry",
        token_provider=lambda: "tok",
    )
    tool = tools[0]
    # Strands tool carries the description on its spec
    spec = getattr(tool, "tool_spec", None) or {}
    desc = spec.get("description", "") if isinstance(spec, dict) else ""
    if not desc:
        # Fallback: check the wrapped callable's __doc__ or __description__.
        desc = getattr(tool, "__description__", "") or tool.__doc__ or ""
    assert "Estimate savings" in desc
    assert "How much can I save dimming LEDs?" in desc


def test_tool_invocation_soft_fails_on_send_error(monkeypatch):
    from tools import a2a as a2a_mod
    from tools.a2a import build_a2a_tools

    _patch_card_fetch(monkeypatch, {"rec-energy": CARD_ENERGY})

    def _raise(*a, **kw):
        raise RuntimeError("network boom")

    monkeypatch.setattr(a2a_mod, "_send_a2a_message", _raise)

    tools = build_a2a_tools(
        grants={"rec-energy": ["estimate_savings"]},
        registry_id="test-registry",
        token_provider=lambda: "tok",
    )
    # Invoke the underlying function (strands.tool wraps it but keeps the
    # callable). Strands tools are usually invoked via .invoke() or by the
    # strands runtime; for unit testing, test the wrapped function directly.
    wrapped = tools[0]
    # Find the raw Python function. Different strands versions expose it
    # differently; try common attributes.
    for attr in ("func", "_func", "callable", "__wrapped__"):
        fn = getattr(wrapped, attr, None)
        if callable(fn):
            out = fn("hi")
            break
    else:
        # Fall back to treating the decorator return value as callable.
        out = wrapped("hi")
    assert isinstance(out, str)
    assert out.startswith("A2A agent call failed:")


def test_tool_invocation_returns_remote_reply(monkeypatch):
    from tools import a2a as a2a_mod
    from tools.a2a import build_a2a_tools

    _patch_card_fetch(monkeypatch, {"rec-energy": CARD_ENERGY})
    calls = []

    def _fake_send(endpoint_url, message, allowed_skill_ids, token_provider):
        calls.append({
            "endpoint": endpoint_url,
            "message": message,
            "allowed": list(allowed_skill_ids),
            "token": token_provider(),
        })
        return f"⟦A2A⟧ echo: {message}"

    monkeypatch.setattr(a2a_mod, "_send_a2a_message", _fake_send)

    tools = build_a2a_tools(
        grants={"rec-energy": ["estimate_savings"]},
        registry_id="test-registry",
        token_provider=lambda: "tok-abc",
    )
    wrapped = tools[0]
    for attr in ("func", "_func", "callable", "__wrapped__"):
        fn = getattr(wrapped, attr, None)
        if callable(fn):
            out = fn("How much can I save?")
            break
    else:
        out = wrapped("How much can I save?")
    assert out.startswith("⟦A2A⟧")
    assert len(calls) == 1
    assert calls[0]["endpoint"] == CARD_ENERGY["url"]
    assert calls[0]["message"] == "How much can I save?"
    assert calls[0]["allowed"] == ["estimate_savings"]
    assert calls[0]["token"] == "tok-abc"
