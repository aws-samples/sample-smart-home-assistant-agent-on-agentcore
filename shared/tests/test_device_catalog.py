"""Tests for the shared device catalog and its command validation.

The three cases this file exists for are the defects the catalog replaces
(see device_catalog.py's module docstring): clamping instead of rejecting,
actually enforcing declared types, and accepting the enum values the frontend
always supported but the old hardcoded table rejected.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import device_catalog as dc  # noqa: E402


# ---------------------------------------------------------------------------
# Catalog integrity — a malformed catalog breaks validation everywhere
# ---------------------------------------------------------------------------

def test_catalog_loads():
    assert dc.load_catalog()["devices"]


def test_device_ids_unique():
    ids = dc.device_ids()
    assert len(ids) == len(set(ids)), f"duplicate deviceId in catalog: {ids}"


def test_device_ids_are_topic_safe():
    # deviceId becomes an MQTT topic segment; '/' '+' '#' would reshape the topic.
    for did in dc.device_ids():
        assert did, "empty deviceId"
        for bad in ("/", "+", "#", " "):
            assert bad not in did, f"{did!r} contains {bad!r}, unsafe in a topic"


def test_every_action_writes_a_declared_capability():
    for d in dc.devices():
        caps = d.get("capabilities", {})
        for action, spec in d.get("actions", {}).items():
            writes = spec.get("writes")
            assert writes in caps, (
                f"{d['deviceId']}.{action} writes '{writes}' which is not a capability"
            )


def test_every_accepted_parameter_resolves_to_a_capability():
    """Guards the catalog bug this constraint was added for.

    `colors` (on setEffect) and `enabled` (on setOscillation/keepWarm) are named
    differently from the capability they write, so they need an explicit `params`
    mapping. Without it, validation fell back to the action's `writes` target and
    checked a color list against the effect enum.
    """
    for d in dc.devices():
        caps = d.get("capabilities", {})
        for action, spec in d.get("actions", {}).items():
            mapping = spec.get("params", {})
            for param in set(spec.get("required", [])) | set(spec.get("optional", [])):
                cap_name = mapping.get(param, param)
                assert cap_name in caps, (
                    f"{d['deviceId']}.{action} accepts '{param}' -> '{cap_name}', "
                    f"which is not a capability. Add a `params` mapping."
                )


def test_power_payloads_reference_real_actions():
    # voice_session.turn_on_all_devices replays these verbatim.
    for d in dc.devices():
        for key in ("powerOn", "powerOff"):
            payload = d.get(key)
            if payload:
                assert payload["action"] in d["actions"], (
                    f"{d['deviceId']}.{key} uses undeclared action {payload['action']}"
                )


def test_readonly_devices_declare_no_actions():
    sensor = dc.device_by_id("living-sensor-1")
    assert dc.is_readonly(sensor)
    assert not dc.is_readonly(dc.device_by_id("living-fan-1"))


# ---------------------------------------------------------------------------
# Defect 1 — out-of-range values clamp instead of failing
# ---------------------------------------------------------------------------

def test_speed_above_max_is_clamped_with_a_warning():
    fan = dc.device_by_id("living-fan-1")
    ok, cmd, warnings = dc.validate_command(fan, {"action": "setSpeed", "speed": 20})
    assert ok, "over-range should clamp, not reject"
    assert cmd["speed"] == 8
    assert warnings and "8" in warnings[0]


def test_speed_below_min_is_clamped():
    fan = dc.device_by_id("living-fan-1")
    ok, cmd, warnings = dc.validate_command(fan, {"action": "setSpeed", "speed": -5})
    assert ok
    assert cmd["speed"] == 0
    assert warnings


def test_in_range_value_passes_without_warning():
    fan = dc.device_by_id("living-fan-1")
    ok, cmd, warnings = dc.validate_command(fan, {"action": "setSpeed", "speed": 5})
    assert ok and cmd["speed"] == 5 and not warnings


# ---------------------------------------------------------------------------
# Defect 2 — declared types are enforced
# ---------------------------------------------------------------------------

def test_string_for_boolean_is_rejected():
    fan = dc.device_by_id("living-fan-1")
    ok, cmd, warnings = dc.validate_command(fan, {"action": "setPower", "power": "yes"})
    assert not ok, "the old table declared this type but never checked it"
    assert cmd is None


def test_number_for_boolean_is_rejected():
    fan = dc.device_by_id("living-fan-1")
    ok, _, _ = dc.validate_command(fan, {"action": "setPower", "power": 1})
    assert not ok


def test_bool_for_integer_is_rejected():
    # bool is a subclass of int in Python, so this needs an explicit guard.
    fan = dc.device_by_id("living-fan-1")
    ok, _, _ = dc.validate_command(fan, {"action": "setSpeed", "speed": True})
    assert not ok


def test_boolean_accepts_real_booleans():
    fan = dc.device_by_id("living-fan-1")
    for value in (True, False):
        ok, cmd, _ = dc.validate_command(fan, {"action": "setPower", "power": value})
        assert ok and cmd["power"] is value


# ---------------------------------------------------------------------------
# Defect 3 — enum values the frontend supported but the Lambda rejected
# ---------------------------------------------------------------------------

def test_led_solid_mode_is_accepted():
    led = dc.device_by_id("living-led-1")
    ok, _, _ = dc.validate_command(led, {"action": "setMode", "mode": "solid"})
    assert ok, "LedMatrix.tsx has always rendered 'solid'"


def test_oven_preheat_mode_is_accepted():
    oven = dc.device_by_id("kitchen-oven-1")
    ok, _, _ = dc.validate_command(oven, {"action": "setMode", "mode": "preheat"})
    assert ok, "Oven.tsx has always rendered 'preheat'"


def test_unknown_enum_value_is_rejected():
    led = dc.device_by_id("living-led-1")
    ok, _, warnings = dc.validate_command(led, {"action": "setMode", "mode": "disco"})
    assert not ok and "disco" in warnings[0]


# ---------------------------------------------------------------------------
# Sensors reject writes with a query hint
# ---------------------------------------------------------------------------

def test_sensor_rejects_control_with_a_hint():
    sensor = dc.device_by_id("living-sensor-1")
    ok, _, warnings = dc.validate_command(sensor, {"action": "setPower", "power": True})
    assert not ok
    assert "temperature" in warnings[0], "the refusal should point at what it can report"


# ---------------------------------------------------------------------------
# Colors and segments
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["red", "#fff", "ff0000", "#gggggg", 123])
def test_invalid_colors_rejected(bad):
    light = dc.device_by_id("bedroom-light-1")
    ok, _, _ = dc.validate_command(light, {"action": "setColor", "color": bad})
    assert not ok, f"{bad!r} should not validate as a color"


def test_valid_color_is_lowercased():
    light = dc.device_by_id("bedroom-light-1")
    ok, cmd, _ = dc.validate_command(light, {"action": "setColor", "color": "#FF00AA"})
    assert ok and cmd["color"] == "#ff00aa"


def test_segments_longer_than_strip_are_truncated():
    strip = dc.device_by_id("living-strip-1")
    count = strip["capabilities"]["segments"]["count"]
    ok, cmd, warnings = dc.validate_command(
        strip, {"action": "setEffect", "effect": "gradient", "colors": ["#112233"] * (count + 5)}
    )
    assert ok
    assert len(cmd["colors"]) == count
    assert warnings


def test_color_temp_clamps_to_kelvin_range():
    light = dc.device_by_id("bedroom-light-1")
    ok, cmd, warnings = dc.validate_command(
        light, {"action": "setColorTemp", "color_temp": 9000}
    )
    assert ok and cmd["color_temp"] == 6500 and warnings


# ---------------------------------------------------------------------------
# Device resolution
# ---------------------------------------------------------------------------

def test_resolve_by_id():
    d, err = dc.resolve_device(device_id="bedroom-light-1")
    assert err is None and d["room"] == "bedroom"


def test_resolve_unknown_id_lists_known_ones():
    d, err = dc.resolve_device(device_id="nope-1")
    assert d is None and "bedroom-light-1" in err


def test_resolve_by_type_falls_back_for_legacy_callers():
    d, err = dc.resolve_device(device_type="fan")
    assert err is None and d["deviceType"] == "fan"


def test_resolve_requires_one_of_the_two():
    d, err = dc.resolve_device()
    assert d is None and err


# ---------------------------------------------------------------------------
# Discovery payload keeps the contract voice_session depends on
# ---------------------------------------------------------------------------

def test_discovery_keeps_device_type_and_power_payloads():
    payload = dc.discovery_payload()
    assert payload
    for entry in payload:
        assert "deviceType" in entry, "voice_session.py:350 reads this"
    powered = [e for e in payload if not e["readOnly"]]
    for entry in powered:
        assert "powerOn" in entry, "voice_session.py:351 reads this"


def test_discovery_exposes_capabilities_and_rooms():
    payload = dc.discovery_payload()
    strip = next(e for e in payload if e["deviceId"] == "living-strip-1")
    assert strip["capabilities"]["brightness"]["max"] == 100
    assert strip["room"] == "living"
    assert strip["roomName"] == "Living Room"


def test_two_lights_in_different_rooms_are_distinguishable():
    # The whole reason deviceId replaced deviceType in the topic.
    payload = dc.discovery_payload()
    rooms = {e["room"] for e in payload if "light" in e["deviceType"]}
    assert len(rooms) > 1, "need lights in at least two rooms to demo disambiguation"


# ---------------------------------------------------------------------------
# implies — device-side side effects
# ---------------------------------------------------------------------------

def test_set_speed_implies_power_on():
    """"Set the fan to 5" should run the fan, not just store a speed."""
    fan = dc.device_by_id("living-fan-1")
    ok, cmd, _ = dc.validate_command(fan, {"action": "setSpeed", "speed": 5})
    assert ok
    assert cmd["power"] is True, "the device turns itself on; the agent should not need two commands"
    assert cmd["speed"] == 5


def test_explicit_parameter_beats_the_implication():
    fan = dc.device_by_id("living-fan-1")
    ok, cmd, _ = dc.validate_command(
        fan, {"action": "setSpeed", "speed": 3, "power": False})
    assert ok and cmd["power"] is False, "an explicit value must win over `implies`"


def test_a_parameter_no_action_declares_is_dropped_not_forwarded():
    """The normalised command carries only what the action actually takes.

    This used to be `out.update(command)`, which forwarded every key the caller
    sent — so a key no action declares reached the device having been validated by
    nothing at all. `bluetooth` on the TV backlight is the case that matters: it is
    a READONLY capability, the pairing state the device alone may report, and the
    scene-sync agent decides whether music sync is really running by reading it.
    Letting a command assert it would let anything that can phrase a command (a
    model, or a prompt injection reaching one) fake the answer.

    Dropped rather than refused: an extra key is usually a caller being sloppy, not
    an attack, and failing the whole command would be a worse trade. What matters
    is that it goes no further.
    """
    tv = dc.device_by_id("living-tvlight-1")
    ok, cmd, _ = dc.validate_command(
        tv, {"action": "setSyncMode", "sync_mode": "music",
             "bluetooth": "connected", "somethingInvented": 1})
    assert ok, "a stray key should not fail an otherwise valid command"
    assert "bluetooth" not in cmd, "a readonly capability must not be settable"
    assert "somethingInvented" not in cmd
    assert cmd["sync_mode"] == "music"


def test_bluetooth_has_no_action_that_writes_it():
    """Belt and braces on the above: nothing in the catalog should offer to set it.

    Asserted against the catalog rather than the validator, so adding a
    `setBluetooth` action fails here rather than quietly making the state
    forgeable.
    """
    tv = dc.device_by_id("living-tvlight-1")
    assert "bluetooth" in tv["capabilities"], "the capability should exist to read"
    writers = [name for name, spec in tv["actions"].items()
               if spec.get("writes") == "bluetooth"
               or "bluetooth" in (spec.get("required") or [])
               or "bluetooth" in (spec.get("optional") or [])]
    assert writers == [], f"bluetooth is reported, never set; found {writers}"


def test_set_power_off_does_not_self_imply():
    fan = dc.device_by_id("living-fan-1")
    ok, cmd, _ = dc.validate_command(fan, {"action": "setPower", "power": False})
    assert ok and cmd["power"] is False


def test_light_property_changes_turn_the_light_on():
    for device_id, command in (
        ("living-strip-1", {"action": "setEffect", "effect": "wave"}),
        ("bedroom-light-1", {"action": "setBrightness", "brightness": 60}),
        ("bedroom-light-1", {"action": "setColor", "color": "#ff0000"}),
        ("living-led-1", {"action": "setMode", "mode": "ocean"}),
    ):
        device = dc.device_by_id(device_id)
        ok, cmd, _ = dc.validate_command(device, command)
        assert ok and cmd.get("power") is True, f"{device_id} {command['action']} should light up"


def test_cooker_start_implies_cooking():
    cooker = dc.device_by_id("kitchen-cooker-1")
    ok, cmd, _ = dc.validate_command(cooker, {"action": "start", "mode": "porridge"})
    assert ok and cmd.get("cooking") is True


def test_implied_fields_are_real_capabilities():
    for d in dc.devices():
        caps = d.get("capabilities", {})
        for action, spec in d.get("actions", {}).items():
            for field in (spec.get("implies") or {}):
                assert field in caps, (
                    f"{d['deviceId']}.{action} implies '{field}', not a capability")


def test_stop_actions_declare_that_they_stop():
    """A parameterless stop has to say what it clears.

    `stopIce` and `stop` write a boolean but take no arguments, so without an
    explicit `implies` the published payload carried no state at all and the
    device never stopped. The simulator had a fallback rule for this; the Lambda
    did not, so the two disagreed.
    """
    for device_id, action, field in (
        ("kitchen-icemaker-1", "stopIce", "making_ice"),
        ("kitchen-cooker-1", "stop", "cooking"),
    ):
        device = dc.device_by_id(device_id)
        ok, cmd, _ = dc.validate_command(device, {"action": action})
        assert ok
        assert cmd.get(field) is False, f"{device_id}.{action} must clear {field}"


def test_parameterless_actions_all_declare_an_effect():
    """Guards the class of bug above for any device added later."""
    for d in dc.devices():
        for action, spec in d.get("actions", {}).items():
            if spec.get("required") or spec.get("optional"):
                continue
            assert spec.get("implies"), (
                f"{d['deviceId']}.{action} takes no parameters and declares no "
                f"`implies`, so it would publish no state change at all"
            )


def test_phase_two_devices_are_present():
    ids = set(dc.device_ids())
    for expected in ("bedroom-humidifier-1", "living-purifier-1",
                     "kitchen-icemaker-1", "living-tvlight-1"):
        assert expected in ids


def test_maintenance_readings_are_readonly_but_devices_stay_controllable():
    """water_level / filter_life / bin_level are readings, not settings — but a
    humidifier is still a controllable device, unlike the sensor."""
    for device_id, field in (
        ("bedroom-humidifier-1", "water_level"),
        ("living-purifier-1", "filter_life"),
        ("kitchen-icemaker-1", "bin_level"),
    ):
        device = dc.device_by_id(device_id)
        assert device["capabilities"][field]["type"] == "readonly"
        assert not dc.is_readonly(device), f"{device_id} must still accept commands"
