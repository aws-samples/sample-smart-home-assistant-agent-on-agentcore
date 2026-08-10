"""Tests for the agent fleet read model.

The fleet is DERIVED from three sources rather than listed, so the risks are in
the join, not in any single source:

  - a Registry record and a runtime ARN describe the same agent and must collapse
    into one row; the only thing tying them together is the runtime name buried in
    the AgentCard's percent-encoded invocation URL
  - an approved record whose runtime is absent must still appear, because that
    state means either an orphaned record or a deploy that never ran
    `patch-text-agent` — and silently dropping the row is how the second goes
    unnoticed
  - every source is optional; a partial outage should degrade the page, not blank it
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents as fleet


TEXT_ARN = "arn:aws:bedrock-agentcore:us-west-2:111:runtime/smarthome_smarthome-ee97ToCthI"
VOICE_ARN = "arn:aws:bedrock-agentcore:us-west-2:111:runtime/smarthomevoice_smarthomevoice-HAi7bi8jxA"
LIGHT_ARN = "arn:aws:bedrock-agentcore:us-west-2:111:runtime/sha2alight_sha2alight-KAoLvdD6OV"


def _card(name, runtime_arn, skills=(), description="d", version="1.0.0"):
    """An AgentCard as render_card_for_registry produces it — the invocation URL
    is the percent-encoded runtime ARN, which is what the join reads."""
    encoded = runtime_arn.replace(":", "%3A").replace("/", "%2F")
    return {
        "name": name,
        "description": description,
        "version": version,
        "url": f"https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/{encoded}/invocations",
        "skills": [{"id": s, "name": s, "description": ""} for s in skills],
    }


# ---------------------------------------------------------------------------
# Runtime name extraction — the join key
# ---------------------------------------------------------------------------

def test_runtime_name_drops_the_random_suffix():
    assert fleet._runtime_name(LIGHT_ARN) == "sha2alight_sha2alight"


def test_runtime_name_is_read_out_of_an_encoded_card_url():
    card = _card("light-effect-agent", LIGHT_ARN)
    assert fleet._runtime_name_from_card_url(card["url"]) == "sha2alight_sha2alight"


def test_a_card_url_that_is_not_a_runtime_url_yields_nothing():
    """The three original agents' cards carried an example.com placeholder before
    deploy rewrote them."""
    assert fleet._runtime_name_from_card_url("https://example.com/a2a/x") == ""
    assert fleet._runtime_name_from_card_url("") == ""


# ---------------------------------------------------------------------------
# The join
# ---------------------------------------------------------------------------

def test_a_record_and_its_runtime_collapse_into_one_row():
    """Otherwise the page shows every specialist twice."""
    out = fleet.build_fleet(
        runtime_arns=[TEXT_ARN, LIGHT_ARN],
        registry_records=[{
            "recordId": "REC1", "displayName": "light-effect-agent",
            "status": "APPROVED",
            "card": _card("light-effect-agent", LIGHT_ARN, ["compose_effect"]),
        }],
        metadata={},
    )
    assert len(out) == 2
    light = next(a for a in out if a["runtimeName"] == "sha2alight_sha2alight")
    assert light["recordId"] == "REC1"
    assert [s["id"] for s in light["skills"]] == ["compose_effect"]
    assert light["live"] is True


def test_an_approved_record_with_no_live_runtime_still_appears_flagged():
    """This is the state after a torn-down runtime, or after a deploy that skipped
    patch-text-agent so DASHBOARD_EXTRA_RUNTIME_ARNS never learned the ARN. Both
    want an operator's attention."""
    out = fleet.build_fleet(
        runtime_arns=[TEXT_ARN],
        registry_records=[{
            "recordId": "ORPHAN", "displayName": "ghost-agent", "status": "APPROVED",
            "card": _card("ghost-agent", LIGHT_ARN),
        }],
        metadata={},
    )
    ghost = next(a for a in out if a["recordId"] == "ORPHAN")
    assert ghost["live"] is False
    assert ghost["runtimeArn"] == ""


