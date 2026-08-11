"""Keep EventBridge Scheduler in step with the time-triggered scenes.

A scene is created at runtime by an agent, so its schedule cannot be declared in
CDK — something has to create one when a scene is saved and delete it when the
scene is removed or switched off. That is this module.

Which triggers get a schedule of their own:

  - `time` — one schedule, at the hour the user named, in the owner's timezone.
  - `solar` — one schedule, at today's sunrise/sunset for the owner's coordinates,
    always in UTC because a solar time is already an absolute instant. Recomputed
    here on every reconcile and nightly by the runner.
  - `device_state` / `sensor` — none. No clock to fire on; the runner's recurring
    sweep evaluates them against the current reading, and CDK declares that one.
  - `manual` — none. It runs when the user asks for it by name.

Reconciliation rather than incremental updates: `sync_schedules` lists what
Scheduler currently holds, works out what the table says it should hold, and
fixes the difference. The incremental version has a failure mode this avoids — a
scene deleted while the admin API was erroring leaves a schedule behind, and that
orphan keeps firing a scene that no longer exists, forever, with nobody looking at
it. A reconcile makes that self-healing on the next call.

That same reconcile is why solar times are computed HERE rather than only by the
runner's nightly pass. If this module could not name a solar scene's expression it
would see the runner's schedule in `existing`, not in `wanted`, and delete it as an
orphan — so every scene save would silently wipe the solar schedules until the next
midnight. Both processes deriving the same name and the same expression is what
makes the two idempotent with respect to each other.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os

logger = logging.getLogger()

GROUP = os.environ.get("SCENARIO_SCHEDULE_GROUP", "smarthome-scenarios")
RUNNER_ARN = os.environ.get("SCENARIO_RUNNER_ARN", "")
SCHEDULER_ROLE_ARN = os.environ.get("SCENARIO_SCHEDULER_ROLE_ARN", "")


def _shared():
    """`scenarios` and `solar`, which the naming and the maths come from."""
    import scenarios as sc
    import solar

    return sc, solar


def _cognito():
    """For resolving a scene owner's sub to the email their settings are under."""
    import boto3

    return boto3.client("cognito-idp",
                        region_name=os.environ.get("AWS_REGION", "us-west-2"))


# Re-exported so existing callers and tests keep working. The definition lives in
# shared/scenarios.py because the runner derives the same names — see the module
# docstring.
def schedule_name(user_id: str, scenario_id: str) -> str:
    import scenarios as sc

    return sc.schedule_name(user_id, scenario_id)


def _name_prefix() -> str:
    import scenarios as sc

    return sc.SCHEDULE_NAME_PREFIX


def _client():
    import boto3

    return boto3.client("scheduler", region_name=os.environ.get("AWS_REGION",
                                                                "us-west-2"))


def _settings_lookup(skills_table):
    """A per-user settings reader, memoised for one reconcile.

    `_wanted` scans every scene, and several scenes usually belong to the same
    user, so an uncached read here would be one GetItem per scene rather than one
    per user. Returns `{}` for a user with no settings row, which means UTC and no
    coordinates — the behaviour every schedule had before these fields existed.
    """
    cache: dict[str, tuple[dict, bool]] = {}

    def lookup(user_id: str) -> tuple[dict, bool]:
        """(settings, readable). `readable` is False only when the read RAISED.

        The two failures have to be distinguishable. A user who never set
        coordinates and a user whose settings row could not be read this second
        both produce `{}`, but the right response differs: the first means the
        solar schedule should not exist, the second means we do not know and must
        not delete the one that does.
        """
        if user_id in cache:
            return cache[user_id]
        settings: dict = {}
        readable = True
        if skills_table is not None:
            import user_settings

            try:
                # A scene row is keyed by Cognito sub; the settings row by email.
                # `read_settings` crosses that line — see shared/user_settings.py.
                settings = user_settings.read_settings(
                    skills_table, user_id, cognito=_cognito(),
                    user_pool_id=os.environ.get("COGNITO_USER_POOL_ID", ""))
            except Exception as exc:  # noqa: BLE001
                # Soft failure on purpose. A settings read that fails must not stop
                # the reconcile: a time schedule still gets created, in UTC, which
                # is where it was before timezones existed.
                readable = False
                logger.warning("could not read settings for %s...: %s",
                               user_id[:8], type(exc).__name__)
        cache[user_id] = (settings, readable)
        return cache[user_id]

    return lookup


