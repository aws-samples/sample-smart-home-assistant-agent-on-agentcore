"""Registry-driven A2A gateway targets.

The point: an APPROVED record should be enough to be fronted on the gateway. The
previous path read `a2a-agent-registry/deployed-state.json`, a file only the platform
team has, so a third-party team with an approved agent had to ask someone to add a
target by hand — exactly the cross-team ticket this work exists to delete.

Two decisions are load-bearing and both are tested here:

  - Matched by RUNTIME, never by target name. The eight built-in targets are named
    after internal short names (`light-effect`) while their cards carry long names
    (`light-effect-agent`).
  - Orphans are REPORTED, not deleted. A target whose record is gone is dead weight,
    not an open door — the revocation sweep is what closes access — and deleting it
    would break any card still pointing at it.
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GW_HOST = ("https://smarthome-a2a-gw-ab12.gateway.bedrock-agentcore."
           "us-west-2.amazonaws.com")


def _arn(name):
    return f"arn:aws:bedrock-agentcore:us-west-2:123:runtime/{name}"


def _direct(name):
    return ("https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
            + _arn(name).replace(":", "%3A").replace("/", "%2F") + "/invocations")


@pytest.fixture(scope="module")
def idx():
    """Reloaded once — see test_conformance_gate.py for why not per test."""
    os.environ["COGNITO_USER_POOL_ID"] = "us-west-2_HwYYt6qLz"
    os.environ["COGNITO_APP_CLIENT_ID"] = "client123"
    os.environ["REGISTRY_ID"] = "Zuy3YNKrPQ5uwE9t"
    import importlib

    import index
    importlib.reload(index)
    return index


def _run(idx, records, targets, *, apply=False):
    """Drive the reconcile with stubbed Registry records and gateway targets."""
    control = MagicMock()
    control.get_paginator.return_value.paginate.return_value = [
        {"items": [{"name": t["name"], "targetId": t["targetId"]} for t in targets]}]
    detail = {t["targetId"]: {"targetConfiguration": {"http": {"passthrough": {
        "endpoint": t["endpoint"], "protocolType": "A2A"}}}} for t in targets}
    control.get_gateway_target.side_effect = \
        lambda gatewayIdentifier, targetId: detail[targetId]
    control.create_gateway_target.return_value = {"targetId": "NEW"}

    event = {"queryStringParameters": {"apply": "true"} if apply else {}}
    # The gateway URL is pinned rather than derived here: which of its two discovery
    # sources answered is `test_the_gateway_is_found_*` below, and leaving it live made
    # every direct-URL case fall out of the "no gateway in use" early exit — which
    # showed up as a missing `created` key rather than as anything about gateways.
    with patch.object(idx, "_fetch_a2a_records", return_value=records), \
            patch.object(idx, "agentcore_control", control), \
            patch.object(idx, "_a2a_gateway_url", return_value=GW_HOST), \
            patch.object(idx, "_grantability",
                         return_value={r["recordId"]: (r.pop("_grantable", True), "ok")
                                       for r in records}):
        result = idx.reconcile_a2a_gateway_targets(event)
    return json.loads(result["body"]), control


# ---------------------------------------------------------------------------
# The bug that shipped: our own cards point AT the gateway
# ---------------------------------------------------------------------------

def test_cards_already_behind_the_gateway_are_fronted_not_orphaned(idx):
    """The exact regression found on the live deployment.

    All eight built-in cards point at `{gatewayUrl}/{target}`, so a RAW url parse
    yields no runtime id. Building the live-runtime set that way collapsed it to {""},
    nothing matched, and every one of the eight working targets was reported orphaned —
    a report that reads as "delete these" about the entire fleet.
    """
    records = [{"recordId": "r1", "name": "light-effect-agent",
                "card": {"name": "light-effect-agent", "url": f"{GW_HOST}/light-effect"}}]
    targets = [{"name": "light-effect", "targetId": "T1",
                "endpoint": _direct("sha2alight-aaa")}]
    body, _ = _run(idx, records, targets)
    assert body["orphaned"] == []
    assert body["missing"] == []
    assert [f["name"] for f in body["fronted"]] == ["light-effect-agent"]
    # The target name comes off the card's own path, so the report is actionable.
    assert body["fronted"][0]["targetName"] == "light-effect"


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def test_a_target_is_matched_by_runtime_not_by_name(idx):
    """`light-effect` fronts `light-effect-agent`. Name matching would call this
    missing and create a duplicate target at the same runtime."""
    records = [{"recordId": "r1", "name": "light-effect-agent",
                "card": {"name": "light-effect-agent",
                         "url": _direct("sha2alight-aaa")}}]
    targets = [{"name": "light-effect", "targetId": "T1",
                "endpoint": _direct("sha2alight-aaa")}]
    body, control = _run(idx, records, targets, apply=True)
    assert body["missing"] == [] and body["created"] == []
    assert body["fronted"][0]["via"] == "matched by runtime id"
    control.create_gateway_target.assert_not_called()


def test_an_unfronted_agent_is_reported_then_created(idx):
    records = [{"recordId": "r1", "name": "third-party-agent",
                "card": {"name": "third-party-agent",
                         "url": _direct("sha2atp-zzz")}}]
    targets = [{"name": "light-effect", "targetId": "T1",
                "endpoint": _direct("sha2alight-aaa")}]

    body, control = _run(idx, [dict(r) for r in records], targets)
    assert [m["name"] for m in body["missing"]] == ["third-party-agent"]
    assert body["created"] == []
    assert body["applied"] is False
    control.create_gateway_target.assert_not_called()

    body, control = _run(idx, [dict(r) for r in records], targets, apply=True)
    assert [c["name"] for c in body["created"]] == ["third-party-agent"]
    kwargs = control.create_gateway_target.call_args.kwargs
    # Named after the CARD, so a third party can compute its own gateway URL from the
    # manifest's targetNameTemplate before the target exists.
    assert kwargs["name"] == "third-party-agent"
    assert kwargs["targetConfiguration"]["http"]["passthrough"] == {
        "endpoint": _direct("sha2atp-zzz"), "protocolType": "A2A"}
    # JWT_PASSTHROUGH or the container sees the GATEWAY's identity instead of the
    # user's, and per-user authorization silently stops existing.
    assert kwargs["credentialProviderConfigurations"] == [
        {"credentialProviderType": "JWT_PASSTHROUGH"}]


def test_a_non_grantable_record_is_not_fronted(idx):
    """A rejected or deprecated agent must not get a fresh front door."""
    records = [{"recordId": "r1", "name": "gone-agent", "_grantable": False,
                "card": {"name": "gone-agent", "url": _direct("sha2agone-xxx")}}]
    body, control = _run(idx, records, [], apply=True)
    assert body["missing"] == [] and body["created"] == []
    control.create_gateway_target.assert_not_called()


def test_a_card_url_that_is_not_a_runtime_is_an_error_not_a_target(idx):
    records = [{"recordId": "r1", "name": "weird-agent",
                "card": {"name": "weird-agent", "url": "https://example.com/hook"}}]
    body, control = _run(idx, records, [], apply=True)
    assert body["created"] == []
    assert any("not a runtime invocations URL" in e for e in body["errors"])
    control.create_gateway_target.assert_not_called()


# ---------------------------------------------------------------------------
# Orphans are reported, never deleted
# ---------------------------------------------------------------------------

def test_a_target_with_no_record_is_reported_and_left_alone(idx):
    records = [{"recordId": "r1", "name": "light-effect-agent",
                "card": {"name": "light-effect-agent",
                         "url": _direct("sha2alight-aaa")}}]
    targets = [{"name": "light-effect", "targetId": "T1",
                "endpoint": _direct("sha2alight-aaa")},
               {"name": "retired", "targetId": "T2",
                "endpoint": _direct("sha2aretired-bbb")}]
    body, control = _run(idx, records, targets, apply=True)
    assert body["orphaned"] == ["retired"]
    assert not hasattr(control, "delete_gateway_target") or \
        not control.delete_gateway_target.called


# ---------------------------------------------------------------------------
# Refuses to act on a bad read — same rule as the revocation sweep
# ---------------------------------------------------------------------------

def test_an_unreadable_registry_refuses_rather_than_unfronting_everything(idx):
    with patch.object(idx, "_fetch_a2a_records",
                      side_effect=RuntimeError("throttled")):
        result = idx.reconcile_a2a_gateway_targets({})
    body = json.loads(result["body"])
    assert result["statusCode"] == 503 and body["reconciled"] is False


def test_an_empty_registry_refuses_too(idx):
    """Indistinguishable from a half-provisioned registry."""
    with patch.object(idx, "_fetch_a2a_records", return_value=[]):
        result = idx.reconcile_a2a_gateway_targets({})
    assert result["statusCode"] == 503


# ---------------------------------------------------------------------------
# Finding the gateway, without an environment variable to lose
# ---------------------------------------------------------------------------

def test_the_gateway_is_found_from_a_records_own_url_without_calling_aws(idx):
    """Free, and exact: a card routed through the gateway names it."""
    records = [{"recordId": "r1", "card": {"name": "x", "url": f"{GW_HOST}/x"}}]
    control = MagicMock()
    with patch.object(idx, "agentcore_control", control):
        assert idx._a2a_gateway_url(records) == GW_HOST
    control.get_paginator.assert_not_called()


def test_the_gateway_is_found_by_name_when_no_record_uses_it_yet(idx):
    """Breaks the circularity. Deriving ONLY from the records means a deployment where
    nothing is behind the gateway yet can never front its first agent, and the manifest
    can never tell anyone the gateway exists."""
    control = MagicMock()
    control.get_paginator.return_value.paginate.return_value = [
        {"items": [{"name": "some-other-gw", "gatewayId": "G0"},
                   {"name": idx.A2A_GATEWAY_NAME, "gatewayId": "G1"}]}]
    control.get_gateway.return_value = {"gatewayUrl": f"{GW_HOST}/mcp"}
    with patch.object(idx, "agentcore_control", control):
        # The origin only: A2A passthrough targets hang off the host root, so a
        # returned `/mcp` path would build `.../mcp/{target}` and 404 on every call.
        assert idx._a2a_gateway_url([]) == GW_HOST
    control.get_gateway.assert_called_once_with(gatewayIdentifier="G1")


def test_no_gateway_anywhere_is_an_empty_string_not_an_error(idx):
    """The gateway is optional. A blank manifest field would read as a broken
    deployment; an absent section reads as an unused option."""
    control = MagicMock()
    control.get_paginator.return_value.paginate.return_value = [{"items": []}]
    with patch.object(idx, "agentcore_control", control):
        assert idx._a2a_gateway_url([]) == ""


def test_a_lookup_failure_degrades_to_empty_rather_than_raising(idx):
    control = MagicMock()
    control.get_paginator.side_effect = RuntimeError("AccessDenied")
    with patch.object(idx, "agentcore_control", control):
        assert idx._a2a_gateway_url([]) == ""
