"""Tests for the dashboard's time ranges and the span-history horizon.

Two things are locked down here, both of them measured against the live account
before being designed (see the spec dated 2026-08-11 §2.2-2.4):

  - The window is queried as ONE Logs Insights call, whatever its length. A 90d
    query measured 3.2s against a 22s budget, so there is nothing to win by
    chunking — and chunking loses data. A chunk lying entirely outside a group's
    retention is a hard 400, and dropping `aws/spans` from the older chunks to
    avoid that silently loses days held only there. The count assertion below
    exists so that "optimisation" cannot be reintroduced without a red test.

  - The span-history note is derived from the DATA, never hardcoded and never
    from log-group metadata. The first attempt used the oldest group's
    creationTime and got 2026-04-12: true (the group existed) but useless, since
    it held no smarthome spans until July — and it suppressed the note in exactly
    the case the note exists for, because any old group makes the horizon look
    older than any window. A literal date would be wrong within a day, in the
    direction that invents history.
"""
import importlib
import os
from datetime import datetime, timedelta, timezone

import pytest

TEXT = ("arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/"
        "smarthome_smarthome-ee97ToCthI")
TEXT_GROUP = "/aws/bedrock-agentcore/runtimes/smarthome_smarthome-ee97ToCthI-DEFAULT"
LEGACY = "aws/spans"


def _load():
    os.environ["AGENT_RUNTIME_ARN"] = TEXT
    os.environ["VOICE_AGENT_RUNTIME_ARN"] = ""
    os.environ["DASHBOARD_EXTRA_RUNTIME_ARNS"] = ""
    import dashboard
    return importlib.reload(dashboard)


class _CountingLogs:
    """Records every StartQuery, and completes each one immediately."""

    def __init__(self, groups):
        self.groups = {g["logGroupName"]: g for g in groups}
        self.starts = []

    # -- describe -----------------------------------------------------------
    def describe_log_groups(self, logGroupNamePrefix, limit=50):
        return {"logGroups": [g for n, g in sorted(self.groups.items())
                              if n.startswith(logGroupNamePrefix)]}

    # -- query --------------------------------------------------------------
    def start_query(self, logGroupNames, startTime, endTime, queryString):
        self.starts.append({"groups": list(logGroupNames),
                            "startTime": startTime, "endTime": endTime})
        return {"queryId": f"q{len(self.starts)}"}

    def get_query_results(self, queryId):
        return {"status": "Complete", "results": [], "statistics": {}}

    def stop_query(self, queryId):
        return {}


def _group(name, created, retention=None):
    g = {"logGroupName": name, "creationTime": int(created.timestamp() * 1000)}
    if retention:
        g["retentionInDays"] = retention
    return g


@pytest.fixture
def patched(monkeypatch):
    def _install(mod, groups):
        fake = _CountingLogs(groups)
        monkeypatch.setattr(mod, "_client", lambda svc: fake)
        return fake
    return _install


# ---------------------------------------------------------------------------
# Ranges
# ---------------------------------------------------------------------------

def test_ranges_include_sixty_and_ninety_days():
    mod = _load()
    assert mod.RANGES == {"24h": 1, "7d": 7, "30d": 30, "60d": 60, "90d": 90}


def test_every_range_maps_to_a_positive_day_count():
    mod = _load()
    for name, days in mod.RANGES.items():
        assert isinstance(days, int) and days > 0, (name, days)


def test_unknown_range_is_rejected():
    mod = _load()
    resp = mod.get_dashboard({"queryStringParameters": {"range": "1y"}})
    assert resp["statusCode"] == 400


@pytest.mark.parametrize("rng", ["24h", "7d", "30d", "60d", "90d"])
def test_each_range_is_accepted_by_the_handler(rng, monkeypatch):
    """A range the handler rejects would 400 the whole page, not degrade."""
    mod = _load()
    monkeypatch.setattr(mod, "_cache_get", lambda key: None)
    monkeypatch.setattr(mod, "_cache_put", lambda key, payload: None)
    monkeypatch.setattr(mod, "_fetch_health", lambda days: {"ok": True})
    monkeypatch.setattr(mod, "_fetch_evaluations", lambda days: {})
    monkeypatch.setattr(mod, "_fetch_ab_comparison", lambda days: {})
    monkeypatch.setattr(mod, "_fetch_satisfaction", lambda days: {"available": False})
    monkeypatch.setattr(mod, "_fetch_release", lambda: {})
    resp = mod.get_dashboard({"queryStringParameters": {"range": rng}})
    assert resp["statusCode"] == 200


