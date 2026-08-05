"""Tests for code-interpreter session Lambda handler.

Parallels test_browser_files.py: covers the code-active row lookup and that
it reuses the same self-or-admin guard. Workspace file probing (chart loads)
does NOT go through this Lambda — the chatbot calls InvokeAgentRuntimeCommand
directly — so it is not covered here.
"""
import json
import importlib


def _index():
    return importlib.import_module("index")


def _event(user_id: str, groups: str = ""):
    return {
        "httpMethod": "GET",
        "resource": "/sessions",
        "queryStringParameters": {"action": "code-active", "userId": user_id},
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


def test_handle_code_sessions_active_returns_latest_by_started_at(monkeypatch):
    m = _index()

    class FakeTable:
        def query(self, **_):
            return {"Items": [
                {"userId": "caller@example.com", "sessionId": "code_older",
                 "status": "idle", "startedAt": "2026-06-20T08:00:00Z",
                 "steps": []},
                {"userId": "caller@example.com", "sessionId": "code_newer",
                 "status": "running", "startedAt": "2026-06-20T09:00:00Z",
                 "steps": [{"index": 0, "title": "Aggregate", "code": "print(1)",
                            "stdout": "1\n", "stderr": "", "exitCode": 0,
                            "charts": [], "status": "running"}]},
            ]}

    monkeypatch.setattr(m, "_code_sessions_table", lambda: FakeTable())
    ev = _event(user_id="caller@example.com")
    resp = m.handle_code_sessions_active(ev)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["sessionId"] == "code_newer"
    assert body["status"] == "running"
    assert body["steps"][0]["title"] == "Aggregate"


def test_handle_code_sessions_active_empty_when_no_rows(monkeypatch):
    m = _index()

    class FakeTable:
        def query(self, **_):
            return {"Items": []}

    monkeypatch.setattr(m, "_code_sessions_table", lambda: FakeTable())
    resp = m.handle_code_sessions_active(_event(user_id="caller@example.com"))
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {}


def test_handle_code_sessions_active_requires_user_id():
    m = _index()
    ev = _event(user_id="")
    ev["queryStringParameters"]["userId"] = ""
    resp = m.handle_code_sessions_active(ev)
    assert resp["statusCode"] == 400


def test_handle_code_sessions_active_rejects_cross_user():
    m = _index()
    resp = m.handle_code_sessions_active(_event(user_id="someone@else.com"))
    assert resp["statusCode"] == 403


def test_router_dispatches_code_active(monkeypatch):
    """GET /sessions?action=code-active routes to the code handler without
    requiring admin (the public dispatch branch)."""
    m = _index()
    called = {}

    def _fake(ev):
        called["yes"] = True
        return m.response(200, {"ok": True})

    monkeypatch.setattr(m, "handle_code_sessions_active", _fake)
    resp = m.handler(_event(user_id="caller@example.com"), None)
    assert called.get("yes") is True
    assert resp["statusCode"] == 200
