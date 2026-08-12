"""Tests for resolving a tool to its Cedar action AND to the gateway that holds it.

The Cedar action for a gateway tool is `{TargetName}___{toolName}`, and the map
that produces it is cached for the life of the container. That combination used
to fail silently: register a new gateway target, and any warm container still
holding the old map fell back to the BARE tool name. The resulting policy is
ACTIVE and well-formed, names an action the Gateway never emits, and therefore
permits nothing — a deny that looks exactly like a successful deploy.

Since 2026-08-12 there is a second failure of the same shape. The `web-search`
connector is not offered in us-west-2, so it sits on a gateway in us-east-1, and a
policy engine is regional. A policy for web search written to the us-west-2 engine
would also be ACTIVE, well-formed and completely inert. So "which region" is now
part of what resolving a tool has to answer, and it is returned alongside the
statement rather than looked up separately — two lookups can disagree.

These tests pin the behaviours that prevent both silent denies:
  - the map refreshes on a miss
  - an unresolvable tool raises instead of writing a bare-name policy
  - a connector target contributes tools at all (both old parsers read only
    `mcp.lambda`, so web search was invisible AND ungovernable)
  - a tool's policy is written to its own region's engine
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
    sys.modules.pop("gateway_catalog", None)

    with patch("boto3.resource") as mres, patch("boto3.client") as mclient:
        mres.return_value.Table.return_value = _mock_table
        mclient.side_effect = lambda name, **kw: _mock_ac
        import index  # noqa: F401
        importlib.reload(index)
        yield
        sys.modules.pop("index", None)
        sys.modules.pop("gateway_catalog", None)


@pytest.fixture(autouse=True)
def _reset():
    import gateway_catalog
    _mock_table.reset_mock()
    _mock_ac.reset_mock()
    _mock_ac.get_gateway_target.side_effect = None
    _mock_table.get_item.side_effect = None
    _mock_ac.get_policy.side_effect = None
    # Clear the caches so each test controls what the "container" already knows.
    gateway_catalog._catalog = None
    gateway_catalog._clients.clear()
    # One gateway unless a test says otherwise.
    gateway_catalog.GATEWAY_ID = "gw-tools"
    gateway_catalog.WEBSEARCH_GATEWAY_ID = ""
    gateway_catalog.WEBSEARCH_GATEWAY_REGION = "us-east-1"
    # `control()` is cached per region and the mock is shared, so re-seat it.
    gateway_catalog._clients["us-west-2"] = _mock_ac
    gateway_catalog._clients["us-east-1"] = _mock_ac


def _targets(mapping, connectors=None):
    """Make the mocked control plane describe `mapping` = {target: [tools]}.

    `connectors` = {target: configName} describes connector targets, which carry
    no tool schema at all.
    """
    connectors = connectors or {}
    names = list(mapping) + list(connectors)
    _mock_ac.list_gateway_targets.return_value = {
        "items": [{"name": n, "targetId": f"tid-{n}"} for n in names]
    }

    def get_target(gatewayIdentifier, targetId):
        name = targetId.removeprefix("tid-")
        if name in connectors:
            return {"targetConfiguration": {"mcp": {"connector": {
                "source": {"connectorId": "web-search", "version": "1.2.0"},
                "configurations": [{"name": connectors[name], "parameterValues": {}}],
            }}}}
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
    stmt, entry = index.build_cedar_statement("query_device_state", ["alice"])
    assert 'AgentCore::Action::"SmartHomeDeviceQuery___query_device_state"' in stmt
    assert '(principal.id) == "alice"' in stmt
    assert entry["region"] == "us-west-2"


def test_a_miss_refreshes_the_cached_map():
    """A tool registered after the container warmed up must still resolve."""
    import gateway_catalog
    import index
    _targets({"SmartHomeDeviceControl": ["control_device"]})
    gateway_catalog.catalog(index.s3_client)  # warm without the new target
    assert gateway_catalog.entry_for("navigate_to_page", index.s3_client) is None

    _targets({
        "SmartHomeDeviceControl": ["control_device"],
        "SmartHomeNavigation": ["navigate_to_page"],
    })
    stmt, _ = index.build_cedar_statement("navigate_to_page", ["alice"])
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
    stmt, _ = index.build_cedar_statement("navigate_to_page", [])
    assert stmt == ""


def test_refresh_is_not_forced_on_every_call():
    """The cache still has to work — a read path must not re-list the targets."""
    import gateway_catalog
    import index
    _targets({"SmartHomeNavigation": ["navigate_to_page"]})
    gateway_catalog.catalog(index.s3_client)
    calls_after_warm = _mock_ac.list_gateway_targets.call_count
    gateway_catalog.catalog(index.s3_client)
    gateway_catalog.catalog(index.s3_client)
    assert _mock_ac.list_gateway_targets.call_count == calls_after_warm


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
        stmt, _ = index.build_cedar_statement("discover_devices", ["alice"])
    assert 'AgentCore::Action::"SmartHomeDeviceDiscovery___discover_devices"' in stmt


# --- connector targets ------------------------------------------------------

def test_a_connector_target_contributes_a_tool():
    """Both old parsers read only `mcp.lambda`, so web search was invisible to the
    Tool Policy page AND unnameable by Cedar — an ungovernable tool that looks
    like a tool nobody granted."""
    import index
    _targets({}, connectors={"SmartHomeWebSearch": "WebSearch"})
    stmt, entry = index.build_cedar_statement("WebSearch", ["alice"])
    assert 'AgentCore::Action::"SmartHomeWebSearch___WebSearch"' in stmt
    assert entry["targetKind"] == "connector"


def test_connector_tool_name_matches_what_the_gateway_advertises():
    """Confirmed against a live tools/list: SmartHomeWebSearch___WebSearch."""
    import gateway_catalog
    import index
    _targets({}, connectors={"SmartHomeWebSearch": "WebSearch"})
    entry = gateway_catalog.entry_for("WebSearch", index.s3_client)
    assert entry["actionName"] == "SmartHomeWebSearch___WebSearch"


def test_connector_gets_a_description_rather_than_a_blank_cell():
    import gateway_catalog
    import index
    _targets({}, connectors={"SmartHomeWebSearch": "WebSearch"})
    entry = gateway_catalog.entry_for("WebSearch", index.s3_client)
    assert "web-search" in entry["description"]
    assert "1.2.0" in entry["description"]


# --- two gateways, two regions ---------------------------------------------

def test_a_tool_carries_the_region_of_its_own_gateway():
    """The whole point: web search is governed in us-east-1, devices in us-west-2."""
    import gateway_catalog
    import index
    gateway_catalog.WEBSEARCH_GATEWAY_ID = "gw-websearch"

    def per_gateway(gatewayIdentifier):
        if gatewayIdentifier == "gw-websearch":
            return {"items": [{"name": "SmartHomeWebSearch", "targetId": "tid-ws"}]}
        return {"items": [{"name": "SmartHomeDeviceControl", "targetId": "tid-dc"}]}

    _mock_ac.list_gateway_targets.side_effect = per_gateway

    def get_target(gatewayIdentifier, targetId):
        if targetId == "tid-ws":
            return {"targetConfiguration": {"mcp": {"connector": {
                "source": {"connectorId": "web-search", "version": "1.2.0"},
                "configurations": [{"name": "WebSearch"}]}}}}
        return {"targetConfiguration": {"mcp": {"lambda": {"toolSchema": {
            "inlinePayload": [{"name": "control_device"}]}}}}}

    _mock_ac.get_gateway_target.side_effect = get_target
    _mock_ac.get_gateway.return_value = {"gatewayArn": "arn:aws:test:gateway/gw-1"}

    tools, errors = gateway_catalog.catalog(index.s3_client)
    by_name = {t["name"]: t for t in tools}
    assert errors == []
    assert by_name["control_device"]["region"] == "us-west-2"
    assert by_name["WebSearch"]["region"] == "us-east-1"
    assert by_name["WebSearch"]["gatewayId"] == "gw-websearch"
    _mock_ac.list_gateway_targets.side_effect = None


def test_one_unreachable_gateway_does_not_empty_the_other():
    """An empty tool list reads as 'everything was revoked', which sends an admin
    looking in exactly the wrong place."""
    import gateway_catalog
    import index
    gateway_catalog.WEBSEARCH_GATEWAY_ID = "gw-websearch"

    def per_gateway(gatewayIdentifier):
        if gatewayIdentifier == "gw-websearch":
            raise RuntimeError("gateway not found")
        return {"items": [{"name": "SmartHomeDeviceControl", "targetId": "tid-dc"}]}

    _mock_ac.list_gateway_targets.side_effect = per_gateway
    _mock_ac.get_gateway_target.side_effect = lambda gatewayIdentifier, targetId: {
        "targetConfiguration": {"mcp": {"lambda": {"toolSchema": {
            "inlinePayload": [{"name": "control_device"}]}}}}}

    tools, errors = gateway_catalog.catalog(index.s3_client)
    assert [t["name"] for t in tools] == ["control_device"]
    assert len(errors) == 1 and "websearch" in errors[0]
    _mock_ac.list_gateway_targets.side_effect = None


def test_the_home_region_keeps_its_original_policy_engine_row():
    """Sharing one row across regions would make the second region's provisioning
    overwrite the first's engine id."""
    import index
    assert index._policy_engine_sk("us-west-2") == "__policy_engine__"
    assert index._policy_engine_sk("us-east-1") == "__policy_engine_us-east-1__"


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


