import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SKILLS_TABLE_NAME", "smarthome-skills-test")
    yield


def _event(method="GET", path_params=None, query=None, body=None, claims=None):
    return {
        "httpMethod": method,
        "pathParameters": path_params or {},
        "queryStringParameters": query or {},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {"claims": claims or {"email": "admin@example.com",
                                                  "cognito:groups": "admin"}}
        },
    }


def test_list_returns_all_overrides():
    import tenant_env
    table = MagicMock()
    table.query.return_value = {
        "Items": [
            {"userId": "__global__", "skillName": "__tenant_env_alice@x.com__",
             "mode": "ab-bundles", "updatedAt": "2026-06-13", "updatedBy": "admin@x.com"},
            {"userId": "__global__", "skillName": "__tenant_env_bob@x.com__",
             "mode": "ab-targets", "updatedAt": "2026-06-12", "updatedBy": "admin@x.com"},
        ]
    }
    with patch.object(tenant_env, "_table", return_value=table):
        resp = tenant_env.list_tenant_envs(_event())
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert len(body["overrides"]) == 2
    assert {o["email"] for o in body["overrides"]} == {"alice@x.com", "bob@x.com"}


def test_get_returns_default_when_row_absent():
    import tenant_env
    table = MagicMock()
    table.get_item.return_value = {}
    with patch.object(tenant_env, "_table", return_value=table):
        resp = tenant_env.get_tenant_env(_event(query={"tenantEnv": "1", "userId": "alice@x.com"}))
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["mode"] == "default"


def test_put_blocks_when_per_user_prompt_exists_without_ack():
    import tenant_env
    table = MagicMock()
    table.get_item.side_effect = [
        {"Item": {"userId": "alice@x.com", "skillName": "__prompt_text__", "promptBody": "..."}},
    ]
    body = {"mode": "ab-bundles"}
    with patch.object(tenant_env, "_table", return_value=table):
        resp = tenant_env.put_tenant_env(
            _event(method="PUT", path_params={"userId": "alice@x.com"}, body=body))
    assert resp["statusCode"] == 409
    assert json.loads(resp["body"])["error"] == "PerUserPromptWillBeMasked"


def test_put_succeeds_with_ack():
    import tenant_env
    table = MagicMock()
    table.get_item.return_value = {"Item": {"promptBody": "x"}}
    body = {"mode": "ab-bundles", "acknowledgeMaskedOverride": True}
    with patch.object(tenant_env, "_table", return_value=table):
        resp = tenant_env.put_tenant_env(
            _event(method="PUT", path_params={"userId": "alice@x.com"}, body=body))
    assert resp["statusCode"] == 200
    table.put_item.assert_called_once()


def test_put_succeeds_with_no_existing_prompt_row():
    import tenant_env
    table = MagicMock()
    table.get_item.return_value = {}
    body = {"mode": "ab-bundles"}
    with patch.object(tenant_env, "_table", return_value=table):
        resp = tenant_env.put_tenant_env(
            _event(method="PUT", path_params={"userId": "alice@x.com"}, body=body))
    assert resp["statusCode"] == 200
    table.put_item.assert_called_once()


def test_put_rejects_invalid_mode():
    import tenant_env
    body = {"mode": "bogus"}
    resp = tenant_env.put_tenant_env(
        _event(method="PUT", path_params={"userId": "alice@x.com"}, body=body))
    assert resp["statusCode"] == 400


def test_delete_removes_row():
    import tenant_env
    table = MagicMock()
    with patch.object(tenant_env, "_table", return_value=table):
        resp = tenant_env.delete_tenant_env(
            _event(method="DELETE", path_params={"userId": "alice@x.com"}))
    assert resp["statusCode"] == 200
    table.delete_item.assert_called_once_with(
        Key={"userId": "__global__", "skillName": "__tenant_env_alice@x.com__"})


def test_index_dispatches_get_when_query_flag_set():
    import index, tenant_env
    with patch.object(tenant_env, "list_tenant_envs", return_value={"statusCode": 200, "body": "{}"}) as h:
        event = {"resource": "/skills", "httpMethod": "GET",
                 "queryStringParameters": {"tenantEnv": "1"},
                 "pathParameters": {}, "body": None,
                 "requestContext": {"authorizer": {"claims": {"email": "x@y", "cognito:groups": "admin"}}}}
        index.handler(event, None)
    h.assert_called_once()


def test_index_dispatches_put_for_tenant_env_sk():
    import index, tenant_env
    with patch.object(tenant_env, "put_tenant_env", return_value={"statusCode": 200, "body": "{}"}) as h:
        event = {"resource": "/skills/{userId}/{skillName}", "httpMethod": "PUT",
                 "queryStringParameters": None,
                 "pathParameters": {"userId": "alice@x.com", "skillName": "__tenant_env__"},
                 "body": '{"mode":"default"}',
                 "requestContext": {"authorizer": {"claims": {"email": "x@y", "cognito:groups": "admin"}}}}
        index.handler(event, None)
    h.assert_called_once()
