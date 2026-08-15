"""Tests for getting from an AgentCard's `url` back to the runtime behind it.

The pure half is tested here because that is where the mistakes are: the ARN is
URL-escaped inside the path, the two URL shapes 404 at each other's endpoints, and a
wrong answer does not raise — it makes a record look unresolvable, which the
conformance check then reports as "could not check" rather than as a bug here.

The gateway hop is tested with a stub client rather than moto, because what matters is
the SHAPE of the two calls (ListGatewayTargets does not return the endpoint;
GetGatewayTarget does) and no library will tell us if we got that wrong.
"""
from unittest.mock import MagicMock

import a2a_runtimes as rt

REGION = "us-west-2"
ARN = f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/sha2aair-XyZ123"
DIRECT = (f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/"
          + ARN.replace(":", "%3A").replace("/", "%2F") + "/invocations")
GW_HOST = (f"https://smarthome-a2a-gw-ab12.gateway.bedrock-agentcore."
           f"{REGION}.amazonaws.com")
GATEWAY = f"{GW_HOST}/air-quality"


# ---------------------------------------------------------------------------
# Pure: parsing a direct runtime URL
# ---------------------------------------------------------------------------

def test_the_escaped_arn_is_recovered_whole():
    """`%3A` and `%2F`, not `:` and `/` — splitting on a colon returns nonsense."""
    assert rt.runtime_arn_from_url(DIRECT) == ARN
    assert rt.runtime_id_from_url(DIRECT) == "sha2aair-XyZ123"


def test_a_url_with_no_runtime_segment_yields_nothing():
    for url in ("", "https://example.com/", "https://example.com/invocations",
                GATEWAY):
        assert rt.runtime_id_from_url(url) == ""
        assert rt.runtime_arn_from_url(url) == ""


def test_something_that_is_not_an_arn_is_rejected_rather_than_returned():
    """A non-ARN would otherwise be handed to GetAgentRuntime as an id."""
    url = (f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/"
           "not-an-arn/invocations")
    assert rt.runtime_arn_from_url(url) == ""
    assert rt.runtime_id_from_url(url) == ""


# ---------------------------------------------------------------------------
# Pure: parsing a gateway URL
# ---------------------------------------------------------------------------

def test_the_gateway_id_and_target_come_off_the_host_and_the_path():
    assert rt.gateway_target_from_url(GATEWAY) == \
        ("smarthome-a2a-gw-ab12", "air-quality")
    assert rt.gateway_target_from_url(GATEWAY + "/") == \
        ("smarthome-a2a-gw-ab12", "air-quality")


def test_a_bare_gateway_host_names_no_target():
    """Returning the host as the target name would look up a target named after the
    gateway and report "no such target" — which reads like a deleted target."""
    gw, target = rt.gateway_target_from_url(GW_HOST)
    assert gw == "smarthome-a2a-gw-ab12" and target == ""
    gw, target = rt.gateway_target_from_url(GW_HOST + "/")
    assert gw == "smarthome-a2a-gw-ab12" and target == ""


def test_the_gateway_base_url_drops_the_target_path():
    """This is what the manifest publishes, so a trailing target would send every
    third party's card at one specific agent's target."""
    assert rt.gateway_base_url(GATEWAY) == GW_HOST
    assert rt.gateway_base_url(GW_HOST) == GW_HOST
    assert rt.gateway_base_url(DIRECT) == ""
    assert rt.gateway_base_url("") == ""


# ---------------------------------------------------------------------------
# The gateway hop
# ---------------------------------------------------------------------------

def _client(targets):
    """A stub where ListGatewayTargets omits the endpoint, as the real one does."""
    c = MagicMock()
    pages = [{"items": [{"name": t["name"], "targetId": t["targetId"],
                         "status": "READY"} for t in targets]}]
    c.get_paginator.return_value.paginate.return_value = pages
    detail = {t["targetId"]: {"targetConfiguration": {"http": {"passthrough": {
        "endpoint": t["endpoint"], "protocolType": "A2A"}}}} for t in targets}
    c.get_gateway_target.side_effect = \
        lambda gatewayIdentifier, targetId: detail[targetId]
    return c


def test_a_direct_url_resolves_without_touching_aws():
    client = MagicMock()
    rid, via = rt.resolve(DIRECT, client)
    assert rid == "sha2aair-XyZ123" and via == "direct runtime url"
    client.get_paginator.assert_not_called()


def test_a_gateway_url_resolves_through_the_targets_endpoint():
    client = _client([{"name": "air-quality", "targetId": "T1", "endpoint": DIRECT}])
    rid, via = rt.resolve(GATEWAY, client)
    assert rid == "sha2aair-XyZ123"
    assert "gateway smarthome-a2a-gw-ab12 target air-quality" == via


def test_a_missing_target_says_so_instead_of_raising():
    """Every caller has something useful to say about "could not check" and nothing
    useful to do with an exception."""
    client = _client([{"name": "other", "targetId": "T9", "endpoint": DIRECT}])
    rid, via = rt.resolve(GATEWAY, client)
    assert rid == "" and "no target named air-quality" in via


def test_a_target_that_is_not_a_runtime_passthrough_is_reported():
    client = _client([{"name": "air-quality", "targetId": "T1",
                       "endpoint": "https://example.com/webhook"}])
    rid, via = rt.resolve(GATEWAY, client)
    assert rid == "" and "not a runtime passthrough" in via


def test_an_aws_failure_is_reported_not_raised():
    client = MagicMock()
    client.get_paginator.side_effect = RuntimeError("AccessDenied")
    rid, via = rt.resolve(GATEWAY, client)
    assert rid == "" and "lookup failed" in via


def test_list_targets_joins_each_target_to_its_runtime():
    """ListGatewayTargets does not return the endpoint, and the endpoint is the only
    thing that says which agent a target fronts. Matching on NAME instead would break
    the moment a card is renamed."""
    client = _client([{"name": "air-quality", "targetId": "T1", "endpoint": DIRECT},
                      {"name": "weird", "targetId": "T2",
                       "endpoint": "https://example.com/x"}])
    out = rt.list_targets(client, "smarthome-a2a-gw-ab12")
    assert [(t["name"], t["runtimeId"]) for t in out] == [
        ("air-quality", "sha2aair-XyZ123"), ("weird", "")]


def test_one_unreadable_target_does_not_lose_the_others():
    client = _client([{"name": "ok", "targetId": "T1", "endpoint": DIRECT}])
    original = client.get_gateway_target.side_effect

    def flaky(gatewayIdentifier, targetId):
        if targetId == "T1":
            raise RuntimeError("throttled")
        return original(gatewayIdentifier=gatewayIdentifier, targetId=targetId)

    client.get_gateway_target.side_effect = flaky
    assert rt.list_targets(client, "gw") == []