def test_an_ungovernable_tool_provisions_no_engine():
    """rebuild resolves the gateway BEFORE creating an engine, so a typo'd tool
    name does not leave a policy engine behind for a tool that cannot exist."""
    import index
    _targets({"SmartHomeNavigation": ["navigate_to_page"]})
    _mock_table.scan.return_value = {"Items": []}
    with pytest.raises(ValueError, match="not exposed by any gateway target"):
        index.rebuild_tool_policy("no_such_tool")
    assert not _mock_ac.create_policy_engine.called


def test_only_tools_whose_membership_changed_are_rebuilt():
    """The union was silently destructive.

    A tool with no Cedar policy is allow-all — measured, the tools gateway runs in
    ENFORCE with zero policies and still serves all six tools. Rebuilding one
    materialises a permit naming only the users who hold it in DynamoDB. With 14 of
    40 users holding a `__permissions__` row, granting ONE new tool to ONE user
    would have revoked the six device tools from the other 26, and reported it as a
    successful save of an unrelated grant.
    """
    import index
    rebuilt = []
    with patch.object(index, "rebuild_tool_policy", side_effect=rebuilt.append), \
         patch.object(index, "GATEWAY_ID", "gw-tools"):
        _mock_table.get_item.return_value = {
            "Item": {"allowedTools": ["control_device", "discover_devices"]}}
        resp = index.update_user_permissions({
            "pathParameters": {"userId": "sub-alice"},
            "body": json.dumps({"allowedTools": [
                "control_device", "discover_devices", "WebSearch"]}),
        })
    assert resp["statusCode"] == 200
    assert rebuilt == ["WebSearch"], (
        f"granting WebSearch rebuilt {rebuilt}; anything beyond WebSearch converts "
        f"an allow-all tool into default-deny as a side effect")


