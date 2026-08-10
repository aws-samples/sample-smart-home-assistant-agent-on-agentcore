"""Tests for the scenario model.

A scenario is written once by an agent and executed later, unattended. That gap is
where the risk lives: a scene the user was told was saved but which cannot run, or
which runs differently from what was stored, fails at a time when nobody is
watching and there is no one to ask for a correction. So the tests here care most
about two things:

  - validation happens at WRITE time, not only at execution time
  - what gets stored is what will be executed (clamped values included)

The trigger tests lean on inference being *reported*. A scene that fires at a time
the user never named is worse than one that refuses to save, so any field the
model filled in must come back in `autoInferredFields`.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import device_catalog  # noqa: E402
import scenarios as sc  # noqa: E402

NOW = "2026-08-10T04:00:00Z"


def _led_off():
    return [{"deviceId": "living-led-1",
             "command": {"action": "setPower", "power": False}}]


# ---------------------------------------------------------------------------
# Time triggers
# ---------------------------------------------------------------------------

def test_a_time_trigger_normalises_and_reports_what_it_inferred():
    trigger, auto = sc.validate_trigger(
        {"sceneType": "time", "conditionValue": "23:00"})
    assert trigger["conditionValue"] == "23:00"
    assert trigger["calculationType"] == sc.CALC_EQUAL
    assert trigger["executionType"] == sc.EXEC_RECURRING
    # Both were filled in, and both are declared.
    assert set(auto) == {"calculationType", "executionType"}


@pytest.mark.parametrize("bad", ["11pm", "23:00:00", "7:5", "25:00", "23-00", "", None])
def test_a_time_that_is_not_24_hour_hh_mm_is_refused(bad):
    """"11pm" and "23:00:00" both read as a time to a human and neither is what
    EventBridge Scheduler accepts. Refusing at write time means the agent can ask
    the user; refusing at trigger time means silence."""
    with pytest.raises(sc.ScenarioError):
        sc.validate_trigger({"sceneType": "time", "conditionValue": bad})


def test_a_time_trigger_rejects_a_comparison_that_makes_no_sense():
    with pytest.raises(sc.ScenarioError, match="only supports"):
        sc.validate_trigger({"sceneType": "time", "conditionValue": "23:00",
                             "calculationType": "above"})


def test_a_time_trigger_has_no_subject():
    with pytest.raises(sc.ScenarioError, match="no subject"):
        sc.validate_trigger({"sceneType": "time", "conditionValue": "23:00",
                             "subject": "living-led-1"})


# ---------------------------------------------------------------------------
# Device-state triggers
# ---------------------------------------------------------------------------

def test_a_device_state_trigger_requires_a_real_device_id():
    """The error carries the known ids — an agent told only "invalid" retries with
    another guess, one told the vocabulary fixes the call."""
    with pytest.raises(sc.ScenarioError) as exc:
        sc.validate_trigger({"sceneType": "device_state", "subject": "no-such-device",
                             "conditionValue": "on"})
    assert "living-led-1" in str(exc.value)


def test_a_device_state_trigger_defaults_to_equality_and_says_so():
    trigger, auto = sc.validate_trigger(
        {"sceneType": "device_state", "subject": "living-fan-1",
         "conditionValue": "on", "executionType": "recurring"})
    assert trigger["calculationType"] == sc.CALC_EQUAL
    assert auto == ["calculationType"]


def test_a_device_state_trigger_supports_change():
    trigger, _ = sc.validate_trigger(
        {"sceneType": "device_state", "subject": "living-fan-1",
         "calculationType": "change", "conditionValue": "any"})
    assert sc.describe_trigger(trigger) == "when living-fan-1 changes state"


# ---------------------------------------------------------------------------
# Sensor triggers
# ---------------------------------------------------------------------------

def test_sensor_metrics_come_from_the_catalog():
    """Not a hardcoded list: adding a metric to a sensor in the catalog should
    make it available as a trigger subject with no edit here."""
    metrics = sc._sensor_metrics()
    assert "temperature" in metrics
    assert metrics["temperature"] == ["living-sensor-1"]
    # Only read-only devices contribute metrics.
    assert "setPower" not in metrics


def test_a_sensor_threshold_is_a_real_trigger():
    """The predecessor design left sensor triggers out because its sensors were a
    random-number generator. living-sensor-1 reports over MQTT and the readings
    land in DynamoDB, so a threshold is now something that can actually fire."""
    trigger, auto = sc.validate_trigger(
        {"sceneType": "sensor", "subject": "temperature",
         "conditionValue": "26.5", "calculationType": "above"})
    assert trigger["conditionValue"] == 26.5  # coerced to a number
    assert auto == ["executionType"]
    assert sc.describe_trigger(trigger) == "when temperature goes above 26.5"


def test_a_sensor_trigger_refuses_to_guess_the_comparison():
    """Unlike the other two types, there is no safe default here: 'above 26' and
    'below 26' build opposite scenes, and picking one silently would make the
    scene fire at exactly the wrong times."""
    with pytest.raises(sc.ScenarioError, match="no sensible default"):
        sc.validate_trigger({"sceneType": "sensor", "subject": "temperature",
                             "conditionValue": 26})


def test_a_sensor_trigger_needs_a_number():
    with pytest.raises(sc.ScenarioError, match="numeric"):
        sc.validate_trigger({"sceneType": "sensor", "subject": "temperature",
                             "conditionValue": "warm", "calculationType": "above"})


def test_a_sensor_trigger_rejects_a_metric_no_device_reports():
    with pytest.raises(sc.ScenarioError) as exc:
        sc.validate_trigger({"sceneType": "sensor", "subject": "loudness",
                             "conditionValue": 30, "calculationType": "above"})
    assert "temperature" in str(exc.value)


def test_an_unknown_scene_type_lists_the_real_ones():
    with pytest.raises(sc.ScenarioError) as exc:
        sc.validate_trigger({"sceneType": "geofence", "conditionValue": "home"})
    for known in sc.SCENE_TYPES:
        assert known in str(exc.value)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def test_actions_are_validated_against_the_catalog_at_write_time():
    """Execution validates too, but only this check happens while a human is
    still in the conversation to correct it."""
    with pytest.raises(sc.ScenarioError, match="Invalid action"):
        sc.validate_actions([{"deviceId": "living-led-1",
                              "command": {"action": "explode"}}])


def test_a_clamped_value_is_what_gets_stored():
    """Otherwise the stored scene and the executed one differ, and the mismatch
    surfaces as "it set it to 8 but I said 99" long after the fact."""
    actions, warnings = sc.validate_actions(
        [{"deviceId": "living-fan-1", "command": {"action": "setSpeed", "speed": 99}}])
    assert actions[0]["command"]["speed"] == 8
    assert warnings and "99" in warnings[0]


def test_a_sensor_cannot_be_commanded():
    with pytest.raises(sc.ScenarioError, match="read-only sensor"):
        sc.validate_actions([{"deviceId": "living-sensor-1",
                              "command": {"action": "setPower", "power": True}}])


def test_an_unknown_device_names_the_known_ones():
    with pytest.raises(sc.ScenarioError) as exc:
        sc.validate_actions([{"deviceId": "ghost-lamp",
                              "command": {"action": "setPower", "power": True}}])
    assert "living-led-1" in str(exc.value)


def test_a_device_type_still_resolves_for_backward_compatibility():
    actions, _ = sc.validate_actions(
        [{"deviceType": "fan", "command": {"action": "setPower", "power": True}}])
    assert actions[0]["deviceId"] == "living-fan-1"


def test_an_empty_action_list_is_refused():
    with pytest.raises(sc.ScenarioError, match="non-empty"):
        sc.validate_actions([])


def test_too_many_actions_are_refused():
    many = [{"deviceId": "living-led-1", "command": {"action": "setPower", "power": True}}
            ] * (sc.MAX_ACTIONS + 1)
    with pytest.raises(sc.ScenarioError, match="at most"):
        sc.validate_actions(many)


def test_a_missing_command_object_says_what_one_looks_like():
    with pytest.raises(sc.ScenarioError) as exc:
        sc.validate_actions([{"deviceId": "living-led-1"}])
    assert "setPower" in str(exc.value)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def test_building_a_scenario_produces_a_storable_row():
    built = sc.build_scenario(
        "alice@example.com", "Sleep Mode",
        {"sceneType": "time", "conditionValue": "23:00"}, _led_off(), now=NOW)
    item = built["item"]
    assert item["userId"] == "alice@example.com"
    assert item["scenarioKey"] == "strategy#sleep-mode"
    assert item["isActive"] is True
    assert item["isTemplate"] is False
    assert item["createdAt"] == item["updatedAt"] == NOW
    # Not a template, so it stays out of the sparse GSI.
    assert "templateScope" not in item


def test_the_id_is_derived_from_the_name_so_saving_twice_updates_one_row():
    """Random ids gave the predecessor design four rows named "睡眠模式" and no way
    to tell which one a trigger referred to."""
    a = sc.build_scenario("u", "Sleep Mode", {"sceneType": "time",
                          "conditionValue": "23:00"}, _led_off(), now=NOW)
    b = sc.build_scenario("u", "sleep  mode!", {"sceneType": "time",
                          "conditionValue": "22:00"}, _led_off(), now=NOW)
    assert a["item"]["scenarioKey"] == b["item"]["scenarioKey"]


def test_a_template_carries_the_gsi_partition_key():
    built = sc.build_scenario("u", "Movie Night", {"sceneType": "time",
                              "conditionValue": "20:00"}, _led_off(),
                              is_template=True, now=NOW)
    assert built["item"]["scenarioKey"].startswith(sc.TEMPLATE_PREFIX)
    assert built["item"]["templateScope"] == sc.TEMPLATE_SCOPE


def test_a_scenario_needs_a_name():
    with pytest.raises(sc.ScenarioError, match="name is required"):
        sc.build_scenario("u", "   ", {"sceneType": "time",
                          "conditionValue": "23:00"}, _led_off(), now=NOW)


def test_a_scenario_needs_a_user():
    with pytest.raises(sc.ScenarioError, match="userId"):
        sc.build_scenario("", "X", {"sceneType": "time",
                          "conditionValue": "23:00"}, _led_off(), now=NOW)


def test_keys_round_trip():
    assert sc.id_from_key(sc.strategy_key("sleep-mode")) == "sleep-mode"
    assert sc.id_from_key(sc.template_key("movie-night")) == "movie-night"
    assert sc.is_template_key(sc.template_key("x")) is True
    assert sc.is_template_key(sc.strategy_key("x")) is False


# ---------------------------------------------------------------------------
# The execution contract
# ---------------------------------------------------------------------------

def test_pending_actions_are_the_shape_the_orchestrator_executes():
    """The scene agent returns these; the orchestrator calls control_device once
    per entry with the user's identity, so Cedar authorises each command exactly
    as it does a hand-typed one. Giving the sub-agent IoT permissions instead
    would have made scene-driven commands the one path the Admin Console could not
    govern."""
    built = sc.build_scenario(
        "u", "Sleep Mode", {"sceneType": "time", "conditionValue": "23:00"},
        [{"deviceId": "living-led-1", "command": {"action": "setPower", "power": False}},
         {"deviceId": "living-fan-1", "command": {"action": "setSpeed", "speed": 1}}],
        now=NOW)
    actions = sc.pending_actions(built["item"])
    assert [a["deviceId"] for a in actions] == ["living-led-1", "living-fan-1"]
    # Addressed by id, never by type: a type is ambiguous once two units share it.
    assert all("deviceId" in a and "command" in a for a in actions)
    assert all("deviceType" not in a for a in actions)


def test_summarise_returns_no_dynamodb_types():
    built = sc.build_scenario("u", "Sleep Mode", {"sceneType": "time",
                              "conditionValue": "23:00"}, _led_off(), now=NOW)
    out = sc.summarise(built["item"])
    assert out["triggerDescription"] == "every day at 23:00"
    assert out["actionCount"] == 1
    import json
    json.dumps(out)  # would raise on a Decimal


# ---------------------------------------------------------------------------
# DynamoDB storability
# ---------------------------------------------------------------------------

def test_a_sensor_threshold_is_stored_as_decimal_not_float():
    """DynamoDB refuses Python floats outright, and the refusal lands at PutItem —
    after validation passed and the agent was told the trigger was fine. Observed
    live: the model retried the same value, failed identically, then reported the
    scene as saved. It fires at no threshold at all."""
    from decimal import Decimal

    trigger, _ = sc.validate_trigger(
        {"sceneType": "sensor", "subject": "temperature",
         "conditionValue": "26.5", "calculationType": "above"})
    assert isinstance(trigger["conditionValue"], Decimal)
    assert trigger["conditionValue"] == Decimal("26.5")
    # Still a number for comparison purposes, which is all a threshold is for.
    assert trigger["conditionValue"] > 26
    assert trigger["conditionValue"] < 27


def test_an_integer_threshold_is_also_a_decimal():
    from decimal import Decimal

    trigger, _ = sc.validate_trigger(
        {"sceneType": "sensor", "subject": "co2", "conditionValue": 800,
         "calculationType": "above"})
    assert isinstance(trigger["conditionValue"], Decimal)


def test_a_fractional_device_value_is_stored_as_decimal():
    """validate_command is shared with iot-control, which publishes JSON over MQTT
    and returns a float happily. Persisting that float is what breaks."""
    from decimal import Decimal

    actions, _ = sc.validate_actions(
        [{"deviceId": "living-strip-1",
          "command": {"action": "setBrightness", "brightness": 55.5}}])
    assert isinstance(actions[0]["command"]["brightness"], Decimal)


def test_no_float_survives_anywhere_in_a_built_scenario():
    """One check over the whole row: a float in any field fails the same way, and
    the failure surfaces long after the agent has claimed success."""
    built = sc.build_scenario(
        "u", "Cool Down",
        {"sceneType": "sensor", "subject": "temperature",
         "conditionValue": "27.5", "calculationType": "above"},
        [{"deviceId": "living-strip-1",
          "command": {"action": "setBrightness", "brightness": 33.3}},
         {"deviceId": "living-fan-1", "command": {"action": "setSpeed", "speed": 2}}],
        now=NOW)

    floats = []

    def walk(value, path="item"):
        if isinstance(value, float):
            floats.append(path)
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]")

    walk(built["item"])
    assert not floats, f"float(s) that DynamoDB will reject at PutItem: {floats}"


def test_booleans_are_left_alone():
    """bool is not a float, and a Decimal power flag would be wrong on the wire."""
    actions, _ = sc.validate_actions(_led_off())
    assert actions[0]["command"]["power"] is False
