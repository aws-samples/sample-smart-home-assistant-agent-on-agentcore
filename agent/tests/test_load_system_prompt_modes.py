import os
import sys
import pytest
from unittest.mock import patch, MagicMock, Mock

# Mock heavy dependencies before importing agent.agent
sys.modules["strands"] = Mock()
sys.modules["strands.models"] = Mock()
sys.modules["strands.models.bedrock"] = Mock()
sys.modules["strands.tools"] = Mock()
sys.modules["strands.tools.mcp"] = Mock()
sys.modules["strands.tools.mcp.mcp_client"] = Mock()
sys.modules["strands.vended_plugins"] = Mock()
sys.modules["strands.vended_plugins.skills"] = Mock()
sys.modules["mcp"] = Mock()
sys.modules["mcp.client"] = Mock()
sys.modules["mcp.client.streamable_http"] = Mock()
sys.modules["bedrock_agentcore"] = Mock()
sys.modules["bedrock_agentcore.memory"] = Mock()
sys.modules["bedrock_agentcore.memory.integrations"] = Mock()
sys.modules["bedrock_agentcore.memory.integrations.strands"] = Mock()
sys.modules["bedrock_agentcore.memory.integrations.strands.config"] = Mock()
sys.modules["bedrock_agentcore.memory.integrations.strands.session_manager"] = Mock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
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
