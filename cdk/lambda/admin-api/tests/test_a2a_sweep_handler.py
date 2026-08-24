"""Tests for the sweep HANDLER — the part that decides whether to act at all.

The pure "which groups go" logic lives in test_a2a_sweep.py. What is tested here is
the refusal to act on a registry it could not read, because the two dangerous inputs
are indistinguishable by value: "no records are grantable" and "I could not find out
which records are grantable" both arrive as an empty list. Acting on the second
revokes every A2A group in the pool, and the same conflation in
`get_user_a2a_permissions` once presented to an admin as a mass revocation.
"""
import importlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

_mock_table = MagicMock()
_mock_client = MagicMock()


@pytest.fixture(scope="module", autouse=True)
def _install_mocks():
    lambda_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    if sys.path[0] != lambda_dir:
        sys.path.insert(0, lambda_dir)
    sys.modules.pop("index", None)
    os.environ["COGNITO_USER_POOL_ID"] = "us-west-2_TEST"
    with patch("boto3.resource") as mres, patch("boto3.client") as mclient:
        mres.return_value.Table.return_value = _mock_table
        mclient.side_effect = lambda name, **kw: _mock_client
        import index  # noqa: F401
        importlib.reload(index)
        yield
        sys.modules.pop("index", None)


@pytest.fixture(autouse=True)
def _reset():
    _mock_table.reset_mock(return_value=True, side_effect=True)
    _mock_client.reset_mock(return_value=True, side_effect=True)


NOW = datetime.now(timezone.utc)


def _record(name, status="APPROVED", age=timedelta(days=1)):
    return {
        "recordId": f"rec-{name}",
        "name": name,
        "status": status,
        "updatedAt": NOW - age,
    }


def _wire_registry(records, groups, holders=None):
    """Make the mocked clients answer as one registry plus one user pool."""
    holders = holders or {}
    _mock_client.list_registry_records.return_value = {"registryRecords": records}
    _mock_client.get_registry_record.side_effect = lambda **kw: {
        "descriptors": {"a2aAgentCard": {"data": json.dumps({
            "name": next(r["name"] for r in records
                         if r["recordId"] == kw["recordId"]),
            "skills": [{"id": "s1", "name": "S1", "description": "d"}],
        })}}
    }
    _mock_client.list_groups.return_value = {
        "Groups": [{"GroupName": g} for g in groups]}
    _mock_client.list_users_in_group.side_effect = lambda **kw: {
        "Users": [{"Username": u} for u in holders.get(kw["GroupName"], [])]}


def _body(resp):
    return json.loads(resp["body"])


def test_everything_approved_revokes_nothing():
    import index

    _wire_registry([_record("knowledge-qa-agent")],
                   ["a2a-knowledge-qa-agent.s1"])
    out = _body(index.sweep_a2a_revocations({}))
    assert out["swept"] is True
    assert out["revokedGroups"] == []
    assert _mock_client.admin_remove_user_from_group.call_count == 0


def test_a_rejected_record_loses_its_groups():
    import index

    _wire_registry(
        [_record("knowledge-qa-agent"),
         _record("energy-optimization-agent", status="REJECTED")],
        ["a2a-knowledge-qa-agent.s1", "a2a-energy-optimization-agent.s1"],
        holders={"a2a-energy-optimization-agent.s1": ["alice"]})
    out = _body(index.sweep_a2a_revocations({}))
    assert out["revokedGroups"] == ["a2a-energy-optimization-agent.s1"]
    assert out["affectedUsers"] == {"alice": ["a2a-energy-optimization-agent.s1"]}
    # And the reason travels, so the log answers "who took my access away".
    assert "REJECTED" in out["reasons"]["rec-energy-optimization-agent"]


def test_a_fresh_draft_keeps_its_groups():
    """The version-bump case. §2.2: an edit resets APPROVED to DRAFT, and approval
    is a human step, so revoking here would make every deploy an outage."""
    import index

    _wire_registry(
        [_record("energy-optimization-agent", status="DRAFT",
                 age=timedelta(minutes=5))],
        ["a2a-energy-optimization-agent.s1"],
        holders={"a2a-energy-optimization-agent.s1": ["alice"]})
    out = _body(index.sweep_a2a_revocations({}))
    assert out["revokedGroups"] == []


def test_a_stale_draft_loses_its_groups():
    import index

    _wire_registry(
        [_record("energy-optimization-agent", status="DRAFT",
                 age=timedelta(hours=3))],
        ["a2a-energy-optimization-agent.s1"],
        holders={"a2a-energy-optimization-agent.s1": ["alice"]})
    out = _body(index.sweep_a2a_revocations({}))
    assert out["revokedGroups"] == ["a2a-energy-optimization-agent.s1"]


def test_a_registry_error_refuses_to_sweep():
    """Not "nothing is grantable" — "I could not find out"."""
    import index

    _mock_client.list_registry_records.side_effect = RuntimeError("throttled")
    _mock_client.list_groups.return_value = {
        "Groups": [{"GroupName": "a2a-knowledge-qa-agent.s1"}]}
    resp = index.sweep_a2a_revocations({})
    assert resp["statusCode"] == 503
    assert _body(resp)["swept"] is False
    assert _mock_client.admin_remove_user_from_group.call_count == 0


def test_an_empty_registry_refuses_to_sweep():
    """Zero records with no error is indistinguishable from a half-provisioned
    registry, and acting on it would empty the pool."""
    import index

    _wire_registry([], ["a2a-knowledge-qa-agent.s1"],
                   holders={"a2a-knowledge-qa-agent.s1": ["alice"]})
    resp = index.sweep_a2a_revocations({})
    assert resp["statusCode"] == 503
    assert _body(resp)["swept"] is False
    assert _mock_client.admin_remove_user_from_group.call_count == 0


def test_the_scheduled_event_reaches_the_sweep():
    """EventBridge sends no HTTP envelope; without this branch it 404s silently."""
    import index

    _wire_registry([_record("knowledge-qa-agent")],
                   ["a2a-knowledge-qa-agent.s1"])
    out = _body(index.handler({"task": "a2a-sweep"}, None))
    assert out["swept"] is True
