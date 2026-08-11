"""Writing a solar scene's schedule — the one definition, used by two processes.

Two things create a solar schedule, and they must create the SAME one:

  - the admin API, when a scene is saved (it reconciles the whole set, so it has
    to be able to name a solar schedule or it would delete one as an orphan)
  - the runner's nightly pass, which moves each solar schedule to tomorrow's
    sunrise or sunset

If the two disagreed on any field, they would fight: each pass would "correct" the
other, and the visible symptom would be a scene that fires at the right time only
on alternate days. So the payload is built here once.

Deliberately not the whole reconcile — only the put. The admin API owns
reconciliation (it is the process that knows the full set of scenes); the runner
only ever updates schedules it already knows exist.
"""

from __future__ import annotations

import json


def solar_schedule_kwargs(*, name: str, cron: str, user_id: str,
                          scenario_key: str, group: str, runner_arn: str,
                          role_arn: str, description: str = "") -> dict:
    """The create_schedule / update_schedule kwargs for a solar scene.

    `ScheduleExpressionTimezone` is UTC and not the owner's zone. A sunrise time is
    already an absolute instant — it was computed FROM their coordinates — so
    handing Scheduler a local zone as well would apply the offset twice and fire
    the scene hours off. This is the opposite of the `time` trigger, where the
    stored value is a local wall-clock time and the zone is essential.
    """
    return dict(
        Name=name,
        GroupName=group,
        ScheduleExpression=cron,
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={
            "Arn": runner_arn,
            "RoleArn": role_arn,
            # The same payload a time-triggered scene sends. The runner does not
            # need to know which kind of trigger woke it — it loads the scene and
            # applies the actions either way.
            "Input": json.dumps({
                "mode": "scenario",
                "userId": user_id,
                "scenarioKey": scenario_key,
            }),
        },
        Description=description,
        State="ENABLED",
    )


def put_solar_schedule(scheduler, *, name: str, cron: str, user_id: str,
                       scenario_key: str, description: str = "",
                       group: str = "", runner_arn: str = "",
                       role_arn: str = "") -> str:
    """Create or update the schedule. Returns "created" or "updated".

    Update first, create on ResourceNotFound: the nightly pass updates far more
    often than it creates, and a list-then-decide would be an extra API call per
    scene to learn something the write already tells us.
    """
    import os

    group = group or os.environ.get("SCENARIO_SCHEDULE_GROUP", "smarthome-scenarios")
    runner_arn = runner_arn or os.environ.get("SCENARIO_RUNNER_ARN", "")
    role_arn = role_arn or os.environ.get("SCENARIO_SCHEDULER_ROLE_ARN", "")
    if not runner_arn or not role_arn:
        raise ValueError(
            "SCENARIO_RUNNER_ARN and SCENARIO_SCHEDULER_ROLE_ARN are required to "
            "write a schedule")

    kwargs = solar_schedule_kwargs(
        name=name, cron=cron, user_id=user_id, scenario_key=scenario_key,
        group=group, runner_arn=runner_arn, role_arn=role_arn,
        description=description)
    try:
        scheduler.update_schedule(**kwargs)
        return "updated"
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code != "ResourceNotFoundException":
            raise
        scheduler.create_schedule(**kwargs)
        return "created"
