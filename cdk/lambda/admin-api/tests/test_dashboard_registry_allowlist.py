"""The dashboard's runtime allowlist, derived from the Registry.

`service.name` on spans and eval metrics is an EXACT match, so the allowlist decides
what the page can see. Every failure this list has ever had is the same shape: a
runtime is missing from it, no error appears anywhere, and the page just shows less
data — which is indistinguishable from a quiet system.

That is why the properties tested here are all about NOT SHRINKING. A derived list
that empties itself on a Registry blip would be strictly worse than the hand-maintained
env var it replaces.
"""
import importlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEXT_ARN = "arn:aws:bedrock-agentcore:us-west-2:123:runtime/smarthome_smarthome-aaa"
VOICE_ARN = "arn:aws:bedrock-agentcore:us-west-2:123:runtime/smarthome_voice-bbb"
BUNDLE_ARN = "arn:aws:bedrock-agentcore:us-west-2:123:runtime/smarthome_bundles-ccc"
SUB_ARN = "arn:aws:bedrock-agentcore:us-west-2:123:runtime/sha2aair-ddd"


def _direct_url(arn):
    return ("https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
            + arn.replace(":", "%3A").replace("/", "%2F") + "/invocations")


_ENV_KEYS = ("AGENT_RUNTIME_ARN", "VOICE_AGENT_RUNTIME_ARN",
             "DASHBOARD_EXTRA_RUNTIME_ARNS", "REGISTRY_ID")


@pytest.fixture
def dash():
    """A fresh dashboard module with a known env and an empty ARN cache.

    Restores the environment AND reloads on the way out. `dashboard` reads its env at
    import time into module globals, so a leaked variable does not affect this file —
    it silently changes what the NEXT test module sees when it reloads. That is how
    four unrelated dashboard tests started failing only when run together.
    """
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["AGENT_RUNTIME_ARN"] = TEXT_ARN
    os.environ["VOICE_AGENT_RUNTIME_ARN"] = VOICE_ARN
    os.environ["DASHBOARD_EXTRA_RUNTIME_ARNS"] = BUNDLE_ARN
    os.environ["REGISTRY_ID"] = "Zuy3YNKrPQ5uwE9t"
    import dashboard
    importlib.reload(dashboard)
    dashboard._registry_arns_cache.update({"at": 0.0, "arns": []})
    try:
        yield dashboard
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(dashboard)
        dashboard._registry_arns_cache.update({"at": 0.0, "arns": []})


def _registry_client(records):
    """A stub whose GetRegistryRecord carries an AgentCard, as the real one does."""
    client = MagicMock()
    client.list_registry_records.return_value = {
        "registryRecords": [{"recordId": r["recordId"], "status": r["status"]}
                            for r in records]}
    client.get_registry_record.side_effect = lambda registryId, recordId: {
        "_card": next(r["card"] for r in records if r["recordId"] == recordId)}
    return client


def _patched(dash, records, control=None):
    """Patch the two AWS seams `_registry_runtime_arns` reaches through."""
    import agent_registry as registry_ns
    return (
        patch.object(registry_ns, "registry_client",
                     return_value=_registry_client(records)),
        patch.object(registry_ns, "read_agent_card",
                     side_effect=lambda detail: json.dumps(detail["_card"])),
        patch.object(dash, "_client", return_value=control or MagicMock()),
    )


def _run(dash, records, control=None):
    a, b, c = _patched(dash, records, control)
    with a, b, c:
        return dash._registry_runtime_arns()


# ---------------------------------------------------------------------------
# The point: a registered agent needs no env var
# ---------------------------------------------------------------------------

def test_an_approved_record_puts_its_runtime_on_the_allowlist(dash):
    """No `DASHBOARD_EXTRA_RUNTIME_ARNS` entry, no cross-team ticket."""
    records = [{"recordId": "r1", "status": "APPROVED",
                "card": {"name": "air-quality-agent", "url": _direct_url(SUB_ARN)}}]
    assert _run(dash, records) == [SUB_ARN]


