"""Tests for A2A route handlers in skill-erp-api/index.py (boto3 mocked)."""
import json
from unittest.mock import MagicMock, patch

import pytest

# Patch boto3 clients BEFORE importing the handler module so module-level
# `dynamodb.resource(...)` / `boto3.client(...)` calls use mocks.
_mock_table = MagicMock()
_mock_ac = MagicMock()


@pytest.fixture(autouse=True)
def _reset_mocks():
    _mock_table.reset_mock()
    _mock_ac.reset_mock()
    yield


@pytest.fixture(scope="module", autouse=True)
def _install_mocks():
    # Both Lambda dirs define index.py; force skill-erp-api's copy to win by
    # putting its dir at the front of sys.path and dropping any cached module.
    import os, sys, importlib
    lambda_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir)
    )
    if sys.path[0] != lambda_dir:
        sys.path.insert(0, lambda_dir)
    sys.modules.pop("index", None)

    with patch("boto3.resource") as mres, patch("boto3.client") as mclient:
        mres.return_value.Table.return_value = _mock_table
        mclient.return_value = _mock_ac
        import index  # noqa: F401 — imported for side effects of module-level boto3.*
        importlib.reload(index)
        yield
        sys.modules.pop("index", None)


def _event(method, resource, body=None, path=None, caller_sub="alice-sub", caller_email="alice@example.com"):
    ev = {
        "httpMethod": method,
        "resource": resource,
        "requestContext": {
            "authorizer": {
                "claims": {"sub": caller_sub, "email": caller_email}
            }
        },
    }
    if body is not None:
        ev["body"] = json.dumps(body)
    if path is not None:
        ev["pathParameters"] = path
    return ev


def _valid_form():
    return {
        "name": "my-agent",
        "description": "Test agent",
        "endpoint": "https://example.com/a",
        "version": "1.0.0",
        "provider": "Test",
        "capabilities": {"streaming": True, "pushNotifications": False, "stateTransitionHistory": False},
        "auth": "none",
        "tags": ["demo"],
        "skills": [{"id": "s1", "name": "Skill 1", "description": "d", "examples": ["e"]}],
    }


def test_post_rejects_invalid_form():
    import index
    ev = _event("POST", "/my-a2a-agents", body={**_valid_form(), "name": "BAD"})
    resp = index.handler(ev, None)
    assert resp["statusCode"] == 400
    assert "name" in json.loads(resp["body"])["error"].lower()


def test_post_creates_record_and_submits_for_approval():
    import index
    _mock_ac.create_registry_record.return_value = {
        "recordArn": "arn:aws:bedrock-agentcore:us-west-2:111:registry/r/record/rec-123"
    }
    _mock_ac.get_registry_record.return_value = {"status": "ACTIVE"}

    ev = _event("POST", "/my-a2a-agents", body=_valid_form())
    resp = index.handler(ev, None)
    assert resp["statusCode"] == 201, resp["body"]

    # CreateRegistryRecord called with A2A descriptor type
    kwargs = _mock_ac.create_registry_record.call_args.kwargs
    assert kwargs["descriptorType"] == "A2A"
    assert "agentCard" in kwargs["descriptors"]["a2a"]

    # Ownership row written with a2a: prefix
    put_kwargs = _mock_table.put_item.call_args.kwargs
    assert put_kwargs["Item"]["userId"] == "__erp_owner__"
    assert put_kwargs["Item"]["skillName"] == "a2a:rec-123"
    assert put_kwargs["Item"]["recordType"] == "a2a"
    assert put_kwargs["Item"]["ownerSub"] == "alice-sub"
    assert put_kwargs["Item"]["ownerEmail"] == "alice@example.com"

    # SubmitRegistryRecordForApproval called
    _mock_ac.submit_registry_record_for_approval.assert_called_once_with(
        registryId="test-registry-id", recordId="rec-123"
    )


def test_delete_rejects_non_owner():
    import index
    _mock_table.get_item.return_value = {
        "Item": {"ownerSub": "bob-sub", "recordType": "a2a"}
    }
    ev = _event("DELETE", "/my-a2a-agents/{recordId}", path={"recordId": "rec-xyz"})
    resp = index.handler(ev, None)
    assert resp["statusCode"] == 403


def test_delete_removes_record_and_ownership_row():
    import index
    _mock_table.get_item.return_value = {
        "Item": {"ownerSub": "alice-sub", "recordType": "a2a"}
    }
    ev = _event("DELETE", "/my-a2a-agents/{recordId}", path={"recordId": "rec-xyz"})
    resp = index.handler(ev, None)
    assert resp["statusCode"] == 200
    _mock_ac.delete_registry_record.assert_called_once_with(
        registryId="test-registry-id", recordId="rec-xyz"
    )
    del_kwargs = _mock_table.delete_item.call_args.kwargs
    assert del_kwargs["Key"] == {"userId": "__erp_owner__", "skillName": "a2a:rec-xyz"}
