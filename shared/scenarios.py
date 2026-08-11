"""Scene definitions, their triggers, and validation — shared, pure logic.

A "scenario" is a trigger plus a list of device actions: *when this happens, put
these devices in these states*. The A2A task-management agent writes them, the
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
#
# `solar` fires at sunrise or sunset, so its clock time changes every day and
# depends on where the user is. It therefore has no cron of its own here —
# `cron_for` returns "" and the runner's daily `mode="solar"` pass computes
# tomorrow's time and updates the schedule. See shared/solar.py.
#
# `manual` never fires by itself. It is a one-tap command: stored actions with a
# name, run when the user asks for it by name. Both of the automatic execution
# paths ignore it, which is the reason it is a trigger type rather than a second
# table — `cron_for` gives it no schedule and the sweep does not collect it.
SCENE_SOLAR = "solar"
SCENE_MANUAL = "manual"
SCENE_TYPES = (SCENE_TIME, SCENE_DEVICE_STATE, SCENE_SENSOR, SCENE_SOLAR,
               SCENE_MANUAL)

# What a solar trigger's `subject` may be. Mirrors solar.EVENTS; duplicated rather
# than imported because this module is copied into containers that do not all
# carry solar.py, and an ImportError here would break every trigger type.
SOLAR_EVENTS = ("sunrise", "sunset")
# How far a solar trigger may be shifted, in minutes. Bounded so an offset cannot
# quietly turn "at sunset" into a different day; ±4 hours covers "an hour before
# sunset" and every reasonable variant of it.
MAX_SOLAR_OFFSET_MINUTES = 240

CALC_EQUAL = "equal"
CALC_ABOVE = "above"
CALC_BELOW = "below"
CALC_CHANGE = "change"
CALCULATION_TYPES = (CALC_EQUAL, CALC_ABOVE, CALC_BELOW, CALC_CHANGE)

EXEC_ONCE = "once"
EXEC_RECURRING = "recurring"
EXECUTION_TYPES = (EXEC_ONCE, EXEC_RECURRING)

# Who wrote a row. Display only — nothing branches on it, which is why the agent's
# rename did not require a data migration. `SOURCE_LEGACY_AGENT` is the value rows
# written before the rename carry; both are valid and both mean the same agent.
SOURCE_AGENT = "task-management-agent"
SOURCE_LEGACY_AGENT = "scene-orchestration-agent"
KNOWN_SOURCES = (SOURCE_AGENT, SOURCE_LEGACY_AGENT)

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
            f"device, 'sensor' for a temperature/humidity/pm25/co2 threshold, "
            f"'solar' for sunrise/sunset, 'manual' for a one-tap command that "
            f"only runs when the user asks for it by name.")

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

    elif scene_type == SCENE_SOLAR:
        if subject not in SOLAR_EVENTS:
            raise ScenarioError(
                f"a solar trigger needs subject 'sunrise' or 'sunset'; "
                f"got {subject!r}")
        out["subject"] = subject
        # The offset in minutes, negative for "before". Stored as an int rather
        # than Decimal because it is a count, not a measurement, and DynamoDB
        # takes int fine.
        if condition in (None, ""):
            offset = 0
            auto.append("conditionValue")
        else:
            try:
                offset = int(str(condition).strip())
            except (TypeError, ValueError):
                raise ScenarioError(
                    f"a solar trigger's conditionValue is an offset in whole "
                    f"minutes — negative for before, 0 for exactly at the event. "
                    f"Got {condition!r}") from None
        if abs(offset) > MAX_SOLAR_OFFSET_MINUTES:
            raise ScenarioError(
                f"a solar offset must be within ±{MAX_SOLAR_OFFSET_MINUTES} "
                f"minutes; got {offset}")
        out["conditionValue"] = offset
        # Same reasoning as `time`: there is no "above sunset".
        if calc and calc != CALC_EQUAL:
            raise ScenarioError(
                f"a solar trigger only supports calculationType 'equal'; "
                f"got {calc!r}")
        if not calc:
            auto.append("calculationType")
        out["calculationType"] = CALC_EQUAL

    elif scene_type == SCENE_MANUAL:
        # No condition, no subject, no comparison. A manual scene is a named set
        # of actions; asking for a threshold would be asking what a button is
        # greater than.
        if subject:
            raise ScenarioError(
                "a manual trigger has no subject — it runs when the user asks "
                "for it by name; omit it")
        if condition not in (None, ""):
            raise ScenarioError(
                f"a manual trigger has no conditionValue — there is nothing to "
                f"compare. Got {condition!r}")
        if calc and calc != CALC_EQUAL:
            raise ScenarioError(
                f"a manual trigger has no comparison; omit calculationType "
                f"(got {calc!r})")
        if not calc:
            auto.append("calculationType")
        out["calculationType"] = CALC_EQUAL

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
    if scene == SCENE_SOLAR:
        try:
            offset = int(value or 0)
        except (TypeError, ValueError):
            offset = 0
        if offset == 0:
            return f"every day at {subject}"
        minutes = abs(offset)
        when = "before" if offset < 0 else "after"
        return f"every day {minutes} minutes {when} {subject}"
    if scene == SCENE_MANUAL:
        # Says what it does rather than what fires it, because nothing fires it.
        return "when you ask for it by name"
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
                   source: str = SOURCE_AGENT) -> dict:
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

    The task-management agent never controls a device. It returns these and
    the orchestrator calls `control_device` once per entry, carrying the user's
    identity, so every command is authorised by Cedar exactly as a hand-typed one
    is. Giving the sub-agent IoT permissions instead would have made scheduled and
    scene-driven commands the one path the Admin Console could not govern.
    """
    return [{"deviceId": a["deviceId"], "command": a["command"]}
            for a in (item.get("deviceActions") or [])]


