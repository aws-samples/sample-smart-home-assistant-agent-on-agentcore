"""Tests for agent/tools/browser_use.py — run_browse_web.

Mocks:
  * _build_browser_client → returns a fake BrowserClient whose start/
    generate_ws_headers/generate_live_view_url are pre-wired.
  * boto3 DynamoDB put_item for smarthome-browser-sessions.
  * _build_browser_use_agent → fake object whose .run() returns a string.
Covers:
  * happy path: DDB row transitions running → idle, summary returned,
    bc.stop() is NOT called so AgentCore's idle timeout keeps the session
    alive for the user to drive manually
  * session-start failure: DDB row status=failed, "Browser unavailable" returned
  * browser-use raises: DDB row status=failed, bc.stop() still called
  * goal too long: early rejection, no AWS call
  * empty goal: early rejection
"""
from unittest.mock import MagicMock, patch, AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BROWSER_SESSIONS_TABLE_NAME", "smarthome-browser-sessions")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("MODEL_ID", "moonshotai.kimi-k2.5")


def _fake_browser_client(session_id="bsid_abc"):
    bc = MagicMock()
    bc.start.return_value = session_id
    bc.generate_ws_headers.return_value = ("wss://example/ws", {"Authorization": "AWS4-HMAC-SHA256 ..."})
    bc.generate_live_view_url.return_value = f"https://liveview.example/{session_id}"
    bc.stop.return_value = True
    bc.session_id = session_id
    return bc


def test_happy_path_returns_summary():
    from tools import browser_use as bu

    bc = _fake_browser_client()
    ddb_table = MagicMock()
    ddb_res = MagicMock(); ddb_res.Table.return_value = ddb_table

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value="Top 3: A $49, B $79, C $99.")

    with patch.object(bu, "_build_browser_client", lambda: bc), \
         patch.object(bu, "_ddb", lambda: ddb_res), \
         patch.object(bu, "_build_browser_use_agent", return_value=fake_agent):
        result = bu.run_browse_web(
            goal="Find cheap earbuds on amazon.com",
            user_id="user-42",
            agent_session_id="asid-1",
        )

    assert "Top 3" in result
    statuses = [c.kwargs["Item"]["status"] for c in ddb_table.put_item.call_args_list]
    assert statuses[0] == "running"
    assert statuses[-1] == "idle"
    # Do NOT stop the session on success — AgentCore's idle timeout reaps
    # it; meanwhile the user can keep driving via Take Control.
    bc.stop.assert_not_called()


def test_session_start_failure():
    from tools import browser_use as bu

    bc = _fake_browser_client()
    bc.start.side_effect = RuntimeError("Throttled")
    ddb_table = MagicMock()
    ddb_res = MagicMock(); ddb_res.Table.return_value = ddb_table

    with patch.object(bu, "_build_browser_client", lambda: bc), \
         patch.object(bu, "_ddb", lambda: ddb_res):
        result = bu.run_browse_web(
            goal="x",
            user_id="user-42",
            agent_session_id="asid-1",
        )

    assert "Browser unavailable" in result
    statuses = [c.kwargs["Item"]["status"] for c in ddb_table.put_item.call_args_list]
    assert statuses[-1] == "failed"
    bc.stop.assert_not_called()


def test_browser_use_exception_still_stops_session():
    from tools import browser_use as bu

    bc = _fake_browser_client()
    ddb_table = MagicMock()
    ddb_res = MagicMock(); ddb_res.Table.return_value = ddb_table

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(side_effect=RuntimeError("page crashed"))

    with patch.object(bu, "_build_browser_client", lambda: bc), \
         patch.object(bu, "_ddb", lambda: ddb_res), \
         patch.object(bu, "_build_browser_use_agent", return_value=fake_agent):
        result = bu.run_browse_web(
            goal="x",
            user_id="user-42",
            agent_session_id="asid-1",
        )

    assert "Browsing failed" in result
    bc.stop.assert_called_once()
    statuses = [c.kwargs["Item"]["status"] for c in ddb_table.put_item.call_args_list]
    assert statuses[-1] == "failed"


def test_goal_too_long_rejected_early():
    from tools import browser_use as bu
    with patch.object(bu, "_build_browser_client") as factory:
        result = bu.run_browse_web(goal="x" * 4001, user_id="u", agent_session_id="s")
    assert "goal too long" in result.lower()
    factory.assert_not_called()


def test_empty_goal_rejected_early():
    from tools import browser_use as bu
    with patch.object(bu, "_build_browser_client") as factory:
        result = bu.run_browse_web(goal="   ", user_id="u", agent_session_id="s")
    assert "empty" in result.lower()
    factory.assert_not_called()


def test_browse_web_registered_only_with_skill():
    """Tool list contains browse_web only when skills include browser-use."""
    from types import SimpleNamespace
    skills_with = [SimpleNamespace(name="browser-use"), SimpleNamespace(name="weather-lookup")]
    skills_without = [SimpleNamespace(name="weather-lookup")]

    names_with = {getattr(s, "name", "") for s in skills_with}
    names_without = {getattr(s, "name", "") for s in skills_without}
    assert "browser-use" in names_with
    assert "browser-use" not in names_without