def _coords(settings: dict) -> tuple[float, float] | None:
    lat, lon = settings.get("latitude"), settings.get("longitude")
    if lat is None or lon is None:
        return None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def _wanted(table, skills_table=None, now: _dt.datetime | None = None
            ) -> tuple[dict[str, dict], list[dict], set[str]]:
    """(schedule name -> payload, problems, undecided) for every active scene.

    A scan, and bounded by the number of scenes in the deployment. Reconciling
    needs the whole set by definition, so a query per user would be more calls
    for the same data.

    `skills_table` supplies each owner's timezone and coordinates. `now` is a
    parameter so the solar arithmetic is testable against a fixed date.

    `problems` names the scenes that should have a schedule and cannot get one —
    a solar scene whose owner has no coordinates, most likely. Returned rather than
    logged and forgotten: "saved but never fires" is the state a user discovers at
    sunset, so the caller surfaces it.

    `undecided` names schedules this reconcile could not form an opinion about,
    because the owner's settings could not be READ. They must be excluded from the
    orphan sweep: absent from `wanted` normally means "delete this", and a
    momentary DynamoDB error would otherwise delete a perfectly good schedule and
    report success.
    """
    sc, solar = _shared()

    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    settings_for = _settings_lookup(skills_table)
    wanted: dict[str, dict] = {}
    problems: list[dict] = []
    undecided: set[str] = set()
    kwargs: dict = {}
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            trigger = item.get("trigger") or {}
            if item.get("isTemplate") or not item.get("isActive"):
                continue
            if not sc.wants_schedule(trigger):
                continue  # a condition trigger (the sweep handles it) or manual
            user_id = item["userId"]
            settings, readable = settings_for(user_id)
            name = sc.schedule_name(user_id, item.get("scenarioId", ""))

            if trigger.get("sceneType") == sc.SCENE_SOLAR:
                place = _coords(settings)
                if place is None:
                    if not readable:
                        # We do not know whether they have coordinates. Leave any
                        # existing schedule alone and try again next reconcile.
                        undecided.add(name)
                        problems.append({
                            "name": name,
                            "error": "the owner's settings could not be read, so "
                                     "the solar time was left as it was",
                        })
                        continue
                    problems.append({
                        "name": name,
                        "error": "the owner has no coordinates, so a sunrise or "
                                 "sunset time cannot be computed",
                    })
                    continue
                when = solar.next_occurrence_utc(
                    trigger.get("subject", ""), place[0], place[1],
                    offset_minutes=sc.solar_offset_minutes(trigger), now=now)
                if when is None:
                    # Polar day or polar night. A real answer, not a failure.
                    problems.append({
                        "name": name,
                        "error": f"the sun does not {trigger.get('subject', '')} "
                                 f"at this location in the searched period",
                    })
                    continue
                cron = solar.cron_for_utc(when)
                # UTC, always. The solar time is already absolute; applying the
                # user's zone on top of it would shift it by their offset.
                timezone = "UTC"
            else:
                cron = sc.cron_for(trigger)
                timezone = str(settings.get("timezone") or "")

            wanted[name] = {
                "cron": cron,
                "timezone": timezone,
                "solar": trigger.get("sceneType") == sc.SCENE_SOLAR,
                "userId": user_id,
                "scenarioKey": item["scenarioKey"],
                "input": json.dumps({
                    "mode": "scenario",
                    "userId": user_id,
                    "scenarioKey": item["scenarioKey"],
                }),
                "description": f"{item.get('name', '')} "
                               f"({sc.describe_trigger(trigger)})"[:200],
            }
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return wanted, problems, undecided


def _existing(client) -> set[str]:
    names: set[str] = set()
    token = None
    while True:
        kwargs = {"GroupName": GROUP, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        resp = client.list_schedules(**kwargs)
        prefix = _name_prefix()
        for s in resp.get("Schedules", []):
            if s.get("Name", "").startswith(prefix):
                names.add(s["Name"])
        token = resp.get("NextToken")
        if not token:
            break
    return names


def sync_schedules(table, skills_table=None,
                   now: _dt.datetime | None = None) -> dict:
    """Reconcile Scheduler against the table. Returns a summary.

    Never raises: this is called after a scene is saved, and a scheduling failure
    must not make the save look like it failed. It is REPORTED, though — the
    caller surfaces it, because "saved but not scheduled" is precisely the state a
    user would otherwise discover at 23:00.

    `skills_table` is where per-user timezones and coordinates live. Omitting it
    schedules time scenes in UTC and leaves solar scenes unschedulable.
    """
    if not RUNNER_ARN or not SCHEDULER_ROLE_ARN:
        return {"synced": False,
                "reason": "scenario runner or scheduler role not configured"}

    try:
        client = _client()
        wanted, problems, undecided = _wanted(table, skills_table, now=now)
        existing = _existing(client)
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not read scheduling state")
        return {"synced": False, "reason": f"{type(exc).__name__}: {exc}"}

    created, updated, deleted = [], [], []
    # Seeded with what `_wanted` could not schedule, so an un-schedulable solar
    # scene shows up in the same place as an API failure. It is the same problem
    # from the user's side: the scene will not fire.
    failed = list(problems)

    import scenario_schedules_shared as shared

    for name, spec in wanted.items():
        if spec.get("solar"):
            # Built by the shared helper, byte for byte what the runner's nightly
            # pass writes. If these two disagreed on any field they would each
            # "correct" the other and the scene would fire correctly on alternate
            # days only.
            kwargs = shared.solar_schedule_kwargs(
                name=name, cron=spec["cron"], user_id=spec["userId"],
                scenario_key=spec["scenarioKey"], group=GROUP,
                runner_arn=RUNNER_ARN, role_arn=SCHEDULER_ROLE_ARN,
                description=spec["description"])
        else:
            kwargs = dict(
                Name=name, GroupName=GROUP,
                ScheduleExpression=spec["cron"],
                # The stored HH:MM is what the user said, and they meant it on
                # their own clock — so the owner's IANA timezone is handed to
                # Scheduler and the cron stays exactly as the user named it.
                #
                # Converting to a UTC cron ourselves was the other option and it is
                # wrong twice over: it breaks at every DST transition (a 07:30
                # scene would drift to 06:30 for half the year), and it would put a
                # timezone-dependent value in `cron_for`, which is a pure function
                # used by three callers that do not all have the user's settings to
                # hand.
                #
                # A user who has set no timezone gets "UTC", which is what every
                # schedule did before this field was populated — so nothing moves
                # for them.
                ScheduleExpressionTimezone=spec.get("timezone") or "UTC",
                FlexibleTimeWindow={"Mode": "OFF"},
                Target={
                    "Arn": RUNNER_ARN,
                    "RoleArn": SCHEDULER_ROLE_ARN,
                    "Input": spec["input"],
                },
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
    #
    # `undecided` is subtracted: those are scenes whose owner's settings could not
    # be read this pass, so "not in wanted" does not mean "should not exist". A
    # transient read error must not silently delete a working schedule.
    for name in existing - set(wanted) - undecided:
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