def test_a_runtime_with_no_record_still_appears():
    """The orchestrator and the voice runtime have no Registry record at all."""
    out = fleet.build_fleet([TEXT_ARN, VOICE_ARN], [], {})
    assert {a["runtimeName"] for a in out} == {
        "smarthome_smarthome", "smarthomevoice_smarthomevoice"}
    assert all(a["recordId"] == "" for a in out)


def test_kinds_are_classified():
    out = fleet.build_fleet([TEXT_ARN, VOICE_ARN, LIGHT_ARN], [], {})
    kinds = {a["runtimeName"]: a["kind"] for a in out}
    assert kinds["smarthome_smarthome"] == fleet.KIND_ORCHESTRATOR
    assert kinds["smarthomevoice_smarthomevoice"] == fleet.KIND_VOICE
    assert kinds["sha2alight_sha2alight"] == fleet.KIND_SPECIALIST


def test_metadata_overrides_the_derived_values():
    """Metadata is the only place a human-chosen label or a corrected kind lives."""
    out = fleet.build_fleet(
        [LIGHT_ARN], [],
        {"sha2alight": {"label": "Lighting Studio", "labelZh": "灯效工作室",
                        "kind": "specialist", "orchestrator": "smarthome"}})
    a = out[0]
    assert a["displayName"] == "Lighting Studio"
    assert a["displayNameZh"] == "灯效工作室"
    assert a["orchestrator"] == "smarthome"


def test_display_name_falls_back_to_the_agent_id():
    out = fleet.build_fleet([LIGHT_ARN], [], {})
    assert out[0]["displayName"] == "sha2alight"


# ---------------------------------------------------------------------------
# Health metrics
# ---------------------------------------------------------------------------

def test_health_rows_join_on_the_runtime_name():
    """dashboard._fetch_health keys per-runtime rows by `name`, the same key the
    join uses — a mismatch here would leave every metric column empty while the
    page still looked fine."""
    out = fleet.build_fleet(
        [LIGHT_ARN], [], {},
        health_runtimes=[{"name": "sha2alight_sha2alight", "invocations": 12.0,
                          "userErrors": 1.0, "systemErrors": 0.0,
                          "throttles": 0.0, "latencyP95": 880.0}])
    a = out[0]
    assert a["invocations"] == 12.0
    assert a["errors"] == 1.0
    assert a["latencyP95Ms"] == 880.0


def test_missing_health_leaves_metrics_none_rather_than_zero():
    """A zero would read as "this agent is idle"; None reads as "unknown"."""
    out = fleet.build_fleet([LIGHT_ARN], [], {}, health_runtimes=[])
    assert out[0]["invocations"] is None
    assert out[0]["errors"] is None


def test_errors_sum_the_two_cloudwatch_error_metrics():
    """AWS/Bedrock-AgentCore emits no `Errors` metric — only UserErrors and
    SystemErrors, and _fetch_health keeps them split per runtime. Reading a plain
    `errors` key made this column show `--` for every agent on the first live
    call, which reads as "healthy" while an agent is failing."""
    out = fleet.build_fleet(
        [LIGHT_ARN], [], {},
        health_runtimes=[{"name": "sha2alight_sha2alight",
                          "userErrors": 3.0, "systemErrors": 2.0}])
    a = out[0]
    assert a["errors"] == 5.0
    # Both kept, so a detail view can say which kind without a second query.
    assert (a["userErrors"], a["systemErrors"]) == (3.0, 2.0)


def test_errors_is_zero_not_unknown_when_only_one_metric_reported():
    """A runtime with traffic and no system errors reports UserErrors only. That
    is a known zero, not an unknown."""
    out = fleet.build_fleet(
        [LIGHT_ARN], [], {},
        health_runtimes=[{"name": "sha2alight_sha2alight", "userErrors": 0.0}])
    assert out[0]["errors"] == 0.0


def test_errors_stays_unknown_when_neither_metric_reported():
    out = fleet.build_fleet(
        [LIGHT_ARN], [], {},
        health_runtimes=[{"name": "sha2alight_sha2alight", "invocations": 4.0}])
    assert out[0]["invocations"] == 4.0
    assert out[0]["errors"] is None


