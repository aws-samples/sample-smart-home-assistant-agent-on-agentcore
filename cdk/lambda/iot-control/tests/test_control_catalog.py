"""Tests for iot-control's catalog-driven publish path.

Covers the three things a wrong change here would break silently:
the topic shape (a wrong segment publishes to a topic nobody subscribes to and
returns success), clamped values reaching the device, and the backward
compatibility that keeps voice all-devices control working.
"""

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mod(lambda_module):
    with patch("boto3.client") as mock_client:
        mock_client.return_value = MagicMock()
        module = lambda_module("iot_control_index")
    module.iot_client = MagicMock()
    return module


def _ctx():
    ctx = MagicMock()
    ctx.client_context = None
    return ctx


def _published(mod):
    assert mod.iot_client.publish.called, "nothing was published"
    kwargs = mod.iot_client.publish.call_args.kwargs
    return kwargs["topic"], json.loads(kwargs["payload"].decode("utf-8"))


# ---------------------------------------------------------------------------
# Topic shape
# ---------------------------------------------------------------------------

def test_topic_is_scoped_by_user_and_device_id(mod):
    out = mod.handler(
        {"user_id": "sub-123", "device_id": "bedroom-light-1",
         "command": {"action": "setPower", "power": True}},
        _ctx(),
    )
    topic, payload = _published(mod)
    assert topic == "smarthome/sub-123/bedroom-light-1/command"
    assert payload == {"action": "setPower", "power": True}
    assert out["deviceId"] == "bedroom-light-1"


def test_two_lights_publish_to_different_topics(mod):
    """The reason deviceId replaced deviceType in the topic."""
    topics = []
    for did in ("bedroom-light-1", "living-strip-1"):
        mod.iot_client.publish.reset_mock()
        mod.handler(
            {"user_id": "u", "device_id": did,
             "command": {"action": "setPower", "power": True}},
            _ctx(),
        )
        topics.append(_published(mod)[0])
    assert topics[0] != topics[1]


def test_missing_identity_is_refused(mod):
    out = mod.handler(
        {"device_id": "bedroom-light-1", "command": {"action": "setPower", "power": True}},
        _ctx(),
    )
    assert "error" in out
    assert not mod.iot_client.publish.called, "must not publish without an owner"


def test_unknown_device_is_refused_without_publishing(mod):
    out = mod.handler(
        {"user_id": "u", "device_id": "nope-1",
         "command": {"action": "setPower", "power": True}},
        _ctx(),
    )
    assert "error" in out and not mod.iot_client.publish.called


# ---------------------------------------------------------------------------
# Clamping — the published payload must carry the adjusted value
# ---------------------------------------------------------------------------

def test_over_range_speed_publishes_the_clamped_value(mod):
    out = mod.handler(
        {"user_id": "u", "device_id": "living-fan-1",
         "command": {"action": "setSpeed", "speed": 20}},
        _ctx(),
    )
    _, payload = _published(mod)
    assert payload["speed"] == 8, "the device must receive the clamped value, not 20"
    assert out["warnings"], "the agent needs the warning to explain the adjustment"
    assert "8" in out["warnings"][0]


def test_in_range_value_reports_no_warning(mod):
    out = mod.handler(
        {"user_id": "u", "device_id": "living-fan-1",
         "command": {"action": "setSpeed", "speed": 3}},
        _ctx(),
    )
    assert "warnings" not in out
    assert _published(mod)[1]["speed"] == 3


# ---------------------------------------------------------------------------
# Type enforcement — the defect the old table declared but never checked
# ---------------------------------------------------------------------------

def test_string_boolean_is_refused_without_publishing(mod):
    out = mod.handler(
        {"user_id": "u", "device_id": "living-fan-1",
         "command": {"action": "setPower", "power": "yes"}},
        _ctx(),
    )
    assert "error" in out
    assert not mod.iot_client.publish.called


# ---------------------------------------------------------------------------
# Enum values the frontend supported but the old Lambda rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("device_id,mode", [
    ("living-led-1", "solid"),
    ("kitchen-oven-1", "preheat"),
])
def test_previously_rejected_modes_now_publish(mod, device_id, mode):
    mod.handler(
        {"user_id": "u", "device_id": device_id,
         "command": {"action": "setMode", "mode": mode}},
        _ctx(),
    )
    assert _published(mod)[1]["mode"] == mode


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------

def test_sensor_control_is_refused_with_a_query_hint(mod):
    out = mod.handler(
        {"user_id": "u", "device_id": "living-sensor-1",
         "command": {"action": "setPower", "power": True}},
        _ctx(),
    )
    assert "error" in out and "temperature" in out["error"]
    assert not mod.iot_client.publish.called


# ---------------------------------------------------------------------------
# Backward compatibility — device_type still resolves
# ---------------------------------------------------------------------------

def test_device_type_still_works_for_existing_skills(mod):
    """Skills and voice_session were written against device_type."""
    out = mod.handler(
        {"user_id": "u", "device_type": "fan",
         "command": {"action": "setPower", "power": True}},
        _ctx(),
    )
    topic, _ = _published(mod)
    assert topic == "smarthome/u/living-fan-1/command"
    assert out["device"] == "fan"


def test_command_accepted_as_a_json_string(mod):
    """Some Gateway paths deliver `command` pre-serialised."""
    mod.handler(
        {"user_id": "u", "device_id": "living-plug-1",
         "command": json.dumps({"action": "setPower", "power": True})},
        _ctx(),
    )
    assert _published(mod)[1]["power"] is True
