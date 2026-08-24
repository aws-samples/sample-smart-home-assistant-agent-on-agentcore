"""Tests for the admin-api list_a2a_agents handler."""
import json
from unittest.mock import MagicMock, patch

import pytest

_mock_table = MagicMock()
_mock_ac = MagicMock()


@pytest.fixture(autouse=True)
def _reset():
    _mock_table.reset_mock()
    _mock_ac.reset_mock()


@pytest.fixture(scope="module", autouse=True)
def _install_mocks():
    # Both Lambda dirs define index.py; force admin-api's copy to win.
    import os, sys, importlib
    lambda_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir)
    )
    if sys.path[0] != lambda_dir:
        sys.path.insert(0, lambda_dir)
    sys.modules.pop("index", None)

    with patch("boto3.resource") as mres, patch("boto3.client") as mclient:
        mres.return_value.Table.return_value = _mock_table

        def client_factory(name, **kw):
            return _mock_ac  # same mock for every named client in this test

        mclient.side_effect = client_factory
        import index  # noqa: F401
        importlib.reload(index)
        yield
        sys.modules.pop("index", None)


def _event():
    return {
        "httpMethod": "GET",
        "resource": "/registry/records",
        "queryStringParameters": {"action": "a2a-list"},
        "requestContext": {"authorizer": {"claims": {
            "sub": "admin-sub",
            "cognito:groups": "admin",
        }}},
    }


def test_list_passes_correct_filters_to_boto():
    import index
    _mock_ac.list_registry_records.return_value = {"registryRecords": []}
    _mock_table.scan.return_value = {"Items": []}

    resp = index.list_a2a_agents(_event())
    assert resp["statusCode"] == 200, resp["body"]

    # GA: descriptorType and the flat status parameter are gone, replaced by a
    # structured filters list. recordType=AGENT is the equivalent of A2A.
    kw = _mock_ac.list_registry_records.call_args.kwargs
    assert "descriptorType" not in kw
    assert "status" not in kw
    assert {"name": "recordType", "values": ["AGENT"]} in kw["filters"]
    # And NOT filtered by status. This page used to list APPROVED records only,
    # which hid an agent exactly when an admin came looking for it — "access to the
    # energy specialist disappeared" has no answer on a page that shows only
    # healthy records. Grantability is reported per row instead.
    assert not any(f["name"] == "status" for f in kw["filters"])


def test_list_joins_published_by_from_ownership_rows():
    import index
    _mock_ac.list_registry_records.return_value = {
        "registryRecords": [
            {"recordId": "r1", "name": "agent-one", "description": "d1",
             "status": "APPROVED", "updatedAt": None},
            {"recordId": "r2", "name": "agent-two", "description": "d2",
             "status": "APPROVED", "updatedAt": None},
        ]
    }
    _mock_ac.get_registry_record.side_effect = [
        {
            "recordId": "r1",
            "descriptors": {"a2aAgentCard": {"data": json.dumps({
                    "name": "agent-one", "description": "d1",
                    "url": "https://x/one", "version": "1",
                    "capabilities": {"streaming": True},
                    "authentication": {"schemes": ["none"]},
                    "tags": ["t1"],
                    "skills": [{"id": "s", "name": "S", "description": "", "examples": []}],
                })}},
        },
        {
            "recordId": "r2",
            "descriptors": {"a2aAgentCard": {"data": json.dumps({
                    "name": "agent-two", "description": "d2",
                    "url": "https://x/two", "version": "1",
                    "capabilities": {},
                    "authentication": {"schemes": ["bearer"]},
                    "tags": [],
                    "skills": [{"id": "s", "name": "S", "description": "", "examples": []}],
                })}},
        },
    ]
    _mock_table.scan.return_value = {"Items": [
        {"userId": "__erp_owner__", "skillName": "a2a:r1",
         "recordType": "a2a", "ownerSub": "alice-sub", "ownerEmail": "alice@example.com"},
        # r2 has no ownership row
    ]}

    resp = index.list_a2a_agents(_event())
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    recs = body["records"]
    assert len(recs) == 2
    assert recs[0]["publishedBy"] == "alice@example.com"
    assert recs[1]["publishedBy"] == ""


def test_list_tolerates_get_record_failure_on_a_single_item():
    import index
    _mock_ac.list_registry_records.return_value = {
        "registryRecords": [
            {"recordId": "r1", "name": "a1", "description": "d",
             "status": "APPROVED", "updatedAt": None},
            {"recordId": "r2", "name": "a2", "description": "d",
             "status": "APPROVED", "updatedAt": None},
        ]
    }

    def get_side_effect(registryId, recordId):
        if recordId == "r2":
            raise RuntimeError("boom")
        return {"recordId": "r1", "descriptors": {"a2a": {
            "agentCard": {"inlineContent": json.dumps({
                "name": "a1", "description": "d", "url": "https://x", "version": "1",
                "capabilities": {}, "authentication": {"schemes": ["none"]},
                "tags": [], "skills": [{"id": "s", "name": "S", "description": "", "examples": []}],
            })}}}}
    _mock_ac.get_registry_record.side_effect = get_side_effect
    _mock_table.scan.return_value = {"Items": []}

    resp = index.list_a2a_agents(_event())
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert len(body["records"]) == 1
    assert body["records"][0]["recordId"] == "r1"