def test_the_env_var_is_unioned_not_replaced(dash):
    """The bundles and voice runtimes have no Registry record at all, so replacing the
    env var would delete them from the page."""
    records = [{"recordId": "r1", "status": "APPROVED",
                "card": {"name": "air-quality-agent", "url": _direct_url(SUB_ARN)}}]
    a, b, c = _patched(dash, records)
    with a, b, c:
        allowlist = dash._all_runtime_arns()
    assert allowlist == [TEXT_ARN, VOICE_ARN, SUB_ARN, BUNDLE_ARN]


def test_a_runtime_named_by_both_sources_appears_once(dash):
    """Existing deployments have the sub-agents in BOTH, and a duplicated
    service.name would double-count that agent's tokens."""
    os.environ["DASHBOARD_EXTRA_RUNTIME_ARNS"] = f"{BUNDLE_ARN},{SUB_ARN}"
    importlib.reload(dash)
    dash._registry_arns_cache.update({"at": 0.0, "arns": []})
    records = [{"recordId": "r1", "status": "APPROVED",
                "card": {"name": "air-quality-agent", "url": _direct_url(SUB_ARN)}}]
    a, b, c = _patched(dash, records)
    with a, b, c:
        allowlist = dash._all_runtime_arns()
    assert allowlist.count(SUB_ARN) == 1


def test_a_non_approved_record_is_left_out(dash):
    """A DRAFT record's runtime may not exist; naming a missing log group takes the
    WHOLE spans query down with ResourceNotFoundException."""
    records = [{"recordId": "r1", "status": "DRAFT",
                "card": {"name": "air-quality-agent", "url": _direct_url(SUB_ARN)}}]
    assert _run(dash, records) == []


def test_a_card_pointing_at_the_gateway_resolves_through_its_target(dash):
    """Our own eight cards point at the gateway, so this is the normal case here."""
    gw = ("https://smarthome-a2a-gw-ab12.gateway.bedrock-agentcore."
          "us-west-2.amazonaws.com/air-quality")
    control = MagicMock()
    control.get_paginator.return_value.paginate.return_value = [
        {"items": [{"name": "air-quality", "targetId": "T1"}]}]
    control.get_gateway_target.return_value = {"targetConfiguration": {"http": {
        "passthrough": {"endpoint": _direct_url(SUB_ARN)}}}}
    records = [{"recordId": "r1", "status": "APPROVED",
                "card": {"name": "air-quality-agent", "url": gw}}]
    assert _run(dash, records, control) == [SUB_ARN]


# ---------------------------------------------------------------------------
# Never shrink
# ---------------------------------------------------------------------------

def test_a_registry_failure_serves_the_last_known_list(dash):
    """The failure this whole function exists to prevent is a silently shorter
    allowlist, so it must not cause one itself."""
    dash._registry_arns_cache.update({"at": 0.0, "arns": [SUB_ARN]})
    import agent_registry as registry_ns
    with patch.object(registry_ns, "registry_client",
                      side_effect=RuntimeError("throttled")):
        assert dash._registry_runtime_arns() == [SUB_ARN]


def test_a_placeholder_registry_id_serves_the_last_known_list(dash):
    """`cdk deploy` resets REGISTRY_ID to PLACEHOLDER_SET_BY_SETUP_SCRIPT, which is
    not a valid id — calling with it would throw ValidationException per request."""
    os.environ["REGISTRY_ID"] = "PLACEHOLDER_SET_BY_SETUP_SCRIPT"
    importlib.reload(dash)
    dash._registry_arns_cache.update({"at": 0.0, "arns": [SUB_ARN]})
    assert dash._registry_runtime_arns() == [SUB_ARN]


def test_the_result_is_cached_so_every_query_is_not_a_registry_sweep(dash):
    """`_all_runtime_arns` is called by both the service filter and the log-group
    list, several times per dashboard request."""
    records = [{"recordId": "r1", "status": "APPROVED",
                "card": {"name": "air-quality-agent", "url": _direct_url(SUB_ARN)}}]
    a, b, c = _patched(dash, records)
    with a as reg, b, c:
        dash._registry_runtime_arns()
        dash._registry_runtime_arns()
        dash._registry_runtime_arns()
        assert reg.call_count == 1
