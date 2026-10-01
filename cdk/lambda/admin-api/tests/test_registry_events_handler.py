"""Registry event notifications — the handler half.

Two things are worth a test here beyond the pure module. First, that the event is
STORED even when the enrichment read fails: a row with only a recordId is still a
notification, and dropping it because GetRegistryRecord was throttled is how an admin
misses a submission. Second, that a scheduled-style invocation (no HTTP envelope)
reaches the ingest path from `_dispatch`, which is otherwise all HTTP routing.
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDA_DIR = os.path.dirname(HERE)
if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)

import index  # noqa: E402


def _eb_event(record_id="rec-1", registry_id="reg-1",
              detail_type="Registry Record State changed to Pending Approval"):
    return {
        "version": "0", "id": "evt-1", "detail-type": detail_type,
        "source": "aws.agent-registry", "account": "123456789012",
        "time": "2026-09-19T07:00:00Z", "region": "us-west-2",
        "resources": [f"arn:aws:agent-registry:us-west-2:123456789012:registry/{registry_id}/record/{record_id}"],
        "detail": {"registryRecordId": record_id, "registryId": registry_id},
    }


def _http_event(groups="admin"):
    return {
        "httpMethod": "GET",
        "resource": "/registry/records",
        "queryStringParameters": {"action": "events"},
        "requestContext": {"authorizer": {"claims": {
            "email": "admin@smarthome.local", "cognito:groups": groups}}},
    }


@pytest.fixture
def wired():
    """Registry client, events table and ownership maps, all stand-ins."""
    control = MagicMock()
    control.get_registry_record.return_value = {
        "recordId": "rec-1", "name": "acme-weather-1", "displayName": "Weather",
        "recordType": "SKILL", "recordVersion": "2", "status": "PENDING_APPROVAL",
        "description": "Forecasts", "statusReason": "submitted",
    }
    events_table = MagicMock()
    events_table.query.return_value = {"Items": []}
    # The Skill ERP's ownership row: keyed by bare recordId for skills, and it
    # carries only the Cognito sub — no email, no recordType (measured 2026-09-19
    # against all 14 rows in the deployment).
    skills_table = MagicMock()
    skills_table.get_item.return_value = {"Item": {"ownerSub": "s-1"}}
    cognito = MagicMock()
    cognito.list_users.return_value = {"Users": [{"Username": "u1", "Attributes": [
        {"Name": "sub", "Value": "s-1"}, {"Name": "email", "Value": "author@erp.local"}]}]}
    with patch.object(index, "registry_control", control), \
         patch.object(index, "REGISTRY_ID", "reg-1"), \
         patch.object(index, "COGNITO_USER_POOL_ID", "us-west-2_TEST"), \
         patch.object(index, "_registry_events_table", return_value=events_table), \
         patch.object(index, "table", skills_table), \
         patch.object(index, "cognito_client", cognito):
        yield control, events_table, skills_table, cognito


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def test_dispatch_routes_registry_events_to_ingest(wired):
    _, events_table, _, _ = wired
    out = index._dispatch(_eb_event(), None)
    assert "stored" in out
    events_table.put_item.assert_called_once()


def test_ingest_stores_enriched_row(wired):
    _, events_table, _, _ = wired
    index.ingest_registry_event(_eb_event())
    item = events_table.put_item.call_args.kwargs["Item"]
    assert item["registryId"] == "reg-1"
    assert item["eventKey"] == "2026-09-19T07:00:00Z#evt-1"
    assert item["transition"] == "PENDING_APPROVAL"
    assert item["recordId"] == "rec-1"
    assert item["recordType"] == "SKILL"
    assert item["name"] == "Weather"
    assert item["recordVersion"] == "2"
    assert item["publishedBy"] == "author@erp.local"
    assert item["enrichError"] == ""
    assert isinstance(item["ttl"], int)


def test_ingest_ignores_other_registries(wired):
    _, events_table, _, _ = wired
    out = index.ingest_registry_event(_eb_event(registry_id="someone-elses"))
    assert "skipped" in out
    events_table.put_item.assert_not_called()


def test_ingest_ignores_unrecognised_events(wired):
    _, events_table, _, _ = wired
    out = index.ingest_registry_event(_eb_event(detail_type="Registry Ready"))
    assert "skipped" in out
    events_table.put_item.assert_not_called()


def test_ingest_keeps_the_event_when_the_registry_read_fails(wired):
    control, events_table, _, _ = wired
    control.get_registry_record.side_effect = RuntimeError("ThrottlingException")
    index.ingest_registry_event(_eb_event())
    item = events_table.put_item.call_args.kwargs["Item"]
    assert item["recordId"] == "rec-1"
    assert item["transition"] == "PENDING_APPROVAL"
    assert item["recordType"] == ""
    assert "ThrottlingException" in item["enrichError"]


def test_ingest_reads_the_erp_owner_row_by_record_id_for_skills(wired):
    _, _, skills_table, _ = wired
    index.ingest_registry_event(_eb_event())
    skills_table.get_item.assert_called_once_with(
        Key={"userId": "__erp_owner__", "skillName": "rec-1"})


def test_ingest_reads_the_prefixed_owner_row_for_agent_records(wired):
    control, _, skills_table, _ = wired
    control.get_registry_record.return_value["recordType"] = "AGENT"
    index.ingest_registry_event(_eb_event())
    skills_table.get_item.assert_called_once_with(
        Key={"userId": "__erp_owner__", "skillName": "a2a:rec-1"})


def test_ingest_prefers_an_email_stored_on_the_owner_row(wired):
    _, events_table, skills_table, cognito = wired
    skills_table.get_item.return_value = {"Item": {"ownerSub": "s-1", "ownerEmail": "row@erp.local"}}
    index.ingest_registry_event(_eb_event())
    assert events_table.put_item.call_args.kwargs["Item"]["publishedBy"] == "row@erp.local"
    cognito.list_users.assert_not_called()


def test_ingest_falls_back_to_the_sub_when_cognito_cannot_resolve_it(wired):
    _, events_table, _, cognito = wired
    cognito.list_users.side_effect = RuntimeError("TooManyRequests")
    index.ingest_registry_event(_eb_event())
    assert events_table.put_item.call_args.kwargs["Item"]["publishedBy"] == "s-1"


def test_ingest_reads_meta_published_by_for_builtin_skills(wired):
    # Built-ins are published by the deploy, not through the ERP: no owner row,
    # but the definition says who did it.
    control, events_table, skills_table, _ = wired
    skills_table.get_item.return_value = {}
    control.get_registry_record.return_value["descriptors"] = {
        "agentSkillsDefinition": {"data": json.dumps({"_meta": {"publishedBy": "deploy"}})}}
    index.ingest_registry_event(_eb_event())
    assert events_table.put_item.call_args.kwargs["Item"]["publishedBy"] == "deploy"


def test_ingest_survives_an_owner_lookup_failure(wired):
    _, events_table, skills_table, _ = wired
    skills_table.get_item.side_effect = RuntimeError("ddb down")
    index.ingest_registry_event(_eb_event())
    item = events_table.put_item.call_args.kwargs["Item"]
    assert item["publishedBy"] == ""
    assert item["name"] == "Weather"


# ---------------------------------------------------------------------------
# Read: GET /registry/records?action=events
# ---------------------------------------------------------------------------

def test_events_route_folds_stored_rows(wired):
    _, events_table, _, _ = wired
    events_table.query.return_value = {"Items": [
        {"recordId": "rec-1", "transition": "APPROVED", "eventKey": "2#b", "registryId": "reg-1"},
        {"recordId": "rec-1", "transition": "PENDING_APPROVAL", "eventKey": "1#a", "registryId": "reg-1"},
        {"recordId": "rec-2", "transition": "PENDING_APPROVAL", "eventKey": "0#z", "registryId": "reg-1"},
    ]}
    out = index._dispatch(_http_event(), None)
    assert out["statusCode"] == 200
    body = json.loads(out["body"])
    assert body["pendingCount"] == 1
    assert [e["actionable"] for e in body["events"]] == [False, False, True]
    assert body["generatedAt"]
    kwargs = events_table.query.call_args.kwargs
    assert kwargs["ScanIndexForward"] is False
    assert kwargs["Limit"] == 100


def test_events_route_reports_a_read_failure_as_such(wired):
    _, events_table, _, _ = wired
    events_table.query.side_effect = RuntimeError("table missing")
    out = index._dispatch(_http_event(), None)
    assert out["statusCode"] == 502
    assert "table missing" in json.loads(out["body"])["error"]


def test_events_route_is_admin_only(wired):
    out = index._dispatch(_http_event(groups="users"), None)
    assert out["statusCode"] == 403
