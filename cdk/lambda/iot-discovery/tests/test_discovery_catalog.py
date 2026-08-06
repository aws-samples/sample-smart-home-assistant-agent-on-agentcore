"""Tests for iot-discovery's catalog-backed device list.

The contract that matters most here is the one the voice agent depends on:
agent/voice_session.py's turn_on_all_devices reads `deviceType` and `powerOn`
off each entry and replays them. Dropping either field breaks all-devices voice
control with no error — the loop just finds nothing to do.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mod(lambda_module):
    return lambda_module("iot_discovery_index")


def _ctx():
    ctx = MagicMock()
    ctx.client_context = None
    return ctx


def test_requires_caller_identity(mod):
    out = mod.handler({}, _ctx())
    assert "error" in out


def test_returns_the_catalog(mod):
    out = mod.handler({"user_id": "sub-1"}, _ctx())
    assert out["userId"] == "sub-1"
    assert out["count"] == len(out["devices"]) > 0


def test_voice_all_devices_contract_is_intact(mod):
    """voice_session.py:350-351 reads exactly these two fields."""
    out = mod.handler({"user_id": "u"}, _ctx())
    controllable = [d for d in out["devices"] if not d["readOnly"]]
    assert controllable
    for d in controllable:
        assert d.get("deviceType"), f"{d['deviceId']} lost deviceType"
        assert d.get("powerOn"), f"{d['deviceId']} lost powerOn"
        assert "action" in d["powerOn"]


def test_capabilities_are_exposed_with_ranges(mod):
    """Without ranges the agent guesses parameters and gets rejected."""
    out = mod.handler({"user_id": "u"}, _ctx())
    fan = next(d for d in out["devices"] if d["deviceId"] == "living-fan-1")
    assert fan["capabilities"]["speed"]["max"] == 8
    assert fan["capabilities"]["mode"]["values"]


def test_rooms_are_resolvable_for_disambiguation(mod):
    out = mod.handler({"user_id": "u"}, _ctx())
    assert out["rooms"], "the agent needs room names to resolve 'the living room light'"
    lights = [d for d in out["devices"] if "light" in d["deviceType"]]
    assert len({d["room"] for d in lights}) > 1


def test_sensor_is_flagged_read_only(mod):
    out = mod.handler({"user_id": "u"}, _ctx())
    sensor = next(d for d in out["devices"] if d["deviceId"] == "living-sensor-1")
    assert sensor["readOnly"] is True
    assert "powerOn" not in sensor, "a sensor must not advertise a power action"


def test_every_device_carries_an_id(mod):
    out = mod.handler({"user_id": "u"}, _ctx())
    for d in out["devices"]:
        assert d.get("deviceId"), "deviceId is the agent's handle and the topic segment"