def test_removing_a_tool_still_rebuilds_it():
    """The symmetric difference must not lose the revoke case — that is the whole
    point of rebuilding at all."""
    import index
    rebuilt = []
    with patch.object(index, "rebuild_tool_policy", side_effect=rebuilt.append), \
         patch.object(index, "GATEWAY_ID", "gw-tools"):
        _mock_table.get_item.return_value = {
            "Item": {"allowedTools": ["control_device", "WebSearch"]}}
        index.update_user_permissions({
            "pathParameters": {"userId": "sub-alice"},
            "body": json.dumps({"allowedTools": ["control_device"]}),
        })
    assert rebuilt == ["WebSearch"]


def test_the_statement_uses_a_sub_and_keeps_the_entity_type_guard():
    """Two live failures pinned at once.

    An email in place of the sub yields an ACTIVE policy matching nobody. Dropping
    the `principal is` guard yields UPDATE_FAILED — `attribute 'id' on entity type
    'AgentCore::UnauthenticatedUser' not found' — while the API still returns 200.
    """
    import index
    _targets({}, connectors={"SmartHomeWebSearch": "WebSearch"})
    sub = "88c1a3e0-b041-707c-01ae-3ac4d821adbd"
    stmt, _ = index.build_cedar_statement("WebSearch", [sub])
    assert f'(principal.id) == "{sub}"' in stmt
    assert "principal is AgentCore::OAuthUser" in stmt
    assert "principal is AgentCore::IamEntity" in stmt
    assert "@" not in stmt.split("principal.id")[1][:80], (
        "an email reached principal.id; that policy is ACTIVE and matches nobody")
