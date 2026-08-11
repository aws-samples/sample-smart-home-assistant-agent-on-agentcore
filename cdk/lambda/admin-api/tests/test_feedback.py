"""Tests for end-user feedback: submitting a vote and aggregating it.

The satisfaction card on the Overview page was hardcoded mock data until now,
because the only feedback path was the `user-feedback` skill writing JSON files
into a runtime's workspace — readable one at a time through Remote Shell, never
aggregatable into a number.

The two assertions that matter most:

  - The vote route sits BEFORE the admin gate. Every voter is an ordinary user,
    so behind the gate all of them 403 — and the chatbot's failure mode for that
    is a button that appears to do nothing, which reads as a frontend bug.

  - An empty table reports `available: False`, never a zero CSAT. "Nobody has
    voted" and "everybody is unhappy" render identically on a gauge and mean
    opposite things. Inventing the pessimistic reading of missing data is the
    same class of bug as inventing the optimistic one.
"""
import importlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

# The project resolves a caller to email -> cognito:username -> sub, everywhere:
# the runtime, the chatbot's polling, and `_caller_id`. Feedback rows follow the
# same rule so a self-access check agrees with how the row was written.
USER_SUB = "u@example.com"
OTHER_SUB = "someone-else@example.com"


def _load_index():
    import index
    return importlib.reload(index)


def _load_dashboard():
    os.environ["AGENT_RUNTIME_ARN"] = ""
    import dashboard
    return importlib.reload(dashboard)


def _event(body, sub=USER_SUB, groups="", action="feedback"):
    claims = {"sub": "11112222-3333-4444-5555-666677778888", "email": sub}
    if groups:
        claims["cognito:groups"] = groups
    return {
        "resource": "/sessions",
        "httpMethod": "POST",
        "queryStringParameters": {"action": action} if action else {},
        "body": json.dumps(body),
        "requestContext": {"authorizer": {"claims": claims}},
    }


class _FakeTable:
    def __init__(self, items=None):
        self.items = list(items or [])
        self.puts = []

    def put_item(self, Item):
        self.puts.append(Item)
        self.items = [i for i in self.items
                      if not (i.get("userId") == Item.get("userId")
                              and i.get("feedbackKey") == Item.get("feedbackKey"))]
        self.items.append(Item)
        return {}

    def scan(self, **kwargs):
        return {"Items": list(self.items)}


@pytest.fixture
def idx(monkeypatch):
    mod = _load_index()
    table = _FakeTable()
    monkeypatch.setattr(mod, "_feedback_table", lambda: table)
    return mod, table


# ---------------------------------------------------------------------------
# The gate — the single most important test in this file
# ---------------------------------------------------------------------------

def test_a_non_admin_user_can_submit_feedback(idx):
    """Behind check_admin this returns 403 and the UI shows a dead button."""
    mod, table = idx
    resp = mod._dispatch(_event({"userId": USER_SUB, "vote": "up", "turnId": "t1"}), None)
    assert resp["statusCode"] == 200, resp
    assert table.puts and table.puts[0]["vote"] == "up"


def test_feedback_route_is_reached_without_admin_group(idx):
    """Asserts ordering explicitly: no cognito:groups claim at all."""
    mod, _ = idx
    ev = _event({"userId": USER_SUB, "vote": "down", "turnId": "t1"})
    ev["requestContext"]["authorizer"]["claims"].pop("cognito:groups", None)
    assert mod._dispatch(ev, None)["statusCode"] == 200


def test_a_user_cannot_vote_as_someone_else(idx):
    """Self-or-admin: otherwise one account could skew another's numbers."""
    mod, _ = idx
    resp = mod._dispatch(_event({"userId": OTHER_SUB, "vote": "up", "turnId": "t1"}), None)
    assert resp["statusCode"] == 403


def test_an_admin_may_submit_on_behalf_of_a_user(idx):
    mod, _ = idx
    resp = mod._dispatch(
        _event({"userId": OTHER_SUB, "vote": "up", "turnId": "t1"}, groups="admin"), None)
    assert resp["statusCode"] == 200


def test_unknown_sessions_action_is_rejected(idx):
    mod, _ = idx
    resp = mod._dispatch(_event({}, action="bogus"), None)
    assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vote", ["", "yes", "UP", "1", None])
def test_vote_must_be_up_or_down(idx, vote):
    mod, table = idx
    resp = mod._dispatch(_event({"userId": USER_SUB, "vote": vote, "turnId": "t1"}), None)
    assert resp["statusCode"] == 400
    assert not table.puts


def test_turn_id_is_required(idx):
    """Without it a re-vote cannot overwrite, so one user moves the mean twice."""
    mod, table = idx
    resp = mod._dispatch(_event({"userId": USER_SUB, "vote": "up"}), None)
    assert resp["statusCode"] == 400
    assert not table.puts


