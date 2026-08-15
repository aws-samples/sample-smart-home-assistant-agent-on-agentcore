"""Resolving a Registry record's card URL back to a runtime ARN, for the fleet join.

This is the half of the fleet that needs AWS, and it is where the 2026-08-15
regression actually lived. `agents.build_fleet` was given a card URL and asked to find
the runtime name in it; once every card pointed at a gateway target instead of at
`/runtimes/<arn>/invocations`, there was nothing in the URL to find. The fix is to
resolve the target here, where a `bedrock-agentcore-control` client exists, and hand
the ARN to the pure join.

What these tests protect:

  - a gateway-fronted record resolves (the regression)
  - a direct-URL record resolves with NO AWS call at all, because a third party
    registering their own runtime must not depend on our gateway being reachable
  - ONE ListGatewayTargets for the whole batch, not one per record
  - every failure degrades to "no ARN" rather than raising, because a fleet page that
    500s tells an operator less than one with an unresolved row
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GW_HOST = ("https://smarthome-a2a-gw-gazpaav3r9.gateway.bedrock-agentcore."
           "us-west-2.amazonaws.com")
LIGHT_ARN = "arn:aws:bedrock-agentcore:us-west-2:111:runtime/sha2alight_sha2alight-KAoL"
ENERGY_ARN = "arn:aws:bedrock-agentcore:us-west-2:111:runtime/sha2aenergy_sha2aenergy-2Lt"


def _direct(arn):
    return ("https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
            + arn.replace(":", "%3A").replace("/", "%2F") + "/invocations")


@pytest.fixture(scope="module")
def idx():
    os.environ["COGNITO_USER_POOL_ID"] = "us-west-2_HwYYt6qLz"
    os.environ["COGNITO_APP_CLIENT_ID"] = "client123"
    os.environ["REGISTRY_ID"] = "Zuy3YNKrPQ5uwE9t"
    import importlib

    import index
    importlib.reload(index)
    return index


def _control(targets):
    """A stub `bedrock-agentcore-control` fronting `targets` = {name: endpoint}."""
    control = MagicMock()
    control.get_paginator.return_value.paginate.return_value = [
        {"items": [{"name": n, "targetId": f"T-{n}"} for n in targets]}]
    control.get_gateway_target.side_effect = lambda gatewayIdentifier, targetId: {
        "targetConfiguration": {"http": {"passthrough": {
            "endpoint": targets[targetId[2:]], "protocolType": "A2A"}}}}
    return control


def _rec(name, url):
    return {"recordId": f"REC-{name}", "name": name, "displayName": name,
            "status": "APPROVED", "card": {"name": name, "url": url}}


def test_a_gateway_fronted_record_resolves_to_its_runtime_arn(idx):
    records = [_rec("light-effect-agent", f"{GW_HOST}/light-effect")]
    control = _control({"light-effect": _direct(LIGHT_ARN)})
    with patch.object(idx, "agentcore_control", control):
        idx._attach_runtime_arns(records)
    assert records[0]["runtimeArn"] == LIGHT_ARN


def test_a_direct_url_record_costs_no_aws_call(idx):
    """A third party's own runtime URL needs no resolution, and must not be made to
    wait on a gateway it does not use."""
    records = [_rec("third-party-agent", _direct(LIGHT_ARN))]
    control = MagicMock()
    with patch.object(idx, "agentcore_control", control):
        idx._attach_runtime_arns(records)
    assert records[0]["runtimeArn"] == LIGHT_ARN
    control.get_paginator.assert_not_called()


def test_the_whole_batch_costs_one_target_listing(idx):
    """`resolve_arn` pages the target list and reads one target from it, so calling it
    per record turns eight rows into eight paginations of the same list."""
    records = [_rec("light-effect-agent", f"{GW_HOST}/light-effect"),
               _rec("energy-optimization-agent", f"{GW_HOST}/energy-optimization")]
    control = _control({"light-effect": _direct(LIGHT_ARN),
                        "energy-optimization": _direct(ENERGY_ARN)})
    with patch.object(idx, "agentcore_control", control):
        idx._attach_runtime_arns(records)
    assert [r["runtimeArn"] for r in records] == [LIGHT_ARN, ENERGY_ARN]
    assert control.get_paginator.return_value.paginate.call_count == 1


def test_a_target_that_does_not_exist_leaves_the_record_unresolved(idx):
    """The state right after an approve, before the gateway reconcile has run. The
    row should say "no runtime", not break the page."""
    records = [_rec("brand-new-agent", f"{GW_HOST}/brand-new")]
    with patch.object(idx, "agentcore_control", _control({})):
        idx._attach_runtime_arns(records)
    assert "runtimeArn" not in records[0]


def test_a_gateway_listing_failure_is_swallowed(idx):
    control = MagicMock()
    control.get_paginator.side_effect = RuntimeError("AccessDeniedException")
    records = [_rec("light-effect-agent", f"{GW_HOST}/light-effect")]
    with patch.object(idx, "agentcore_control", control):
        idx._attach_runtime_arns(records)   # must not raise
    assert "runtimeArn" not in records[0]


def test_a_lambda_target_is_not_mistaken_for_a_runtime(idx):
    """The navigation DeepLink is an A2A-shaped target that fronts a Lambda. Its
    endpoint carries no runtime ARN, and inventing one would attach a specialist's
    metrics to it."""
    records = [_rec("navigation", f"{GW_HOST}/navigate")]
    with patch.object(idx, "agentcore_control",
                      _control({"navigate": "https://lambda.example/invoke"})):
        idx._attach_runtime_arns(records)
    assert "runtimeArn" not in records[0]


def test_an_unrecognisable_url_is_left_alone(idx):
    records = [_rec("placeholder-agent", "https://example.com/a2a/x")]
    control = MagicMock()
    with patch.object(idx, "agentcore_control", control):
        idx._attach_runtime_arns(records)
    assert "runtimeArn" not in records[0]
    control.get_paginator.assert_not_called()
