"""Tests for iot-query — the read half of the device link.

The behaviour worth pinning down is what happens when there is NO data. A
simulator that was never opened, or whose rows aged out, must read as "not
reported" rather than as a plausible default: the agent would otherwise tell the
user their light is off when it has no idea.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mod(lambda_module):
    with patch("boto3.resource") as res:
        res.return_value = MagicMock()
        module = lambda_module("iot_query_index")
    return module


def _ctx(tool=""):
    ctx = MagicMock()
    ctx.client_context = MagicMock()
    ctx.client_context.custom = {"bedrockAgentCoreToolName": f"Target___{tool}"} if tool else {}
    return ctx


def _stub_tables(mod, state_item=None, history_items=None):
    """Point the module at fake tables. get_item/query are the only calls."""
    state = MagicMock()
    state.get_item.return_value = {"Item": state_item} if state_item else {}
    history = MagicMock()
    history.query.return_value = {"Items": history_items or []}

    def table(name):
        return history if "history" in name else state

    mod._table = table
    return state, history


# ---------------------------------------------------------------------------
# Current state
# ---------------------------------------------------------------------------

def test_missing_row_reports_not_reported_rather_than_a_default(mod):
    _stub_tables(mod, state_item=None)
    out = mod.handler({"user_id": "u", "device_id": "bedroom-light-1"}, _ctx())
    entry = out["devices"][0]
    assert entry["state"] is None, "must not invent a state the device never reported"
    assert entry["online"] is False
    assert "reason" in entry, "the agent needs to be able to explain the gap"


def test_reported_state_is_returned(mod):
    _stub_tables(mod, state_item={
        "state": {"power": True, "brightness": Decimal("80")},
        "online": True,
        "reportedAt": "2026-08-06T00:00:00Z",
    })
    out = mod.handler({"user_id": "u", "device_id": "bedroom-light-1"}, _ctx())
    entry = out["devices"][0]
    assert entry["state"]["power"] is True
    assert entry["state"]["brightness"] == 80, "Decimal must be JSON-safe"
    assert isinstance(entry["state"]["brightness"], int)
    assert entry["online"] is True


def test_state_is_scoped_to_the_caller(mod):
    state, _ = _stub_tables(mod, state_item={"state": {}, "online": True})
    mod.handler({"user_id": "sub-abc", "device_id": "bedroom-light-1"}, _ctx())
    key = state.get_item.call_args.kwargs["Key"]
    assert key == {"userId": "sub-abc", "deviceId": "bedroom-light-1"}


def test_no_device_argument_returns_the_whole_fleet(mod):
    _stub_tables(mod, state_item={"state": {"power": False}, "online": True})
    out = mod.handler({"user_id": "u"}, _ctx())
    assert out["count"] > 1


def test_unknown_device_is_an_error(mod):
    _stub_tables(mod)
    out = mod.handler({"user_id": "u", "device_id": "nope-1"}, _ctx())
    assert "error" in out


def test_missing_identity_is_refused(mod):
    _stub_tables(mod)
    out = mod.handler({"device_id": "bedroom-light-1"}, _ctx())
    assert "error" in out


# ---------------------------------------------------------------------------
# Sensor history
# ---------------------------------------------------------------------------

def _points(metric, values, start_ts=1_000_000):
    return [
        {"ts": Decimal(start_ts + i * 300), "metric": metric, "value": Decimal(str(v))}
        for i, v in enumerate(values)
    ]


def test_history_queries_one_partition_per_metric(mod):
    """Metrics share a timestamp, so each one is its own partition."""
    _, history = _stub_tables(mod, history_items=_points("temperature", [21.0, 22.0]))
    mod.handler({"user_id": "u", "device_id": "living-sensor-1", "hours": 24}, _ctx("query_sensor_history"))
    keys = [c.kwargs["KeyConditionExpression"] for c in history.query.call_args_list]
    assert len(keys) == 4, "one query per readonly metric on the sensor"


def test_history_returns_summary_and_series(mod):
    _stub_tables(mod, history_items=_points("temperature", [20.0, 22.0, 24.0]))
    out = mod.handler(
        {"user_id": "u", "device_id": "living-sensor-1", "metric": "temperature", "hours": 24},
        _ctx("query_sensor_history"),
    )
    s = out["summary"]["temperature"]
    assert (s["min"], s["max"], s["latest"]) == (20.0, 24.0, 24.0)
    assert s["average"] == 22.0
    assert out["series"]["temperature"][0]["ts"] < out["series"]["temperature"][-1]["ts"], \
        "series must be oldest-first for plotting"
    assert out["units"]["temperature"] == "C"


def test_history_with_no_rows_says_so(mod):
    _stub_tables(mod, history_items=[])
    out = mod.handler(
        {"user_id": "u", "device_id": "living-sensor-1", "hours": 24},
        _ctx("query_sensor_history"),
    )
    assert out["series"] == {}
    assert "reason" in out


def test_unknown_metric_is_rejected(mod):
    _stub_tables(mod)
    out = mod.handler(
        {"user_id": "u", "device_id": "living-sensor-1", "metric": "loudness"},
        _ctx("query_sensor_history"),
    )
    assert "error" in out and "loudness" in out["error"]


def test_history_on_a_non_sensor_is_rejected(mod):
    _stub_tables(mod)
    out = mod.handler(
        {"user_id": "u", "device_id": "living-fan-1", "device_type": "fan", "hours": 24},
        _ctx("query_sensor_history"),
    )
    assert "error" in out


def test_hours_is_clamped(mod):
    _, history = _stub_tables(mod, history_items=_points("temperature", [21.0]))
    for requested, expected in ((0, 1), (99999, 168), ("bad", 24)):
        history.query.reset_mock()
        out = mod.handler(
            {"user_id": "u", "device_id": "living-sensor-1", "metric": "temperature",
             "hours": requested},
            _ctx("query_sensor_history"),
        )
        assert out["hours"] == expected, f"hours={requested!r} should clamp to {expected}"


def test_history_query_failure_does_not_crash(mod):
    _, history = _stub_tables(mod)
    history.query.side_effect = RuntimeError("throttled")
    out = mod.handler(
        {"user_id": "u", "device_id": "living-sensor-1", "hours": 24},
        _ctx("query_sensor_history"),
    )
    # Individual metric failures are skipped, leaving an empty-but-valid answer.
    assert "series" in out or "error" in out