# ---------------------------------------------------------------------------
# One query per window, regardless of length — see the module docstring
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("days", [1, 7, 30, 60, 90])
def test_one_start_query_per_call_regardless_of_window(days, patched):
    """Locks out chunking. Measured: 0.6s faster, two silent-data-loss modes."""
    mod = _load()
    fake = patched(mod, [_group(TEXT_GROUP, datetime(2026, 4, 12, tzinfo=timezone.utc))])
    mod._run_logs_insights("fields @timestamp", days)
    assert len(fake.starts) == 1, (
        f"{days}d issued {len(fake.starts)} StartQuery calls. The window must be "
        "queried whole: a chunk entirely outside a group's retention is a 400, "
        "and excluding aws/spans from old chunks silently drops real days."
    )


def test_the_window_is_requested_whole():
    """startTime must span the full range, not a slice of it."""
    mod = _load()
    fake = _CountingLogs([_group(TEXT_GROUP, datetime(2026, 4, 12, tzinfo=timezone.utc))])
    mod._client = lambda svc: fake
    mod._run_logs_insights("fields @timestamp", 90)
    start = fake.starts[0]
    assert start["endTime"] - start["startTime"] == 90 * 86400


def test_legacy_group_is_still_queried_alongside_the_runtime_groups(patched):
    """Dropping it would erase pre-cutover history from a trend page."""
    mod = _load()
    fake = patched(mod, [
        _group(TEXT_GROUP, datetime(2026, 4, 12, tzinfo=timezone.utc)),
        _group(LEGACY, datetime(2026, 2, 5, tzinfo=timezone.utc), retention=30),
    ])
    mod._run_logs_insights("fields @timestamp", 90)
    assert LEGACY in fake.starts[0]["groups"]


# ---------------------------------------------------------------------------
# dataFrom — derived from returned data, never hardcoded
# ---------------------------------------------------------------------------

def _trend(*days_ago):
    now = datetime.now(timezone.utc)
    return [{"day": (now - timedelta(days=d)).strftime("%Y-%m-%d")} for d in days_ago]


def test_note_is_reported_when_data_starts_inside_the_window():
    """The case the note exists for: 90 days asked, 25 days of data."""
    mod = _load()
    out = mod._spans_data_from(_trend(25, 24, 23), 90)
    expected = (datetime.now(timezone.utc) - timedelta(days=25)).strftime("%Y-%m-%d")
    assert out and out.startswith(expected)


def test_no_note_when_data_reaches_the_start_of_the_window():
    """Nothing to caveat, so the UI stays quiet."""
    mod = _load()
    assert mod._spans_data_from(_trend(7, 6, 5), 7) is None


def test_no_note_without_any_data():
    """An empty result is already reported via `available`."""
    mod = _load()
    assert mod._spans_data_from([], 90) is None


def test_the_horizon_tracks_the_data_not_the_log_group_age():
    """Regression on the first implementation.

    It read the oldest log group's creationTime, which on this account is
    2026-04-12 — technically queryable, but the group held no smarthome spans
    until July. That answer suppressed the note in exactly the case it exists
    for, because any old group makes the horizon look older than any window.
    """
    mod = _load()
    out = mod._spans_data_from(_trend(30), 90)
    assert out and not out.startswith("2026-04-12")
    assert out.startswith(
        (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"))


def test_the_horizon_is_never_a_hardcoded_cutover_date():
    """Guards against writing 2026-08-05 (the span-group cutover) into the code."""
    mod = _load()
    out = mod._spans_data_from(_trend(40), 90)
    assert "2026-08-05" not in out


def test_a_malformed_day_is_reported_as_unknown_not_guessed():
    mod = _load()
    assert mod._spans_data_from([{"day": "not-a-date"}], 90) is None
    assert mod._spans_data_from([{}], 90) is None