# ---------------------------------------------------------------------------
# The navigation tool
# ---------------------------------------------------------------------------

def test_the_navigation_tool_is_listed_as_a_tool_not_an_agent():
    """It is a Gateway Lambda target, but it is one of the seven entities in the
    customer's architecture and an operator looking for it should find it."""
    out = fleet.build_fleet(
        [TEXT_ARN], [], {},
        gateway_tools=[{"name": "navigate_to_page", "description": "deep links",
                        "targetName": "SmartHomeNavigation"},
                       {"name": "control_device", "description": "x"}])
    tools = [a for a in out if a["kind"] == fleet.KIND_TOOL]
    assert len(tools) == 1
    assert tools[0]["agentId"] == "navigate_to_page"
    assert tools[0]["runtimeArn"] == ""


def test_other_gateway_tools_are_not_listed_as_fleet_entries():
    """control_device is a tool of an agent, not an entity in the fleet."""
    out = fleet.build_fleet(
        [TEXT_ARN], [], {},
        gateway_tools=[{"name": "control_device"}, {"name": "query_device_state"}])
    assert all(a["kind"] != fleet.KIND_TOOL for a in out)


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------

def test_every_source_is_optional():
    assert fleet.build_fleet([], [], {}) == []
    assert len(fleet.build_fleet([TEXT_ARN], [], {})) == 1


def test_metadata_load_survives_a_query_failure():
    """A missing label must not blank the page."""
    from unittest.mock import MagicMock

    table = MagicMock()
    table.query.side_effect = RuntimeError("throttled")
    assert fleet.load_metadata(table) == {}


def test_metadata_sk_round_trips():
    assert fleet._agent_id_from_sk(fleet._meta_sk("sha2alight")) == "sha2alight"
    assert fleet._meta_sk("x").startswith(fleet.AGENT_META_SK_PREFIX)


# ---------------------------------------------------------------------------
# The bundles runtime
# ---------------------------------------------------------------------------

BUNDLES_ARN = "arn:aws:bedrock-agentcore:us-west-2:111:runtime/smarthome_bundles-wC9vsZ75VN"


def test_the_bundles_runtime_is_a_variant_not_a_second_orchestrator():
    """Same image as the orchestrator with ENABLE_BUNDLE_HOOK=1, used only in
    ab-bundles mode. Calling it an orchestrator would overstate the fleet; hiding
    it would make its token spend unattributable."""
    out = fleet.build_fleet([TEXT_ARN, BUNDLES_ARN], [], {})
    kinds = {a["runtimeName"]: a["kind"] for a in out}
    assert kinds["smarthome_smarthome"] == fleet.KIND_ORCHESTRATOR
    assert kinds["smarthome_bundles"] == fleet.KIND_VARIANT


def test_the_bundles_runtime_gets_its_own_agent_id():
    """`smarthome_bundles`.split("_")[0] is "smarthome" — the same id as the
    orchestrator, which would give two rows one trackBy key and one metadata row."""
    out = fleet.build_fleet([TEXT_ARN, BUNDLES_ARN], [], {})
    ids = [a["agentId"] for a in out]
    assert len(ids) == len(set(ids)), ids
    assert "smarthome_bundles" in ids


# ---------------------------------------------------------------------------
# Row shape
# ---------------------------------------------------------------------------

def test_every_row_carries_the_same_keys():
    """A consumer reading row["invocations"] should not have to know which rows
    are agents and which are Gateway targets."""
    out = fleet.build_fleet(
        [TEXT_ARN, LIGHT_ARN], [],
        {}, health_runtimes=[],
        gateway_tools=[{"name": "navigate_to_page", "description": "d"}])
    required = {"agentId", "displayName", "kind", "runtimeName", "runtimeArn",
                "skills", "recordId", "live", "invocations", "errors",
                "userErrors", "systemErrors", "throttles", "latencyP95Ms",
                "description"}
    for row in out:
        missing = required - set(row)
        assert not missing, f"{row.get('agentId')} missing {missing}"