def test_user_id_is_required(idx):
    mod, _ = idx
    resp = mod._dispatch(_event({"vote": "up", "turnId": "t1"}), None)
    assert resp["statusCode"] == 400


def test_reason_is_truncated(idx):
    mod, table = idx
    mod._dispatch(_event({"userId": USER_SUB, "vote": "down", "turnId": "t1",
                          "reason": "x" * 5000}), None)
    assert len(table.puts[0]["reason"]) == mod.MAX_FEEDBACK_REASON_CHARS


def test_turn_prompt_is_truncated(idx):
    mod, table = idx
    mod._dispatch(_event({"userId": USER_SUB, "vote": "up", "turnId": "t1",
                          "turnPrompt": "y" * 900}), None)
    assert len(table.puts[0]["turnPrompt"]) == mod.MAX_FEEDBACK_PROMPT_CHARS


def test_empty_reason_is_omitted_not_stored_blank(idx):
    """A blank string would count as "gave a reason" in the reasons list."""
    mod, table = idx
    mod._dispatch(_event({"userId": USER_SUB, "vote": "down", "turnId": "t1",
                          "reason": "   "}), None)
    assert "reason" not in table.puts[0]


# ---------------------------------------------------------------------------
# source=sim — the honesty of the "N% simulated" note depends on this
# ---------------------------------------------------------------------------

def test_source_defaults_to_user(idx):
    mod, table = idx
    mod._dispatch(_event({"userId": USER_SUB, "vote": "up", "turnId": "t1"}), None)
    assert table.puts[0]["source"] == "user"


def test_a_non_admin_cannot_file_a_vote_as_simulated(idx):
    """Otherwise any client could hide its votes behind the sim label."""
    mod, table = idx
    resp = mod._dispatch(_event({"userId": USER_SUB, "vote": "up", "turnId": "t1",
                                 "source": "sim"}), None)
    assert resp["statusCode"] == 403
    assert not table.puts


def test_an_admin_may_file_simulated_votes(idx):
    mod, table = idx
    resp = mod._dispatch(_event({"userId": USER_SUB, "vote": "up", "turnId": "t1",
                                 "source": "sim"}, groups="admin"), None)
    assert resp["statusCode"] == 200
    assert table.puts[0]["source"] == "sim"


def test_unknown_source_is_rejected(idx):
    mod, _ = idx
    resp = mod._dispatch(_event({"userId": USER_SUB, "vote": "up", "turnId": "t1",
                                 "source": "marketing"}, groups="admin"), None)
    assert resp["statusCode"] == 400


def test_only_an_admin_may_backdate_a_vote(idx):
    """The simulator lays votes across past days; a user's vote is stamped now."""
    mod, table = idx
    past = "2026-06-01T00:00:00+00:00"
    mod._dispatch(_event({"userId": USER_SUB, "vote": "up", "turnId": "t1",
                          "ts": past}), None)
    assert table.puts[0]["ts"] != past

    mod._dispatch(_event({"userId": USER_SUB, "vote": "up", "turnId": "t2",
                          "ts": past}, groups="admin"), None)
    assert table.puts[1]["ts"] == past


# ---------------------------------------------------------------------------
# Re-voting
# ---------------------------------------------------------------------------

def test_agent_dim_accepts_a_string_or_a_list(idx):
    mod, table = idx
    mod._dispatch(_event({"userId": USER_SUB, "vote": "up", "turnId": "t1",
                          "agentDim": "light-effect"}), None)
    assert table.puts[0]["agentDim"] == ["light-effect"]
    mod._dispatch(_event({"userId": USER_SUB, "vote": "up", "turnId": "t2",
                          "agentDim": ["a", "b"]}), None)
    assert table.puts[1]["agentDim"] == ["a", "b"]


def test_a_revote_overwrites_even_though_the_server_restamps_ts(idx):
    """The real client path, which an earlier version of this test missed.

    The chatbot sends no `ts`, so the server stamps one per request. With `ts`
    first in the sort key, a 👎 followed by its reason wrote TWO rows and counted
    as two negatives — verified live before the key was changed. Pinning `ts` in
    the test hid that, because both writes then shared a timestamp.
    """
    mod, table = idx
    mod._dispatch(_event({"userId": USER_SUB, "vote": "down", "turnId": "t1",
                          "sessionId": "s1"}), None)
    mod._dispatch(_event({"userId": USER_SUB, "vote": "down", "turnId": "t1",
                          "sessionId": "s1", "reason": "too slow"}), None)
    assert table.puts[0]["ts"] != table.puts[1]["ts"], "stamps should differ"
    assert table.puts[0]["feedbackKey"] == table.puts[1]["feedbackKey"]
    assert len(table.items) == 1, table.items
    assert table.items[0]["reason"] == "too slow"