def cron_for(trigger: dict) -> str:
    """The EventBridge Scheduler expression for a time trigger, or "".

    Only a `time` trigger gets a cron from THIS function. The other four are each
    "" for a different reason, and the reasons matter to any caller that reconciles
    schedules:

      - `device_state` / `sensor` have no clock to fire on. They are evaluated
        against the current reading by the runner's recurring sweep, which is one
        shared schedule rather than one per scene.
      - `solar` has a clock time, but a different one every day and one that
        depends on the user's coordinates. Its schedule exists and is (re)computed
        by the runner's daily `mode="solar"` pass via `solar_cron_for`. A
        reconciler must therefore NOT treat a solar scene's schedule as an orphan
        just because this function returned "" — see
        cdk/lambda/admin-api/scenario_schedules.py, where deleting them would have
        wiped each night's recompute on the next scene save.
      - `manual` never fires on its own. No schedule is correct.

    `cron(m H * * ? *)`, not `rate(...)`: Scheduler's cron requires six fields with
    `?` in either day-of-month or day-of-week, and a five-field Unix expression is
    rejected. The minute and hour come from a `conditionValue` that TIME_RE has
    already proven is 24-hour HH:MM, so there is nothing to parse defensively here.
    """
    if trigger.get("sceneType") != SCENE_TIME:
        return ""
    hour, minute = trigger["conditionValue"].split(":")
    return f"cron({int(minute)} {int(hour)} * * ? *)"


def wants_schedule(trigger: dict) -> bool:
    """Whether this trigger owns a schedule of its own, whoever computes it.

    The distinction `cron_for` cannot express: a solar scene HAS a schedule but
    this module cannot name its expression, because that needs the user's
    coordinates and today's date. A reconciler asks this question, not
    `bool(cron_for(...))` — the latter reports False for solar and the schedule
    then looks like an orphan to delete.
    """
    return trigger.get("sceneType") in (SCENE_TIME, SCENE_SOLAR)


def solar_offset_minutes(trigger: dict) -> int:
    """A solar trigger's offset in whole minutes; 0 when absent or unreadable."""
    try:
        return int(trigger.get("conditionValue") or 0)
    except (TypeError, ValueError):
        return 0


# Schedule names are `scn-<user hash>-<scenario id>`. The user id is hashed
# because a Cognito sub is 36 characters against Scheduler's 64-character limit,
# and because a schedule name is not the place to publish an identifier.
# Deterministic, so the same scene always maps to the same schedule and a re-save
# updates rather than duplicating.
#
# Here rather than in the admin Lambda because TWO processes now derive it: the
# admin API reconciles on save, and the runner recomputes solar times nightly. Two
# copies of this function would each create a schedule the other treats as an
# orphan, and they would delete each other's work on alternate runs.
SCHEDULE_NAME_PREFIX = "scn-"


def schedule_name(user_id: str, scenario_id: str) -> str:
    import hashlib

    user_tag = hashlib.sha256((user_id or "").encode()).hexdigest()[:12]
    # Scheduler accepts [0-9a-zA-Z-_.]; a scenario id is already a slug.
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in scenario_id)
    return f"{SCHEDULE_NAME_PREFIX}{user_tag}-{safe}"[:64]


def should_fire(trigger: dict, reading) -> bool:
    """Whether a non-time trigger is satisfied by the latest reading.

    `reading` is the device's current state value or the sensor's latest sample;
    None means "no data", which is never a reason to fire — a scene that runs
    because a sensor went quiet is worse than one that does not run.

    Edge detection is deliberately NOT done here. This answers "is the condition
    true now", and the caller decides whether that is a new event, because only
    the caller knows what it saw last time.
    """
    if reading is None:
        return False
    calc = trigger.get("calculationType")
    expected = trigger.get("conditionValue")

    if calc == CALC_CHANGE:
        # Any reading at all satisfies "changed"; the caller compares against the
        # value it recorded on the previous sweep.
        return True

    if calc in (CALC_ABOVE, CALC_BELOW):
        try:
            left, right = float(reading), float(expected)
        except (TypeError, ValueError):
            return False
        return left > right if calc == CALC_ABOVE else left < right

    if calc == CALC_EQUAL:
        # A device state compared as a string, so "on"/True/"true" all match. The
        # simulator reports booleans and a user says "on"; treating those as
        # different values would make the trigger silently never fire.
        return _as_state(reading) == _as_state(expected)
    return False


def _as_state(value) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    text = str(value).strip().lower()
    if text in ("true", "1", "on", "yes"):
        return "on"
    if text in ("false", "0", "off", "no"):
        return "off"
    return text


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
        # Passed through as stored rather than normalised to the current agent
        # name: a row written before the rename says so, and rewriting history to
        # look tidy would hide when a scene was actually created.
        "source": item.get("source", ""),
    }
