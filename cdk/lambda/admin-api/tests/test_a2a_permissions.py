"""Tests for the A2A permissions handlers in admin-api/index.py.

Covers:
  - GET /users/{userId}/permissions?action=a2a
  - PUT /users/{userId}/permissions?action=a2a
  - GET /registry/records?action=a2a-grants&recordId=X
"""
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
            return _mock_ac

        mclient.side_effect = client_factory
        import index  # noqa: F401
        importlib.reload(index)
        yield
        sys.modules.pop("index", None)


def _get_event(user_id="alice-sub"):
    return {
        "httpMethod": "GET",
        "resource": "/users/{userId}/permissions",
        "pathParameters": {"userId": user_id},
        "queryStringParameters": {"action": "a2a"},
        "requestContext": {"authorizer": {"claims": {
            "sub": "admin-sub",
            "cognito:groups": "admin",
        }}},
    }


def _put_event(user_id, body):
    return {
        "httpMethod": "PUT",
        "resource": "/users/{userId}/permissions",
        "pathParameters": {"userId": user_id},
        "queryStringParameters": {"action": "a2a"},
        "body": json.dumps(body),
        "requestContext": {"authorizer": {"claims": {
            "sub": "admin-sub",
            "cognito:groups": "admin",
        }}},
    }


def _grants_event(record_id):
    return {
        "httpMethod": "GET",
        "resource": "/registry/records",
        "queryStringParameters": {"action": "a2a-grants", "recordId": record_id},
        "requestContext": {"authorizer": {"claims": {
            "sub": "admin-sub",
            "cognito:groups": "admin",
        }}},
    }


def _approved_catalog_mocks():
    """Wire mock control-plane to return 2 APPROVED A2A records."""
    _mock_ac.list_registry_records.return_value = {
        "registryRecords": [
            {"recordId": "rec-energy", "name": "energy-optimization-agent",
             "description": "Energy", "status": "APPROVED", "updatedAt": None},
            {"recordId": "rec-security", "name": "home-security-agent",
             "description": "Security", "status": "APPROVED", "updatedAt": None},
        ]
    }

    def _get(registryId, recordId):
        cards = {
            "rec-energy": {
                "name": "energy-optimization-agent",
                "description": "Energy", "url": "https://x/energy", "version": "1",
                "skills": [
                    {"id": "estimate_savings", "name": "Estimate", "description": "", "examples": []},
                    {"id": "tariff_analysis", "name": "Tariff", "description": "", "examples": []},
                ],
            },
            "rec-security": {
                "name": "home-security-agent",
                "description": "Security", "url": "https://x/security", "version": "1",
                "skills": [
                    {"id": "risk_assessment", "name": "Risk", "description": "", "examples": []},
                    {"id": "incident_response", "name": "IR", "description": "", "examples": []},
                ],
            },
        }
        c = cards[recordId]
        return {"recordId": recordId, "descriptors": {"a2a": {
            "agentCard": {"inlineContent": json.dumps(c)}
        }}}

    _mock_ac.get_registry_record.side_effect = _get


def test_get_empty_row_returns_empty_grants_plus_catalog():
    import index
    _mock_table.get_item.return_value = {}
    _approved_catalog_mocks()

    resp = index.get_user_a2a_permissions(_get_event("alice-sub"))
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["userId"] == "alice-sub"
    assert body["a2aGrants"] == {}
    assert len(body["availableAgents"]) == 2
    ids = {a["recordId"] for a in body["availableAgents"]}
    assert ids == {"rec-energy", "rec-security"}


