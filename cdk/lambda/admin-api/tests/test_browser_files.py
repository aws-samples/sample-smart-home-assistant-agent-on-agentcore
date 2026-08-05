"""Tests for browser-sessions Lambda handlers.

Per arch §9.10 the workspace file probing does NOT go through this Lambda
(the chatbot calls InvokeAgentRuntimeCommand directly), so these tests
cover only the browser-active row lookup and the self-or-admin helper.
"""
import importlib


def _index():
    return importlib.import_module("index")


def _event(user_id: str, groups: str = ""):
    return {
        "httpMethod": "GET",
        "resource": "/sessions",
        "queryStringParameters": {"action": "browser-active", "userId": user_id},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "email": "caller@example.com",
                    "sub": "caller-sub",
                    "cognito:groups": groups,
                },
            },
        },
    }


def test_caller_id_prefers_email():
    m = _index()
    ev = _event(user_id="foo@bar.com")
    assert m._caller_id(ev) == "caller@example.com"


def test_require_self_or_admin_allows_caller_for_own_id():
    m = _index()
    ev = _event(user_id="caller@example.com")
    assert m._require_self_or_admin(ev, "caller@example.com") is None


def test_require_self_or_admin_allows_admin_for_any_id():
    m = _index()
    ev = _event(user_id="someone@else.com", groups="admin")
    assert m._require_self_or_admin(ev, "someone@else.com") is None


def test_require_self_or_admin_blocks_cross_user():
    m = _index()
    ev = _event(user_id="someone@else.com")
    resp = m._require_self_or_admin(ev, "someone@else.com")
    assert resp is not None
    assert resp["statusCode"] == 403


def test_handle_browser_sessions_active_returns_latest_by_started_at(monkeypatch):
    """Most-recent row wins regardless of status — the chatbot decides
    whether to render live view based on the returned status field."""
    m = _index()

    class FakeTable:
        def query(self, **_):
            return {"Items": [
                {"userId": "caller@example.com", "sessionId": "bsid_older",
                 "status": "completed", "startedAt": "2026-05-04T08:00:00Z"},
                {"userId": "caller@example.com", "sessionId": "bsid_newer",
                 "status": "running", "startedAt": "2026-05-04T09:00:00Z",
                 "liveViewUrl": "u"},
            ]}

    monkeypatch.setattr(m, "_browser_sessions_table", lambda: FakeTable())
    ev = _event(user_id="caller@example.com")
    resp = m.handle_browser_sessions_active(ev)
    assert resp["statusCode"] == 200
    import json
    body = json.loads(resp["body"])
    assert body["sessionId"] == "bsid_newer"
    assert body["status"] == "running"


def test_handle_browser_sessions_active_empty_when_no_rows(monkeypatch):
    m = _index()

    class FakeTable:
        def query(self, **_):
            return {"Items": []}

    monkeypatch.setattr(m, "_browser_sessions_table", lambda: FakeTable())
    ev = _event(user_id="caller@example.com")
    resp = m.handle_browser_sessions_active(ev)
    assert resp["statusCode"] == 200
    import json
    assert json.loads(resp["body"]) == {}


def test_handle_browser_sessions_active_requires_user_id():
    m = _index()
    ev = _event(user_id="")
    ev["queryStringParameters"]["userId"] = ""
    resp = m.handle_browser_sessions_active(ev)
    assert resp["statusCode"] == 400


def test_handle_browser_sessions_active_rejects_cross_user():
    m = _index()
    ev = _event(user_id="someone@else.com")
    resp = m.handle_browser_sessions_active(ev)
    assert resp["statusCode"] == 403
