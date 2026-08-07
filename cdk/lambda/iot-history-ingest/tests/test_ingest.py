"""Tests for the sensor-history fan-out Lambda.

The case that drove this file: a sensor samples all four of its metrics at the
same instant, so keying rows on (device, ts) made them overwrite each other and
left one metric per timestamp. Including the metric in the partition key is what
fixes it, and test_metrics_at_the_same_timestamp_do_not_collide is the guard.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mod(lambda_module):
    with patch("boto3.resource") as res:
        res.return_value = MagicMock()
        module = lambda_module("iot_history_ingest_index")
    return module


def _stub_table(mod):
    """Capture what batch_writer would have written."""
    written = []
    batch = MagicMock()
    batch.put_item.side_effect = lambda Item: written.append(Item)
    ctx_mgr = MagicMock()
    ctx_mgr.__enter__ = MagicMock(return_value=batch)
    ctx_mgr.__exit__ = MagicMock(return_value=False)
    table = MagicMock()
    table.batch_writer.return_value = ctx_mgr
    mod._get_table = lambda: table
    return written, table


def test_metrics_at_the_same_timestamp_do_not_collide(mod):
    """The bug this keying fixes: four metrics, one instant, four rows."""
    written, _ = _stub_table(mod)
    out = mod.handler({
        "topic": "smarthome/sub-1/living-sensor-1/history",
        "points": [
            {"ts": 1000, "metric": "temperature", "value": 21.5},
            {"ts": 1000, "metric": "humidity", "value": 47},
            {"ts": 1000, "metric": "pm25", "value": 12},
            {"ts": 1000, "metric": "co2", "value": 600},
        ],
    }, MagicMock())
    assert out["written"] == 4
    keys = {(r["metricKey"], r["ts"]) for r in written}
    assert len(keys) == 4, f"rows collided on the primary key: {keys}"


def test_partition_key_is_derived_from_the_topic_not_the_body(mod):
    """A client controls the payload but not the topic it was authorised on."""
    written, _ = _stub_table(mod)
    mod.handler({
        "topic": "smarthome/real-user/living-sensor-1/history",
        # A hostile body claiming to be someone else.
        "deviceId": "other-device", "userId": "victim",
        "points": [{"ts": 1, "metric": "temperature", "value": 20}],
    }, MagicMock())
    assert written[0]["metricKey"] == "real-user#living-sensor-1#temperature"


def test_unroutable_topic_writes_nothing(mod):
    written, _ = _stub_table(mod)
    for topic in (None, "", "nonsense", "other/prefix/x/history"):
        out = mod.handler({"topic": topic, "points": [{"ts": 1, "metric": "t", "value": 1}]}, MagicMock())
        assert out["written"] == 0
    assert written == []


def test_values_are_stored_as_decimal(mod):
    """DynamoDB has no float type; a raw float would raise on write."""
    written, _ = _stub_table(mod)
    mod.handler({
        "topic": "smarthome/u/living-sensor-1/history",
        "points": [{"ts": 1, "metric": "temperature", "value": 21.456}],
    }, MagicMock())
    assert isinstance(written[0]["value"], Decimal)
    assert str(written[0]["value"]) == "21.46"


def test_ttl_is_set_on_every_row(mod):
    written, _ = _stub_table(mod)
    mod.handler({
        "topic": "smarthome/u/living-sensor-1/history",
        "points": [{"ts": 1, "metric": "temperature", "value": 20}],
    }, MagicMock())
    assert written[0]["ttl"] > 0


def test_malformed_points_are_skipped_not_fatal(mod):
    written, _ = _stub_table(mod)
    out = mod.handler({
        "topic": "smarthome/u/living-sensor-1/history",
        "points": [
            {"ts": 1, "metric": "temperature", "value": 20},   # good
            "not-a-dict",                                       # skipped
            {"ts": 2, "value": 20},                             # no metric
            {"ts": 3, "metric": "temperature"},                 # no value
            {"ts": "x", "metric": "temperature", "value": 20},  # bad ts
            {"ts": 4, "metric": "temperature", "value": "hot"}, # bad value
        ],
    }, MagicMock())
    assert out["written"] == 1
    assert len(written) == 1


def test_point_count_is_capped(mod):
    written, _ = _stub_table(mod)
    many = [{"ts": i, "metric": "temperature", "value": 20} for i in range(mod.MAX_POINTS + 500)]
    out = mod.handler({"topic": "smarthome/u/living-sensor-1/history", "points": many}, MagicMock())
    assert out["written"] == mod.MAX_POINTS


def test_non_list_points_is_an_error(mod):
    _stub_table(mod)
    out = mod.handler({"topic": "smarthome/u/living-sensor-1/history", "points": "nope"}, MagicMock())
    assert out["written"] == 0 and "error" in out


def test_empty_points_is_a_no_op(mod):
    written, table = _stub_table(mod)
    out = mod.handler({"topic": "smarthome/u/living-sensor-1/history", "points": []}, MagicMock())
    assert out["written"] == 0
    assert not table.batch_writer.called, "no write should be attempted"


def test_write_failure_reports_partial_progress(mod):
    _, table = _stub_table(mod)
    table.batch_writer.side_effect = RuntimeError("capacity exceeded")
    out = mod.handler({
        "topic": "smarthome/u/living-sensor-1/history",
        "points": [{"ts": 1, "metric": "temperature", "value": 20}],
    }, MagicMock())
    assert "error" in out and out["written"] == 0


def test_seeding_a_full_day_of_four_metrics_writes_every_row(mod):
    """288 five-minute samples x 4 metrics — the first-login backfill."""
    written, _ = _stub_table(mod)
    points = []
    for i in range(288):
        ts = 1_700_000_000 + i * 300
        for metric, value in (("temperature", 21), ("humidity", 45), ("pm25", 10), ("co2", 550)):
            points.append({"ts": ts, "metric": metric, "value": value})
    out = mod.handler({"topic": "smarthome/u/living-sensor-1/history", "points": points}, MagicMock())
    assert out["written"] == len(points) == 1152
    assert len({(r["metricKey"], r["ts"]) for r in written}) == 1152