def test_get_with_existing_grants_returns_them_normalized():
    import index
    _mock_table.get_item.return_value = {
        "Item": {
            "userId": "alice-sub",
            "skillName": "__a2a_permissions__",
            "a2aGrants": {
                "rec-energy": ["tariff_analysis", "estimate_savings"],
            },
            "updatedAt": "2026-05-10T10:00:00Z",
        }
    }
    _approved_catalog_mocks()

    resp = index.get_user_a2a_permissions(_get_event("alice-sub"))
    body = json.loads(resp["body"])
    # Skills are sorted.
    assert body["a2aGrants"] == {"rec-energy": ["estimate_savings", "tariff_analysis"]}
    assert body["updatedAt"] == "2026-05-10T10:00:00Z"


def test_put_writes_valid_grants():
    import index
    _approved_catalog_mocks()

    resp = index.update_user_a2a_permissions(_put_event("alice-sub", {
        "a2aGrants": {
            "rec-energy": ["estimate_savings"],
            "rec-security": ["risk_assessment", "incident_response"],
        }
    }))
    assert resp["statusCode"] == 200, resp["body"]
    body = json.loads(resp["body"])
    assert body["a2aGrants"]["rec-energy"] == ["estimate_savings"]
    assert body["a2aGrants"]["rec-security"] == ["incident_response", "risk_assessment"]
    # Confirm DynamoDB was called.
    _mock_table.put_item.assert_called_once()
    args = _mock_table.put_item.call_args.kwargs["Item"]
    assert args["userId"] == "alice-sub"
    assert args["skillName"] == "__a2a_permissions__"
    assert args["updatedBy"] == "admin-sub"


def test_put_empty_deletes_row():
    import index
    _approved_catalog_mocks()

    resp = index.update_user_a2a_permissions(_put_event("alice-sub", {"a2aGrants": {}}))
    assert resp["statusCode"] == 200
    _mock_table.delete_item.assert_called_once_with(
        Key={"userId": "alice-sub", "skillName": "__a2a_permissions__"}
    )
    _mock_table.put_item.assert_not_called()


def test_put_rejects_unknown_record_id():
    import index
    _approved_catalog_mocks()

    resp = index.update_user_a2a_permissions(_put_event("alice-sub", {
        "a2aGrants": {"rec-ghost": ["estimate_savings"]}
    }))
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert any("rec-ghost" in d for d in body["details"])


def test_put_rejects_bad_skill_id():
    import index
    _approved_catalog_mocks()

    resp = index.update_user_a2a_permissions(_put_event("alice-sub", {
        "a2aGrants": {"rec-energy": ["not_a_real_skill"]}
    }))
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert any("not_a_real_skill" in d for d in body["details"])


def test_put_rejects_non_list_skills():
    import index
    _approved_catalog_mocks()

    resp = index.update_user_a2a_permissions(_put_event("alice-sub", {
        "a2aGrants": {"rec-energy": "estimate_savings"}
    }))
    assert resp["statusCode"] == 400


def test_put_rejects_non_dict_body():
    import index
    resp = index.update_user_a2a_permissions(_put_event("alice-sub", {
        "a2aGrants": "oops"
    }))
    assert resp["statusCode"] == 400


def test_grants_reverse_lookup_finds_matching_users():
    import index
    _mock_table.scan.return_value = {
        "Items": [
            {"userId": "alice", "skillName": "__a2a_permissions__",
             "a2aGrants": {"rec-energy": ["estimate_savings"]},
             "updatedAt": "2026-05-10T10:00:00Z"},
            {"userId": "bob", "skillName": "__a2a_permissions__",
             "a2aGrants": {"rec-energy": ["tariff_analysis"], "rec-security": ["risk_assessment"]},
             "updatedAt": "2026-05-10T11:00:00Z"},
            {"userId": "carol", "skillName": "__a2a_permissions__",
             "a2aGrants": {"rec-security": ["incident_response"]},
             "updatedAt": "2026-05-10T12:00:00Z"},
        ]
    }

    resp = index.list_a2a_grants_for_record(_grants_event("rec-energy"))
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["recordId"] == "rec-energy"
    users = {g["userId"]: g["skillIds"] for g in body["grants"]}
    assert users == {
        "alice": ["estimate_savings"],
        "bob": ["tariff_analysis"],
    }


