"""Tests for the tool -> Cedar action name resolution in admin-api/index.py.

The Cedar action for a gateway tool is `{TargetName}___{toolName}`, and the map
that produces it is cached for the life of the container. That combination used
to fail silently: register a new gateway target, and any warm container still
holding the old map fell back to the BARE tool name. The resulting policy is
ACTIVE and well-formed, names an action the Gateway never emits, and therefore
permits nothing — a deny that looks exactly like a successful deploy.

These tests pin the two behaviours that prevent that: the map refreshes on a
miss, and an unresolvable tool raises instead of writing a bare-name policy.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

_mock_table = MagicMock()
_mock_ac = MagicMock()


@pytest.fixture(scope="module", autouse=True)
def _install_mocks():
    import os, sys, importlib
    lambda_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    if sys.path[0] != lambda_dir:
        sys.path.insert(0, lambda_dir)
    sys.modules.pop("index", None)

    with patch("boto3.resource") as mres, patch("boto3.client") as mclient:
        mres.return_value.Table.return_value = _mock_table
        mclient.side_effect = lambda name, **kw: _mock_ac
        import index  # noqa: F401
        importlib.reload(index)
        yield
        sys.modules.pop("index", None)


@pytest.fixture(autouse=True)
def _reset():
    import index
    _mock_table.reset_mock()
    _mock_ac.reset_mock()
    # Clear both caches so each test controls what the "container" already knows.
    index._get_tool_action_map._cache = None
    if hasattr(index._get_gateway_arn, "_cache"):
        del index._get_gateway_arn._cache


def _targets(mapping):
    """Make the mocked control plane describe `mapping` = {target: [tools]}."""
    _mock_ac.list_gateway_targets.return_value = {
        "items": [{"name": name, "targetId": f"tid-{name}"} for name in mapping]
    }

    def get_target(gatewayIdentifier, targetId):
        name = targetId.removeprefix("tid-")
        return {
            "targetConfiguration": {"mcp": {"lambda": {"toolSchema": {
                "inlinePayload": [{"name": t} for t in mapping[name]],
            }}}},
        }

    _mock_ac.get_gateway_target.side_effect = get_target
    _mock_ac.get_gateway.return_value = {"gatewayArn": "arn:aws:test:gateway/gw-1"}


def test_action_name_carries_the_target_prefix():
    import index
    _targets({"SmartHomeDeviceQuery": ["query_device_state"]})
    stmt = index.build_cedar_statement("query_device_state", ["alice"])
    assert 'AgentCore::Action::"SmartHomeDeviceQuery___query_device_state"' in stmt
    assert '(principal.id) == "alice"' in stmt


def test_a_miss_refreshes_the_cached_map():
    """A tool registered after the container warmed up must still resolve."""
    import index
    _targets({"SmartHomeDeviceControl": ["control_device"]})
    index._get_tool_action_map()  # warm the cache without the new target
    assert "navigate_to_page" not in index._get_tool_action_map._cache

    _targets({
        "SmartHomeDeviceControl": ["control_device"],
        "SmartHomeNavigation": ["navigate_to_page"],
    })
    stmt = index.build_cedar_statement("navigate_to_page", ["alice"])
    assert 'AgentCore::Action::"SmartHomeNavigation___navigate_to_page"' in stmt


def test_unknown_tool_raises_rather_than_writing_a_bare_name_policy():
    """The old fallback produced a policy that permitted nothing, silently."""
    import index
    _targets({"SmartHomeDeviceControl": ["control_device"]})
    with pytest.raises(ValueError, match="not exposed by any gateway target"):
        index.build_cedar_statement("tool_that_does_not_exist", ["alice"])


def test_no_users_yields_no_permit_so_default_deny_applies():
    import index
    _targets({"SmartHomeNavigation": ["navigate_to_page"]})
    assert index.build_cedar_statement("navigate_to_page", []) == ""


def test_refresh_is_not_forced_on_every_call():
    """The cache still has to work — a read path must not re-list the targets."""
    import index
    _targets({"SmartHomeNavigation": ["navigate_to_page"]})
    index._get_tool_action_map()
    calls_after_warm = _mock_ac.list_gateway_targets.call_count
    index._get_tool_action_map()
    index._get_tool_action_map()
    assert _mock_ac.list_gateway_targets.call_count == calls_after_warm


def test_update_waits_for_a_settling_policy():
    """UpdatePolicy rejects with ConflictException while a previous edit is still
    UPDATING, which used to leave DynamoDB permitting a tool that Cedar denied."""
    import index
    _targets({"SmartHomeNavigation": ["navigate_to_page"]})
    _mock_ac.get_policy.side_effect = [
        {"status": "UPDATING"},
        {"status": "ACTIVE"},
    ]
    _mock_table.get_item.side_effect = [
        # ensure_policy_engine
        {"Item": {"policyEngineId": "eng-1", "policyEngineArn": "arn:eng"}},
        # existing policy row for this tool
        {"Item": {"policyId": "pol-1"}},
    ]
    _mock_table.scan.return_value = {
        "Items": [{"userId": "alice", "allowedTools": ["navigate_to_page"]}],
    }
    _mock_ac.get_gateway.return_value = {
        "gatewayArn": "arn:aws:test:gateway/gw-1",
        "name": "gw", "roleArn": "arn:role", "protocolType": "MCP",
        "authorizerType": "CUSTOM_JWT",
        "policyEngineConfiguration": {"arn": "arn:eng"},
    }

    with patch.object(index.time, "sleep"):
        index.rebuild_tool_policy("navigate_to_page")

    # It polled until the policy settled, and only then issued the update.
    assert _mock_ac.get_policy.call_count == 2
    _mock_ac.update_policy.assert_called_once()
    _mock_table.get_item.side_effect = None


def test_s3_backed_tool_schema_still_resolves():
    """The older targets store their schema in S3 rather than inline."""
    import index
    _mock_ac.list_gateway_targets.return_value = {
        "items": [{"name": "SmartHomeDeviceDiscovery", "targetId": "tid-1"}]
    }
    _mock_ac.get_gateway_target.side_effect = None
    _mock_ac.get_gateway_target.return_value = {
        "targetConfiguration": {"mcp": {"lambda": {"toolSchema": {
            "s3": {"uri": "s3://bucket/schema.json"},
        }}}},
    }
    _mock_ac.get_gateway.return_value = {"gatewayArn": "arn:aws:test:gateway/gw-1"}
    body = MagicMock()
    body.read.return_value = json.dumps([{"name": "discover_devices"}]).encode()
    with patch.object(index, "s3_client") as s3:
        s3.get_object.return_value = {"Body": body}
        stmt = index.build_cedar_statement("discover_devices", ["alice"])
    assert 'AgentCore::Action::"SmartHomeDeviceDiscovery___discover_devices"' in stmt
