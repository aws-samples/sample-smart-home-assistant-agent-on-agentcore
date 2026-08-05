import os
import sys
import pytest
from unittest.mock import patch, MagicMock, Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# agent.agent pulls in the whole Strands + AgentCore stack at import time, which
# these tests neither need nor want. They are stubbed via monkeypatch rather than
# assigned to sys.modules at module scope: a bare assignment never gets undone,
# so every test file collected after this one would import the Mock instead of
# the real package. tools/a2a.py imports `strands.tool` lazily for exactly this
# reason, and a leaked Mock there turns its @tool-decorated functions into Mock
# objects — test_a2a.py's 5 failures were this leak, not a bug in a2a.py.
_STUBBED_MODULES = (
    "strands",
    "strands.models",
    "strands.models.bedrock",
    "strands.tools",
    "strands.tools.mcp",
    "strands.tools.mcp.mcp_client",
    "strands.vended_plugins",
    "strands.vended_plugins.skills",
    "mcp",
    "mcp.client",
    "mcp.client.streamable_http",
    "bedrock_agentcore",
    "bedrock_agentcore.memory",
    "bedrock_agentcore.memory.integrations",
    "bedrock_agentcore.memory.integrations.strands",
    "bedrock_agentcore.memory.integrations.strands.config",
    "bedrock_agentcore.memory.integrations.strands.session_manager",
)


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    for name in _STUBBED_MODULES:
        monkeypatch.setitem(sys.modules, name, Mock())
    # agent.agent captured the real modules when some earlier test imported it;
    # drop it so each test's importlib.reload() rebinds against the stubs above.
    monkeypatch.delitem(sys.modules, "agent.agent", raising=False)
    monkeypatch.delenv("ENABLE_BUNDLE_HOOK", raising=False)
    monkeypatch.setenv("SKILLS_TABLE_NAME", "smarthome-skills-test")
    yield


def test_default_runtime_reads_ddb_additive():
    """ENABLE_BUNDLE_HOOK unset → DDB additive resolution (existing behavior)."""
    import importlib
    import agent.agent as agent
    importlib.reload(agent)

    with patch.object(agent, "_get_dynamodb") as get_db:
        table = MagicMock()
        table.get_item.side_effect = [
            {"Item": {"promptBody": "global body"}},
            {"Item": {"promptBody": "user body"}},
        ]
        get_db.return_value.Table.return_value = table

        result = agent.load_system_prompt("alice@example.com", "text", headers=None)

    assert result == "global body\n\nuser body"


def test_bundles_runtime_returns_none(monkeypatch):
    """ENABLE_BUNDLE_HOOK=1 → load_system_prompt short-circuits to None,
    skipping DDB. Hook will be the only prompt source on this runtime."""
    monkeypatch.setenv("ENABLE_BUNDLE_HOOK", "1")
    import importlib
    import agent.agent as agent
    importlib.reload(agent)

    with patch.object(agent, "_get_dynamodb") as get_db:
        result = agent.load_system_prompt("alice@example.com", "text", headers={"baggage": "bundle-arn=foo"})

    assert result is None
    get_db.assert_not_called()


def test_bundles_runtime_ignores_headers(monkeypatch):
    """Even with baggage headers, ENABLE_BUNDLE_HOOK=1 must NOT call
    bundle_config.load_from_baggage in load_system_prompt — that is the
    hook's job, not this function's."""
    monkeypatch.setenv("ENABLE_BUNDLE_HOOK", "1")
    import importlib
    import agent.agent as agent
    importlib.reload(agent)

    with patch.object(agent, "_get_dynamodb") as get_db, \
         patch("bundle_config.load_from_baggage") as lfb:
        result = agent.load_system_prompt("alice@example.com", "text", headers={"baggage": "x=y"})

    assert result is None
    lfb.assert_not_called()
    get_db.assert_not_called()


def test_create_agent_registers_hook_when_env_set(monkeypatch):
    """ENABLE_BUNDLE_HOOK=1 → create_agent() registers the bundle hook."""
    monkeypatch.setenv("ENABLE_BUNDLE_HOOK", "1")
    import importlib
    import agent.agent as agent
    importlib.reload(agent)

    with patch("bundle_config.register_before_model_call_hook") as reg:
        agent.create_agent(headers={"baggage": "x=y"})

    assert reg.called


def test_create_agent_does_not_register_hook_when_env_unset():
    """Default runtime never registers the hook."""
    import importlib
    import agent.agent as agent
    importlib.reload(agent)

    with patch("bundle_config.register_before_model_call_hook") as reg:
        agent.create_agent(headers={"baggage": "x=y"})

    reg.assert_not_called()