def test_grants_reverse_lookup_empty():
    import index
    _mock_table.scan.return_value = {"Items": []}
    resp = index.list_a2a_grants_for_record(_grants_event("rec-energy"))
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["grants"] == []


def test_grants_reverse_lookup_requires_record_id():
    import index
    ev = _grants_event("")
    ev["queryStringParameters"]["recordId"] = ""
    resp = index.list_a2a_grants_for_record(ev)
    assert resp["statusCode"] == 400


def test_put_resolves_sub_to_email_for_ddb_key():
    """UI passes Cognito sub; agent reads by email. The backend must translate
    sub → email before writing the DDB row, or grants silently do not apply."""
    import index
    _approved_catalog_mocks()

    # Mock the cognito-idp lookup: sub → email.
    _mock_ac.list_users.return_value = {
        "Users": [{
            "Username": "alice",
            "Attributes": [
                {"Name": "sub", "Value": "88c1a3e0-b041-707c-01ae-3ac4d821adbd"},
                {"Name": "email", "Value": "alice@example.com"},
            ],
        }]
    }

    resp = index.update_user_a2a_permissions(_put_event(
        "88c1a3e0-b041-707c-01ae-3ac4d821adbd", {
            "a2aGrants": {"rec-energy": ["estimate_savings"]}
        }
    ))
    assert resp["statusCode"] == 200, resp["body"]
    args = _mock_table.put_item.call_args.kwargs["Item"]
    assert args["userId"] == "alice@example.com", (
        f"expected alice@example.com (matches agent runtime key); got {args['userId']}"
    )


def test_put_global_bypasses_sub_resolution():
    """__global__ must not hit the Cognito list_users API."""
    import index
    _approved_catalog_mocks()
    _mock_ac.list_users.reset_mock()

    resp = index.update_user_a2a_permissions(_put_event("__global__", {
        "a2aGrants": {"rec-energy": ["estimate_savings"]}
    }))
    assert resp["statusCode"] == 200
    _mock_ac.list_users.assert_not_called()
    args = _mock_table.put_item.call_args.kwargs["Item"]
    assert args["userId"] == "__global__"


def test_put_passthroughs_email_unchanged():
    """If the UI already passed an email (e.g., legacy), do not re-resolve."""
    import index
    _approved_catalog_mocks()
    _mock_ac.list_users.reset_mock()

    resp = index.update_user_a2a_permissions(_put_event("bob@example.com", {
        "a2aGrants": {"rec-energy": ["estimate_savings"]}
    }))
    assert resp["statusCode"] == 200
    _mock_ac.list_users.assert_not_called()
    args = _mock_table.put_item.call_args.kwargs["Item"]
    assert args["userId"] == "bob@example.com"


# ---------------------------------------------------------------------------
# An empty catalog and an unreadable catalog must not look the same
#
# Both used to produce `availableAgents: []` inside a 200. The console rendered
# "No approved A2A agents in the registry" either way, so a wrong REGISTRY_ID and
# a missing agent-registry:ListRegistryRecords grant both presented as "nothing to
# approve yet" — and the only evidence was a warning in a Lambda log. That
# ambiguity cost a real misdiagnosis: the empty list was attributed to a stale
# botocore and a missing IAM action, when the registry id was simply pointing at a
# different registry.
# ---------------------------------------------------------------------------

def test_a_genuinely_empty_registry_reports_no_error():
    """The ordinary empty case must stay quiet, or the warning means nothing."""
    import index
    _mock_table.get_item.return_value = {}
    _mock_ac.list_registry_records.side_effect = None
    _mock_ac.list_registry_records.return_value = {"registryRecords": []}

    resp = index.get_user_a2a_permissions(_get_event("alice-sub"))
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert body["availableAgents"] == []
    assert body["catalogError"] == ""


