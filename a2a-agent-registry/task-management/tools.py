"""Task-management tools, built per request from the verified caller.

Unlike the other tool-using sub-agents, these do NOT go through the tools Gateway:
a scene is this agent's own data, not a device command, so there is nothing for
Cedar to authorise about writing one. What that costs is the Gateway's identity
plumbing, so the partition key has to be handled carefully here instead:

  - `user_id` is closed over from `caller.sub`, verified from the forwarded
    idToken, and appears in NO model-facing signature. A tool parameter the model
    can fill in is a tool parameter a prompt injection can fill in, and this one
    would read and write another user's scenes.
  - The table is the scenarios table and nothing else. The runtime's IAM grant
    covers only that table, so even a bug here cannot reach the skills table
    where permissions and prompts live.

The agent stores actions; it never applies them. `create_scenario` returns the
scene's `pendingActions` for the orchestrator to execute through `control_device`,
under the user's own identity. That is what keeps a scene-driven command subject to
exactly the same Cedar policy as a hand-typed one — see shared/scenarios.py.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os

logger = logging.getLogger(__name__)

TABLE_ENV = "SCENARIOS_TABLE_NAME"
DEFAULT_TABLE = "smarthome-scenarios"
# Read for exactly one field: whether this user has coordinates, which a sunrise or
# sunset trigger cannot do without. The runtime already has GetItem on this table
# for prompt governance, so no new grant is involved.
SKILLS_TABLE_ENV = "SKILLS_TABLE_NAME"

# How many templates find_template considers. The library is small and shared, so
# this is a context bound rather than a scale one.
TEMPLATE_SCAN_LIMIT = 25
TOP_TEMPLATES = 3

_resource = None


def _table():
    global _resource
    if _resource is None:
        import boto3

        _resource = boto3.resource(
            "dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    return _resource.Table(os.environ.get(TABLE_ENV, DEFAULT_TABLE))


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plain(value):
    """DynamoDB hands back Decimal, which json.dumps refuses."""
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _ok(payload: dict) -> str:
    return json.dumps(_plain(payload), ensure_ascii=False)


def _err(message: str) -> str:
    """An error the model can act on.

    Returned rather than raised: a Strands tool that raises gives the model a
    stack trace, while a message naming the valid vocabulary gets the next call
    right. Every message here comes from ScenarioError, which is written for
    exactly this audience.
    """
    return json.dumps({"error": message}, ensure_ascii=False)


def _relevance(template: dict, intent: str) -> int:
    """How well a template matches an intent — token overlap, not embeddings.

    A deliberate non-answer to semantic search: the library holds a handful of
    templates, and a vector index would be more moving parts than the problem
    has. If the library grows past a page this should become a real retrieval
    call rather than a bigger scoring function.
    """
    words = {w for w in intent.lower().split() if len(w) > 2}
    if not words:
        return 0
    haystack = " ".join([
        template.get("name", ""), template.get("description", ""),
        " ".join(a.get("deviceId", "") for a in template.get("deviceActions") or []),
    ]).lower()
    return sum(1 for w in words if w in haystack)


def build_tools(caller) -> list:
    """Return this request's tools, scoped to `caller`."""
    import sys
    from pathlib import Path

    # shared/ is copied next to the agent code by deploy.py's render step.
    shared = Path(__file__).resolve().parent.parent / "shared"
    if shared.is_dir() and str(shared) not in sys.path:
        sys.path.insert(0, str(shared))

    try:
        import scenarios as sc
    except ImportError as exc:  # pragma: no cover - packaging failure
        logger.error("scenario model unavailable: %s", exc)
        return []

    if not caller.sub:
        # Every tool below reads or writes rows keyed on this value. Without it
        # there is no scope to act in, and defaulting to anything shared would
        # mix users' scenes together.
        logger.error("no verified user identity — refusing to build scene tools")
        return []

    user_id = caller.sub  # closed over; never a tool parameter
    table = _table()

    # Whether this user has coordinates on file, which a solar trigger needs. The
    # email is the row key here, not the sub: the settings row is written by the
    # Admin Console and read by the orchestrator, and both key it by email.
    settings_key = (caller.email or caller.sub)

    def _has_location() -> bool:
        name = os.environ.get(SKILLS_TABLE_ENV, "")
        if not name:
            # No grant, no table name. Treated as "cannot confirm a location",
            # which refuses the solar scene — the alternative is saving one that
            # will never fire and saying it worked.
            logger.warning("no skills table configured; cannot confirm a location")
            return False
        try:
            import boto3

            item = boto3.resource(
                "dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"),
            ).Table(name).get_item(
                Key={"userId": settings_key, "skillName": "__settings__"}
            ).get("Item") or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read settings: %s", type(exc).__name__)
            return False
        return item.get("latitude") is not None and item.get("longitude") is not None

    from strands import tool as strands_tool

    @strands_tool
    def build_trigger(sceneType: str, conditionValue: str = "",
                      calculationType: str = "", executionType: str = "",
                      subject: str = "") -> str:
        """Validate a scene trigger and report what had to be inferred.

        Call this before create_scenario when you are unsure a trigger is
        well-formed — it returns the normalised trigger, a plain-language
        description of it, and the names of any fields it filled in for you, which
        you must pass on to the user.

        sceneType is 'time', 'device_state' or 'sensor'. A time needs
        conditionValue as 24-hour HH:MM. A device_state needs subject set to a
        device id. A sensor needs subject set to a metric (temperature, humidity,
        pm25, co2), a numeric conditionValue, and calculationType 'above' or
        'below' — there is no default for a threshold.
        """
        try:
            trigger, auto = sc.validate_trigger({
                "sceneType": sceneType, "conditionValue": conditionValue,
                "calculationType": calculationType,
                "executionType": executionType, "subject": subject,
            })
        except sc.ScenarioError as exc:
            return _err(str(exc))
        return _ok({"trigger": trigger,
                    "description": sc.describe_trigger(trigger),
                    "autoInferredFields": auto})

    @strands_tool
    def find_template(intent: str) -> str:
        """Look for a reusable scene template matching a described intent.

        Call this first for any create request. A template carries the details a
        user did not think to mention — which lights, what brightness — so
        reusing one produces a better scene than building from the request alone.
        Returns the closest few with their triggers and actions, or an empty list.
        """
        from boto3.dynamodb.conditions import Key

        try:
            resp = table.query(
                IndexName="TemplateIndex",
                KeyConditionExpression=Key("templateScope").eq(sc.TEMPLATE_SCOPE),
                ScanIndexForward=False,  # newest first
                Limit=TEMPLATE_SCAN_LIMIT,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("template query failed: %s", exc)
            # Soft failure: the agent can still build a scene from scratch, which
            # is worse than reusing a template but much better than refusing.
            return _ok({"templates": [],
                        "note": "the template library is unavailable; build the "
                                "scene from the request instead"})

        items = resp.get("Items", [])
        ranked = sorted(items, key=lambda t: _relevance(t, intent), reverse=True)
        best = [t for t in ranked if _relevance(t, intent) > 0][:TOP_TEMPLATES]
        return _ok({
            "templates": [{**sc.summarise(t),
                           "deviceActions": t.get("deviceActions") or []}
                          for t in best],
            "searched": len(items),
        })

    @strands_tool
    def create_scenario(name: str, sceneType: str, deviceActions: list,
                        conditionValue: str = "", calculationType: str = "",
                        executionType: str = "", subject: str = "",
                        description: str = "") -> str:
        """Save a scene: a trigger plus the device actions it applies.

        deviceActions is a list of {"deviceId": "...", "command": {...}} objects —
        the same command shape control_device takes, e.g.
        {"deviceId": "living-led-1", "command": {"action": "setPower", "power": false}}.

        Every action is validated against the device catalog before anything is
        stored, and out-of-range values are clamped: what comes back in
        `deviceActions` is what was saved, so report any clamping to the user.

        Returns `pendingActions` — the actions for the CALLER to apply now if the
        user wanted the scene to take effect immediately. This agent does not
        control devices; do not claim any device changed state.
        """
        if sceneType == sc.SCENE_SOLAR and not _has_location():
            # Checked here rather than in validate_trigger, which is a pure
            # function that never reads DynamoDB — and the user's coordinates live
            # on their settings row. Refused rather than defaulted: a guessed
            # location turns the lights on at the wrong time, in a way nobody
            # thinks to check.
            return _err(
                "a sunrise or sunset scene needs this user's location, and none is "
                "set. Ask an administrator to set the latitude and longitude on "
                "Identity in the Admin Console, or use a fixed clock time instead.")
        try:
            built = sc.build_scenario(
                user_id=user_id, name=name,
                trigger={"sceneType": sceneType, "conditionValue": conditionValue,
                         "calculationType": calculationType,
                         "executionType": executionType, "subject": subject},
                actions=deviceActions, description=description, now=_now())
        except sc.ScenarioError as exc:
            return _err(str(exc))

        item = built["item"]
        try:
            table.put_item(Item=item)
        except Exception as exc:  # noqa: BLE001
            logger.exception("could not store scenario")
            return _err(f"the scene could not be saved: {exc}")

        return _ok({
            "saved": sc.summarise(item),
            "deviceActions": item["deviceActions"],
            "pendingActions": sc.pending_actions(item),
            "autoInferredFields": built["autoInferredFields"],
            "warnings": built["warnings"],
            "note": "Saved. pendingActions are for the caller to execute; this "
                    "agent has not changed any device.",
        })

    @strands_tool
    def list_scenarios() -> str:
        """List the scenes saved for this user, with what triggers each one.

        Use this to answer "what automations do I have", and before updating a
        scene so you refer to it by its real id.
        """
        from boto3.dynamodb.conditions import Key

        try:
            resp = table.query(
                KeyConditionExpression=Key("userId").eq(user_id)
                & Key("scenarioKey").begins_with(sc.STRATEGY_PREFIX))
        except Exception as exc:  # noqa: BLE001
            logger.exception("could not list scenarios")
            return _err(f"the scene list could not be read: {exc}")
        items = resp.get("Items", [])
        return _ok({"scenarios": [sc.summarise(i) for i in items],
                    "count": len(items)})

    @strands_tool
    def run_scenario(scenarioId: str) -> str:
        """Fetch a saved scene's actions so the CALLER can apply them now.

        This is how a one-tap command runs: the user says "run my movie mode", you
        look it up and hand back its actions, and the orchestrator applies each one
        under the user's own identity.

        Returns `pendingActions` — the same shape create_scenario returns. This
        agent still does not control devices, so do not claim anything changed
        state; report the actions you are handing back.

        Works for any saved scene, not only manual ones: "run my sleep mode now"
        is a reasonable thing to ask of a scene that normally fires at 23:00.
        """
        key = {"userId": user_id, "scenarioKey": sc.strategy_key(scenarioId)}
        try:
            item = (table.get_item(Key=key).get("Item")) or {}
        except Exception as exc:  # noqa: BLE001
            logger.exception("could not read scenario")
            return _err(f"the scene could not be read: {exc}")
        if not item:
            return _err(
                f"no scene with id {scenarioId!r} — call list_scenarios to see "
                f"the ids this user has")
        actions = sc.pending_actions(item)
        if not actions:
            # A stored scene always has actions (validate_actions requires a
            # non-empty list), so this means the row is damaged. Say so rather than
            # reporting an empty success, which would read as "ran, did nothing".
            return _err(
                f"the scene {item.get('name', scenarioId)!r} has no device actions "
                f"stored, so there is nothing to run")
        return _ok({
            "scenario": sc.summarise(item),
            "pendingActions": actions,
            "isActive": bool(item.get("isActive")),
            "note": "pendingActions are for the caller to execute; this agent has "
                    "not changed any device. An inactive scene can still be run on "
                    "request — isActive only governs whether it fires by itself.",
        })

    @strands_tool
    def update_scenario(scenarioId: str, name: str = "", description: str = "",
                        isActive: str = "", sceneType: str = "",
                        conditionValue: str = "", calculationType: str = "",
                        executionType: str = "", subject: str = "",
                        deviceActions: list | None = None) -> str:
        """Change a saved scene: rename it, retime it, enable or disable it.

        scenarioId is the id from list_scenarios. Pass only what changes; anything
        omitted keeps its current value. isActive takes "true" or "false".

        Retiming means passing the trigger fields again — a trigger is validated as
        a whole, because a half-changed one (a sensor threshold with the old
        comparison) would be accepted and then behave unexpectedly.
        """
        from boto3.dynamodb.conditions import Key  # noqa: F401  (parity with above)

        key = {"userId": user_id, "scenarioKey": sc.strategy_key(scenarioId)}
        try:
            existing = (table.get_item(Key=key).get("Item")) or {}
        except Exception as exc:  # noqa: BLE001
            logger.exception("could not read scenario")
            return _err(f"the scene could not be read: {exc}")
        if not existing:
            return _err(
                f"no scene with id {scenarioId!r} — call list_scenarios to see "
                f"the ids this user has")

        item = dict(existing)
        if name.strip():
            item["name"] = name.strip()
        if description.strip():
            item["description"] = description.strip()
        if isActive:
            lowered = isActive.strip().lower()
            if lowered not in ("true", "false"):
                return _err("isActive must be \"true\" or \"false\"")
            item["isActive"] = lowered == "true"

        if sceneType == sc.SCENE_SOLAR and not _has_location():
            # Retiming an existing scene to sunset needs a location just as much as
            # creating one does. Missing this check would leave the one path that
            # can produce an unschedulable scene.
            return _err(
                "a sunrise or sunset scene needs this user's location, and none is "
                "set. Ask an administrator to set the latitude and longitude on "
                "Identity in the Admin Console, or use a fixed clock time instead.")

        if sceneType:
            try:
                trigger, auto = sc.validate_trigger({
                    "sceneType": sceneType, "conditionValue": conditionValue,
                    "calculationType": calculationType,
                    "executionType": executionType or
                    (existing.get("trigger") or {}).get("executionType", ""),
                    "subject": subject})
            except sc.ScenarioError as exc:
                return _err(str(exc))
            item["trigger"] = trigger
        else:
            auto = []

        warnings: list[str] = []
        if deviceActions:
            try:
                item["deviceActions"], warnings = sc.validate_actions(deviceActions)
            except sc.ScenarioError as exc:
                return _err(str(exc))

        item["updatedAt"] = _now()
        # The id and key are derived from the ORIGINAL name on purpose: renaming a
        # scene must not silently create a second row and leave the first one
        # still firing.
        item["scenarioKey"] = key["scenarioKey"]
        item["scenarioId"] = existing.get("scenarioId") or scenarioId
        try:
            table.put_item(Item=item)
        except Exception as exc:  # noqa: BLE001
            logger.exception("could not update scenario")
            return _err(f"the scene could not be updated: {exc}")

        return _ok({"updated": sc.summarise(item),
                    "autoInferredFields": auto,
                    "warnings": warnings,
                    "pendingActions": sc.pending_actions(item)})

    return [find_template, build_trigger, create_scenario, list_scenarios,
            update_scenario, run_scenario]
