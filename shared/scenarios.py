"""Scene definitions, their triggers, and validation — shared, pure logic.

A "scenario" is a trigger plus a list of device actions: *when this happens, put
these devices in these states*. The A2A scene-orchestration agent writes them, the
scheduler Lambda reads them, and the Admin Console will display them. All three
need the same notion of what a valid scenario is, so it lives here rather than in
whichever of them was written first.

Two things this module deliberately does NOT do:

  - It does not touch DynamoDB. `to_item`/`from_item` convert to and from the row
    shape; the caller owns the table handle. That keeps every rule below testable
    without a table and without mocks.
  - It does not execute anything. A scenario's actions are *validated* here
    against the device catalog and *executed* elsewhere, through the Gateway,
    carrying a real user identity so Cedar authorises each command. A scenario is
    data, not a capability — which is what makes it safe for an agent to write.

Trigger shape (the five-tuple, from the predecessor design):

    sceneType       what kind of thing happens      "time" | "device_state" | "sensor"
    calculationType how the value is compared       "equal" | "above" | "below" | "change"
    conditionValue  the value compared against      "23:00" | "on" | 26.5
    executionType   once, or every time             "once" | "recurring"
    subject         which device/metric it observes  device id, or "temperature"

`subject` replaces the predecessor's habit of encoding the device into
`sceneType`, which made "temperature above 26" and "the fan turned on" two
unrelated enum values instead of the same shape with different subjects.
"""

from __future__ import annotations

import re

import device_catalog

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

SCENE_TIME = "time"
SCENE_DEVICE_STATE = "device_state"
SCENE_SENSOR = "sensor"
# Sensor thresholds are real: `living-sensor-1` reports temperature, humidity,
# pm25 and co2 over MQTT and `query_sensor_history` reads them back out of
# DynamoDB. The predecessor design left this out because its sensors were a
# random-number generator, which is the only reason it was ever "unsupported".
SCENE_TYPES = (SCENE_TIME, SCENE_DEVICE_STATE, SCENE_SENSOR)

CALC_EQUAL = "equal"
CALC_ABOVE = "above"
CALC_BELOW = "below"
CALC_CHANGE = "change"
CALCULATION_TYPES = (CALC_EQUAL, CALC_ABOVE, CALC_BELOW, CALC_CHANGE)

EXEC_ONCE = "once"
EXEC_RECURRING = "recurring"
EXECUTION_TYPES = (EXEC_ONCE, EXEC_RECURRING)

# 24-hour clock. Deliberately strict: "11pm" and "23:00:00" both parse as a time
# to a human and neither is what EventBridge Scheduler accepts, so the agent is
# made to normalise before the row is written rather than at trigger time, when
# there is no one to ask.
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

STRATEGY_PREFIX = "strategy#"
TEMPLATE_PREFIX = "template#"
# The constant partition key of TemplateIndex. Templates are shared, so they are
# listed across users; a per-user template would defeat the point of a library.
TEMPLATE_SCOPE = "template"

MAX_NAME_LEN = 120
MAX_ACTIONS = 20


class ScenarioError(ValueError):
    """A scenario that cannot be stored, with a reason meant for the model.

    The message is shown to an LLM and, through it, to a user, so it says what to
    do differently — an agent that gets "invalid trigger" back will retry with
    another guess, while one that gets the valid vocabulary will fix the call.
    """


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def strategy_key(strategy_id: str) -> str:
    return f"{STRATEGY_PREFIX}{strategy_id}"


def template_key(template_id: str) -> str:
    return f"{TEMPLATE_PREFIX}{template_id}"


def id_from_key(scenario_key: str) -> str:
    for prefix in (STRATEGY_PREFIX, TEMPLATE_PREFIX):
        if scenario_key.startswith(prefix):
            return scenario_key[len(prefix):]
    return scenario_key


