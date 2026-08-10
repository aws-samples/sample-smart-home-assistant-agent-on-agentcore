"""Keep EventBridge Scheduler in step with the time-triggered scenes.

A scene is created at runtime by an agent, so its schedule cannot be declared in
CDK — something has to create one when a scene is saved and delete it when the
scene is removed or switched off. That is this module.

Only `time` triggers get a schedule of their own. A sensor threshold or a
device-state condition has no clock to fire on; those are evaluated by the
runner's recurring sweep, which CDK does declare.

Reconciliation rather than incremental updates: `sync_schedules` lists what
Scheduler currently holds, works out what the table says it should hold, and
fixes the difference. The incremental version has a failure mode this avoids — a
scene deleted while the admin API was erroring leaves a schedule behind, and that
orphan keeps firing a scene that no longer exists, forever, with nobody looking at
it. A reconcile makes that self-healing on the next call.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger()

GROUP = os.environ.get("SCENARIO_SCHEDULE_GROUP", "smarthome-scenarios")
RUNNER_ARN = os.environ.get("SCENARIO_RUNNER_ARN", "")
SCHEDULER_ROLE_ARN = os.environ.get("SCENARIO_SCHEDULER_ROLE_ARN", "")

# Schedule names are `scn-<user hash>-<scenario id>`. The user id is hashed
# because a Cognito sub is 36 characters and the name limit is 64, and because a
# schedule name is not the place to publish an identifier. Deterministic, so the
# same scene always maps to the same schedule and a re-save updates rather than
# duplicating.
NAME_PREFIX = "scn-"


def _user_tag(user_id: str) -> str:
    import hashlib

    return hashlib.sha256(user_id.encode()).hexdigest()[:12]


def schedule_name(user_id: str, scenario_id: str) -> str:
    # Scheduler accepts [0-9a-zA-Z-_.]; a scenario id is already a slug.
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in scenario_id)
    return f"{NAME_PREFIX}{_user_tag(user_id)}-{safe}"[:64]


def _client():
    import boto3

    return boto3.client("scheduler", region_name=os.environ.get("AWS_REGION",
                                                                "us-west-2"))


def _wanted(table) -> dict[str, dict]:
    """Schedule name -> the payload it should carry, for every active time scene.

    A scan, and bounded by the number of scenes in the deployment. Reconciling
    needs the whole set by definition, so a query per user would be more calls
    for the same data.
    """
    import scenarios as sc

    wanted: dict[str, dict] = {}
    kwargs: dict = {}
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            trigger = item.get("trigger") or {}
            if item.get("isTemplate") or not item.get("isActive"):
                continue
            cron = sc.cron_for(trigger)
            if not cron:
                continue  # a condition trigger; the sweep handles it
            name = schedule_name(item["userId"], item.get("scenarioId", ""))
            wanted[name] = {
                "cron": cron,
                "input": json.dumps({
                    "mode": "scenario",
                    "userId": item["userId"],
                    "scenarioKey": item["scenarioKey"],
                }),
                "description": f"{item.get('name', '')} "
                               f"({sc.describe_trigger(trigger)})"[:200],
            }
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return wanted


def _existing(client) -> set[str]:
    names: set[str] = set()
    token = None
    while True:
        kwargs = {"GroupName": GROUP, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        resp = client.list_schedules(**kwargs)
        for s in resp.get("Schedules", []):
            if s.get("Name", "").startswith(NAME_PREFIX):
                names.add(s["Name"])
        token = resp.get("NextToken")
        if not token:
            break
    return names


def sync_schedules(table) -> dict:
    """Reconcile Scheduler against the table. Returns a summary.

    Never raises: this is called after a scene is saved, and a scheduling failure
    must not make the save look like it failed. It is REPORTED, though — the
    caller surfaces it, because "saved but not scheduled" is precisely the state a
    user would otherwise discover at 23:00.
    """
    if not RUNNER_ARN or not SCHEDULER_ROLE_ARN:
        return {"synced": False,
                "reason": "scenario runner or scheduler role not configured"}

    try:
        client = _client()
        wanted = _wanted(table)
        existing = _existing(client)
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not read scheduling state")
        return {"synced": False, "reason": f"{type(exc).__name__}: {exc}"}

    created, updated, deleted, failed = [], [], [], []

    for name, spec in wanted.items():
        target = {
            "Arn": RUNNER_ARN,
            "RoleArn": SCHEDULER_ROLE_ARN,
            "Input": spec["input"],
        }
        kwargs = dict(
            Name=name, GroupName=GROUP,
            ScheduleExpression=spec["cron"],
            # The stored HH:MM is what the user said, and they meant it on their
            # own clock. Nothing in the schema records a timezone yet, so UTC is
            # the honest choice: a guessed offset would fire a scene at a time the
            # user never named, and be much harder to notice than a consistent
            # UTC one. Recording the caller's timezone is a follow-up.
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            Target=target,
            Description=spec["description"],
            State="ENABLED",
        )
        try:
            if name in existing:
                client.update_schedule(**kwargs)
                updated.append(name)
            else:
                client.create_schedule(**kwargs)
                created.append(name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not put schedule %s: %s", name, exc)
            failed.append({"name": name, "error": str(exc)[:200]})

    # Orphans: a schedule whose scene was deleted or deactivated. Left alone it
    # would keep invoking the runner for a row that is gone.
    for name in existing - set(wanted):
        try:
            client.delete_schedule(Name=name, GroupName=GROUP)
            deleted.append(name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not delete orphan schedule %s: %s", name, exc)
            failed.append({"name": name, "error": str(exc)[:200]})

    out = {"synced": not failed, "created": created, "updated": updated,
           "deleted": deleted}
    if failed:
        out["failed"] = failed
    logger.info("schedule sync: %s", json.dumps(out))
    return out