def test_changing_a_vote_replaces_it_rather_than_adding(idx):
    mod, table = idx
    mod._dispatch(_event({"userId": USER_SUB, "vote": "up", "turnId": "t1",
                          "sessionId": "s1"}), None)
    mod._dispatch(_event({"userId": USER_SUB, "vote": "down", "turnId": "t1",
                          "sessionId": "s1"}), None)
    assert len(table.items) == 1 and table.items[0]["vote"] == "down"


def test_different_turns_in_one_session_are_separate_rows(idx):
    """The other half: overwriting must not collapse distinct turns."""
    mod, table = idx
    mod._dispatch(_event({"userId": USER_SUB, "vote": "up", "turnId": "t1",
                          "sessionId": "s1"}), None)
    mod._dispatch(_event({"userId": USER_SUB, "vote": "down", "turnId": "t2",
                          "sessionId": "s1"}), None)
    assert len(table.items) == 2


def test_the_key_does_not_start_with_the_timestamp(idx):
    """Guards the regression directly: a ts-first key cannot dedupe a re-vote."""
    mod, table = idx
    mod._dispatch(_event({"userId": USER_SUB, "vote": "up", "turnId": "t1",
                          "sessionId": "s1"}), None)
    key = table.puts[0]["feedbackKey"]
    assert not key.startswith(table.puts[0]["ts"]), key
    assert key.endswith("t1"), key


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _row(vote, days_ago=0, source="user", reason=None, agents=None):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    row = {"userId": USER_SUB, "feedbackKey": f"{ts}#s#t", "ts": ts,
           "vote": vote, "source": source}
    if reason:
        row["reason"] = reason
    if agents:
        row["agentDim"] = agents
    return row


def _agg(monkeypatch, rows, days=7):
    mod = _load_dashboard()
    monkeypatch.setattr(mod, "_table", lambda name=None: _FakeTable(rows))
    return mod._fetch_satisfaction(days)


def test_empty_table_is_unavailable_not_a_zero_score(monkeypatch):
    """The whole point: absence of data must not render as a bad score."""
    out = _agg(monkeypatch, [])
    assert out["available"] is False
    assert "csat" not in out


def test_votes_outside_the_window_are_excluded(monkeypatch):
    out = _agg(monkeypatch, [_row("up", days_ago=40)], days=7)
    assert out["available"] is False


def test_csat_maps_a_two_way_vote_onto_the_five_point_scale(monkeypatch):
    out = _agg(monkeypatch, [_row("up")] * 3 + [_row("down")])
    assert out["thumbsUp"] == 3 and out["thumbsDown"] == 1
    assert out["csat"] == pytest.approx(1 + 4 * 0.75)
    assert out["csatScale"] == 5


def test_all_negative_votes_score_the_scale_minimum_not_zero(monkeypatch):
    """A real unanimous 👎 is 1.0 on a 1-5 scale; 0 is off-scale."""
    out = _agg(monkeypatch, [_row("down")] * 4)
    assert out["csat"] == 1.0


def test_simulated_share_is_reported(monkeypatch):
    out = _agg(monkeypatch, [_row("up", source="sim")] * 3 + [_row("up")])
    assert out["simulatedShare"] == pytest.approx(0.75)


def test_trend_buckets_by_day_with_a_down_rate(monkeypatch):
    out = _agg(monkeypatch, [_row("up", days_ago=1), _row("down", days_ago=1),
                             _row("up", days_ago=0)])
    by_day = {d["day"]: d for d in out["trend"]}
    assert len(by_day) == 2
    yesterday = out["trend"][0]
    assert yesterday["downRate"] == pytest.approx(0.5)


def test_by_agent_counts_a_multi_agent_turn_under_each(monkeypatch):
    out = _agg(monkeypatch, [_row("down", agents=["light-effect", "scene-sync"])])
    counts = {r["agent"]: r["down"] for r in out["byAgent"]}
    assert counts == {"light-effect": 1, "scene-sync": 1}


def test_turns_with_no_delegation_are_bucketed_explicitly(monkeypatch):
    out = _agg(monkeypatch, [_row("up")])
    assert out["byAgent"][0]["agent"] == "(no delegation)"


def test_recent_reasons_are_newest_first_and_capped(monkeypatch):
    rows = [_row("down", days_ago=i, reason=f"r{i}") for i in range(15)]
    out = _agg(monkeypatch, rows, days=90)
    assert len(out["recentReasons"]) == 10
    assert out["recentReasons"][0]["reason"] == "r0"


def test_votes_without_a_reason_are_left_out_of_the_reason_list(monkeypatch):
    out = _agg(monkeypatch, [_row("up"), _row("down", reason="too slow")])
    assert [r["reason"] for r in out["recentReasons"]] == ["too slow"]