def is_template_key(scenario_key: str) -> bool:
    return scenario_key.startswith(TEMPLATE_PREFIX)


def slugify(name: str) -> str:
    """A stable id from a scene name.

    Derived rather than random so that saving "sleep mode" twice updates one
    scenario instead of accumulating near-duplicates — the failure mode of the
    predecessor's uuid ids, where a user ended up with four "睡眠模式" rows and no
    way to tell which one the trigger referred to.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "scenario"


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------

def _sensor_metrics() -> dict[str, list[str]]:
    """Metric name -> device ids that report it, from the catalog."""
    out: dict[str, list[str]] = {}
    for device in device_catalog.devices():
        if not device_catalog.is_readonly(device):
            continue
        for metric in (device.get("capabilities") or {}):
            out.setdefault(metric, []).append(device["deviceId"])
    return out


def validate_trigger(trigger: dict) -> tuple[dict, list[str]]:
    """Normalise a trigger and report which fields were inferred.

    Returns `(trigger, auto_inferred)`. `auto_inferred` names the fields this
    function filled in, and is surfaced to the user: a scene that fires at a time
    the user never stated is worse than one that refuses to save, so an inference
    is always reported rather than applied silently.
    """
    if not isinstance(trigger, dict):
        raise ScenarioError("trigger must be an object")

    auto: list[str] = []
    scene_type = (trigger.get("sceneType") or "").strip()
    if scene_type not in SCENE_TYPES:
        raise ScenarioError(
            f"sceneType must be one of {list(SCENE_TYPES)}; got {scene_type!r}. "
            f"Use 'time' for a clock trigger, 'device_state' to react to a "
            f"device, 'sensor' for a temperature/humidity/pm25/co2 threshold.")

    out: dict = {"sceneType": scene_type}
    subject = (str(trigger.get("subject") or "")).strip()
    condition = trigger.get("conditionValue")
    calc = (trigger.get("calculationType") or "").strip()

    if scene_type == SCENE_TIME:
        if not isinstance(condition, str) or not TIME_RE.match(condition.strip()):
            raise ScenarioError(
                "a time trigger needs conditionValue as 24-hour HH:MM, e.g. "
                f"'23:00'; got {condition!r}")
        out["conditionValue"] = condition.strip()
        # The only comparison a clock supports. Inferred rather than demanded
        # because "above 23:00" is not a thing a user means.
        if calc and calc != CALC_EQUAL:
            raise ScenarioError(
                f"a time trigger only supports calculationType 'equal'; "
                f"got {calc!r}")
        if not calc:
            auto.append("calculationType")
        out["calculationType"] = CALC_EQUAL
        if subject:
            raise ScenarioError("a time trigger has no subject; omit it")

    elif scene_type == SCENE_DEVICE_STATE:
        known = device_catalog.device_ids()
        if subject not in known:
            raise ScenarioError(
                f"subject must be a device id for a device_state trigger. "
                f"Known ids: {sorted(known)}")
        out["subject"] = subject
        if condition in (None, ""):
            raise ScenarioError(
                "a device_state trigger needs conditionValue — the state to "
                "watch for, e.g. 'on' or 'off'")
        out["conditionValue"] = condition
        if not calc:
            calc = CALC_EQUAL
            auto.append("calculationType")
        if calc not in (CALC_EQUAL, CALC_CHANGE):
            raise ScenarioError(
                f"a device_state trigger supports 'equal' or 'change'; got {calc!r}")
        out["calculationType"] = calc

    else:  # SCENE_SENSOR
        metrics = _sensor_metrics()
        if subject not in metrics:
            raise ScenarioError(
                f"subject must be a sensor metric for a sensor trigger. "
                f"Available: {sorted(metrics)}")
        out["subject"] = subject
        # Decimal, not float: DynamoDB refuses Python floats outright
        # ("Float types are not supported"), and the failure lands at PutItem —
        # after validation has passed and the model has been told the trigger is
        # fine. It then retries with the same value, fails identically, and (as
        # observed) reports success anyway. Comparisons still work, because the
        # threshold is only ever compared against other numbers.
        from decimal import Decimal, InvalidOperation

        try:
            out["conditionValue"] = Decimal(str(condition).strip())
        except (TypeError, ValueError, InvalidOperation, AttributeError):
            raise ScenarioError(
                f"a sensor trigger needs a numeric conditionValue; "
                f"got {condition!r}") from None
        if not calc:
            raise ScenarioError(
                f"a sensor trigger needs calculationType 'above' or 'below' — "
                f"there is no sensible default for a threshold, and guessing one "
                f"would build a scene that fires at the wrong times")
        if calc not in (CALC_ABOVE, CALC_BELOW):
            raise ScenarioError(
                f"a sensor trigger supports 'above' or 'below'; got {calc!r}")
        out["calculationType"] = calc

    execution = (trigger.get("executionType") or "").strip()
    if not execution:
        # Recurring is the safer default: a scene the user expected daily that
        # fires once looks broken, while one that repeats is visible and can be
        # switched off. Reported either way.
        execution = EXEC_RECURRING
        auto.append("executionType")
    if execution not in EXECUTION_TYPES:
        raise ScenarioError(
            f"executionType must be one of {list(EXECUTION_TYPES)}; got {execution!r}")
    out["executionType"] = execution
    out["autoInferredFields"] = auto
    return out, auto


def describe_trigger(trigger: dict) -> str:
    """One human line for a trigger, for a reply or the console."""
    scene = trigger.get("sceneType")
    calc = trigger.get("calculationType")
    value = trigger.get("conditionValue")
    subject = trigger.get("subject", "")
    every = trigger.get("executionType") == EXEC_RECURRING
    if scene == SCENE_TIME:
        return f"{'every day at' if every else 'once at'} {value}"
    if scene == SCENE_DEVICE_STATE:
        if calc == CALC_CHANGE:
            return f"when {subject} changes state"
        return f"when {subject} is {value}"
    return f"when {subject} goes {calc} {value}"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def validate_actions(actions) -> tuple[list[dict], list[str]]:
    """Validate device actions against the catalog, returning normalised copies.

    Validated at WRITE time as well as at execution time, on purpose. The
    execution path validates because it must; this validates so that a scene the
    user was told was saved cannot turn out to be unrunnable hours later, when
    nobody is watching and there is no one to ask for a correction.

    Values are clamped by `device_catalog.validate_command`, and the CLAMPED
    command is what gets stored — otherwise the stored scene and the executed one
    differ, which is the sort of discrepancy that surfaces as "it set brightness
    to 100 but I asked for 150" long after the fact.
    """
    if not isinstance(actions, list) or not actions:
        raise ScenarioError(
            "deviceActions must be a non-empty list of "
            "{deviceId, command:{action, ...}} objects")
    if len(actions) > MAX_ACTIONS:
        raise ScenarioError(
            f"a scenario may hold at most {MAX_ACTIONS} actions; got {len(actions)}")

    out: list[dict] = []
    warnings: list[str] = []
    for index, raw in enumerate(actions):
        if not isinstance(raw, dict):
            raise ScenarioError(f"deviceActions[{index}] must be an object")
        device_id = raw.get("deviceId") or raw.get("device_id") or ""
        device_type = raw.get("deviceType") or raw.get("device_type") or ""
        command = raw.get("command")
        if not isinstance(command, dict):
            raise ScenarioError(
                f"deviceActions[{index}] needs a command object, e.g. "
                f"{{\"action\": \"setPower\", \"power\": true}}")
        # resolve_device reports failure as (None, reason) rather than raising.
        device, reason = device_catalog.resolve_device(
            device_id=device_id or None, device_type=device_type or None)
        if device is None:
            raise ScenarioError(f"deviceActions[{index}]: {reason}")

        ok, normalised, notes = device_catalog.validate_command(device, command)
        if not ok:
            raise ScenarioError(f"deviceActions[{index}]: {notes[0]}")
        for note in notes:
            warnings.append(f"{device['deviceId']}: {note}")
        out.append({"deviceId": device["deviceId"],
                    "deviceType": device.get("deviceType", ""),
                    "command": _storable(normalised)})
    return out, warnings


def _storable(command: dict) -> dict:
    """Convert floats to Decimal so the command can be written to DynamoDB.

    `validate_command` is shared with iot-control, which publishes JSON over MQTT
    and is perfectly happy with a float — so it returns one, and a fractional
    value (brightness 55.5) then fails at PutItem with "Float types are not
    supported". Converting here rather than in the catalog keeps that Lambda's
    behaviour unchanged; a scenario is the only consumer that persists a command.

    The same class of failure hit sensor thresholds, and it is nasty in the same
    way: validation passes, the model is told the scene is fine, the write fails,
    and the model reports success anyway.
    """
    from decimal import Decimal

    return {k: (Decimal(str(v)) if isinstance(v, float) else v)
            for k, v in command.items()}


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def build_scenario(user_id: str, name: str, trigger: dict, actions,
                   description: str = "", is_template: bool = False,
                   is_active: bool = True, now: str = "",
                   source: str = "scene-orchestration-agent") -> dict:
    """A validated scenario row. Raises ScenarioError on anything unstorable."""
    if not user_id:
        raise ScenarioError("userId is required")
    clean_name = (name or "").strip()
    if not clean_name:
        raise ScenarioError("name is required — it is how the user refers to the scene")
    if len(clean_name) > MAX_NAME_LEN:
        raise ScenarioError(f"name must be at most {MAX_NAME_LEN} characters")

    norm_trigger, auto = validate_trigger(trigger)
    norm_actions, warnings = validate_actions(actions)

    scenario_id = slugify(clean_name)
    key = template_key(scenario_id) if is_template else strategy_key(scenario_id)
    item = {
        "userId": user_id,
        "scenarioKey": key,
        "scenarioId": scenario_id,
        "name": clean_name,
        "description": (description or "").strip(),
        "trigger": norm_trigger,
        "deviceActions": norm_actions,
        "isActive": bool(is_active),
        "isTemplate": bool(is_template),
        "createdAt": now,
        "updatedAt": now,
        "source": source,
    }
    if is_template:
        # Only templates carry the GSI partition key, so the index holds exactly
        # the template rows — a sparse GSI rather than one filtered on read.
        item["templateScope"] = TEMPLATE_SCOPE
    return {"item": item, "autoInferredFields": auto, "warnings": warnings}


def pending_actions(item: dict) -> list[dict]:
    """The scene's actions in the shape the orchestrator executes.

    The scene-orchestration agent never controls a device. It returns these and
    the orchestrator calls `control_device` once per entry, carrying the user's
    identity, so every command is authorised by Cedar exactly as a hand-typed one
    is. Giving the sub-agent IoT permissions instead would have made scheduled and
    scene-driven commands the one path the Admin Console could not govern.
    """
    return [{"deviceId": a["deviceId"], "command": a["command"]}
            for a in (item.get("deviceActions") or [])]


def summarise(item: dict) -> dict:
    """A compact view for a tool result or a list — no raw DynamoDB types."""
    return {
        "scenarioId": item.get("scenarioId") or id_from_key(item.get("scenarioKey", "")),
        "name": item.get("name", ""),
        "description": item.get("description", ""),
        "trigger": item.get("trigger") or {},
        "triggerDescription": describe_trigger(item.get("trigger") or {}),
        "actionCount": len(item.get("deviceActions") or []),
        "isActive": bool(item.get("isActive")),
        "isTemplate": bool(item.get("isTemplate")),
        "updatedAt": item.get("updatedAt", ""),
    }