def test_an_unreadable_registry_reports_why():
    import index
    _mock_table.get_item.return_value = {}
    _mock_ac.list_registry_records.side_effect = Exception(
        "ResourceNotFoundException: Registry with ID gqrzwR9mtoL1Y0UK not found.")

    resp = index.get_user_a2a_permissions(_get_event("alice-sub"))
    body = json.loads(resp["body"])
    # Still a 200: the page needs the user's existing grants regardless, and
    # failing the whole request would hide them too.
    assert resp["statusCode"] == 200
    assert body["availableAgents"] == []
    assert body["catalogError"], "a Registry failure must be reported, not swallowed"
    assert "gqrzwR9mtoL1Y0UK" in body["catalogError"], (
        "the message must carry enough detail to tell the causes apart")
    _mock_ac.list_registry_records.side_effect = None


def test_existing_grants_survive_a_catalog_failure():
    """The stale-grant filter must not fire when the catalog simply failed to
    load — otherwise one Registry error looks like a mass revocation."""
    import index
    _mock_table.get_item.return_value = {"Item": {
        "userId": "alice@example.com",
        "a2aGrants": {"rec-energy": ["estimate_savings"]},
    }}
    _mock_ac.list_registry_records.side_effect = Exception("AccessDeniedException")

    resp = index.get_user_a2a_permissions(_get_event("alice-sub"))
    body = json.loads(resp["body"])
    assert body["a2aGrants"] == {"rec-energy": ["estimate_savings"]}
    assert body["staleGrants"] == []
    assert body["catalogError"]
    _mock_ac.list_registry_records.side_effect = None


# ---------------------------------------------------------------------------
# A global save must not discard per-user overrides (found live 2026-08-13)
# ---------------------------------------------------------------------------

def test_a_global_save_honours_an_email_keyed_per_user_override():
    """The bug an admin actually hit: a revoke that came back.

    This pool has `UsernameAttributes: ['email']`, so a user has a generated UUID
    `Username` AND an email, and Cognito's admin APIs accept either. Grant intent,
    though, is stored under the EMAIL (`_resolve_ddb_user_key` normalises to it,
    because that is the key the agent reads per-user rows by at runtime).

    `list_users` returns the UUID. So a change to `__global__` used to read each
    user's override under the UUID, find nothing, fall back to global, and re-grant
    everything — discarding every per-user narrowing while reporting success.
    Observed live: `advisory_review` was removed from one user, the page saved it,
    and a later global save put the group back, so the specialist kept answering.
    """
    import index

    users = {"Username": "78d153c0-uuid",
             "Attributes": [{"Name": "email", "Value": "zihangh@amazon.com"},
                            {"Name": "sub", "Value": "78d153c0-uuid"}]}
    with patch.object(index, "COGNITO_USER_POOL_ID", "pool"), \
         patch.object(index, "cognito_client") as cog:
        cog.list_users.return_value = {"Users": [users]}
        affected = index._affected_users("__global__")

    assert affected == [("78d153c0-uuid", "zihangh@amazon.com")], (
        "the Cognito username and the DynamoDB intent key must be carried "
        "separately; one string means a global save cannot see an override")


def test_a_per_user_save_resolves_to_the_key_the_write_path_used():
    import index

    with patch.object(index, "_resolve_ddb_user_key",
                      side_effect=lambda v: "zihangh@amazon.com"):
        assert index._affected_users("78d153c0-uuid") == [
            ("78d153c0-uuid", "zihangh@amazon.com")]


def test_a_user_with_no_email_falls_back_to_the_username():
    """An override written under the literal username must still be honoured."""
    import index

    with patch.object(index, "COGNITO_USER_POOL_ID", "pool"), \
         patch.object(index, "cognito_client") as cog:
        cog.list_users.return_value = {"Users": [{"Username": "no-email-user",
                                                  "Attributes": []}]}
        assert index._affected_users("__global__") == [
            ("no-email-user", "no-email-user")]
