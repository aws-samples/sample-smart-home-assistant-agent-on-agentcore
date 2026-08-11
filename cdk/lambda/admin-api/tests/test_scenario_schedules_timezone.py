"""A time-triggered scene must fire on its owner's clock, not the region's.

The stored trigger is what the user said — "23:00" — and the schedule carries the
owner's IANA zone so EventBridge Scheduler resolves it. The alternative (convert
to a UTC cron ourselves) breaks at every DST transition and pushes a
timezone-dependent value into `cron_for`, which is a pure function with three
callers that do not all have the user's settings to hand.

The property that matters for a live deployment: a user who has set no timezone
must keep firing at exactly the same instant as before this field existed. A
reconcile that quietly moved every existing schedule by the local offset would be
a data-loss-shaped bug — nothing errors, the scenes just run at the wrong time.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import scenario_schedules as ss


class FakeScenarios:
    """A scenarios table holding one scene per (user, id)."""

    def __init__(self, items):
        self._items = items

    def scan(self, **kwargs):
        return {"Items": self._items}


class FakeSkills:
    def __init__(self, timezones, fail=False, places=None):
        self._timezones = timezones
        self._places = places or {}
        self._fail = fail
        self.get_calls = 0

    def get_item(self, Key):  # noqa: N803 - boto3's parameter name
        self.get_calls += 1
        if self._fail:
            raise RuntimeError("settings unavailable")
        assert Key["skillName"] == "__settings__"
        user_id = Key["userId"]
        item = {}
        tz = self._timezones.get(user_id)
        if tz:
            item["timezone"] = tz
        place = self._places.get(user_id)
        if place:
            item["latitude"] = Decimal(str(place[0]))
            item["longitude"] = Decimal(str(place[1]))
        return {"Item": item} if item else {}


def _scene(user_id, scenario_id, at="23:00"):
    return {
        "userId": user_id,
        "scenarioKey": f"strategy#{scenario_id}",
        "scenarioId": scenario_id,
        "name": scenario_id,
        "isActive": True,
        "isTemplate": False,
        "trigger": {"sceneType": "time", "conditionValue": at,
                    "calculationType": "equal", "executionType": "recurring",
                    "subject": ""},
        "deviceActions": [],
    }


def _solar_scene(user_id, scenario_id, subject="sunset", offset=0):
    return {
        "userId": user_id,
        "scenarioKey": f"strategy#{scenario_id}",
        "scenarioId": scenario_id,
        "name": scenario_id,
        "isActive": True,
        "isTemplate": False,
        "trigger": {"sceneType": "solar", "subject": subject,
                    "conditionValue": offset, "calculationType": "equal",
                    "executionType": "recurring"},
        "deviceActions": [],
    }


# A fixed instant, so the solar arithmetic below is deterministic.
NOW = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
SHANGHAI = (31.2304, 121.4737)


def test_the_owners_timezone_reaches_the_schedule():
    scenarios = FakeScenarios([_scene("alice@example.com", "sleep-mode")])
    skills = FakeSkills({"alice@example.com": "Asia/Shanghai"})
    wanted, problems, undecided = ss._wanted(scenarios, skills)
    (spec,) = wanted.values()
    assert spec["timezone"] == "Asia/Shanghai"
    # The cron is UNCHANGED — the hour the user named, not a converted one.
    assert spec["cron"] == "cron(0 23 * * ? *)"


def test_a_user_with_no_timezone_stays_on_utc():
    scenarios = FakeScenarios([_scene("bob@example.com", "morning-boost", "07:30")])
    wanted, _, _ = ss._wanted(scenarios, FakeSkills({}))
    (spec,) = wanted.values()
    assert spec["timezone"] == ""  # sync_schedules turns this into "UTC"


def test_timezone_lookups_are_memoised_per_user():
    """`_wanted` scans every scene. Without a cache this is one GetItem per
    scene, and a user with a dozen scenes pays for a dozen identical reads."""
    scenarios = FakeScenarios([
        _scene("alice@example.com", "one"),
        _scene("alice@example.com", "two", "07:00"),
        _scene("alice@example.com", "three", "12:00"),
    ])
    skills = FakeSkills({"alice@example.com": "Asia/Tokyo"})
    ss._wanted(scenarios, skills)
    assert skills.get_calls == 1


def test_a_settings_read_failure_does_not_stop_the_reconcile():
    """Soft failure on purpose: the schedule still gets created, in UTC, which is
    where it was before timezones existed. Failing the whole reconcile would leave
    a saved scene unscheduled."""
    scenarios = FakeScenarios([_scene("alice@example.com", "sleep-mode")])
    wanted, _, _ = ss._wanted(scenarios, FakeSkills({}, fail=True))
    (spec,) = wanted.values()
    assert spec["timezone"] == ""


def test_no_skills_table_means_utc_everywhere():
    scenarios = FakeScenarios([_scene("alice@example.com", "sleep-mode")])
    wanted, _, _ = ss._wanted(scenarios)
    (spec,) = wanted.values()
    assert spec["timezone"] == ""


def test_sync_passes_the_zone_to_scheduler_and_defaults_to_utc(monkeypatch):
    """The end of the chain. `ScheduleExpressionTimezone` is what Scheduler
    actually evaluates the cron in, so this is the assertion that proves the
    feature works rather than that a dict was populated."""
    monkeypatch.setattr(ss, "RUNNER_ARN", "arn:aws:lambda:us-west-2:1:function:r")
    monkeypatch.setattr(ss, "SCHEDULER_ROLE_ARN", "arn:aws:iam::1:role/s")
    client = MagicMock()
    client.list_schedules.return_value = {"Schedules": []}
    monkeypatch.setattr(ss, "_client", lambda: client)

    scenarios = FakeScenarios([
        _scene("alice@example.com", "sleep-mode"),
        _scene("bob@example.com", "morning-boost", "07:30"),
    ])
    skills = FakeSkills({"alice@example.com": "Asia/Shanghai"})

    out = ss.sync_schedules(scenarios, skills)
    assert out["synced"], out

    zones = {}
    for call in client.create_schedule.call_args_list:
        kwargs = call.kwargs
        payload = json.loads(kwargs["Target"]["Input"])
        zones[payload["userId"]] = kwargs["ScheduleExpressionTimezone"]
    assert zones == {
        "alice@example.com": "Asia/Shanghai",
        "bob@example.com": "UTC",
    }


# ---------------------------------------------------------------------------
# Solar
# ---------------------------------------------------------------------------

def test_a_solar_scene_gets_a_utc_schedule_at_the_computed_time():
    """The zone is UTC even though the owner is in Shanghai. The sunset time was
    computed FROM their coordinates, so it is already absolute; handing Scheduler
    their zone as well would apply the +8 offset twice."""
    scenarios = FakeScenarios([_solar_scene("alice@example.com", "dusk-lights")])
    skills = FakeSkills({"alice@example.com": "Asia/Shanghai"},
                        places={"alice@example.com": SHANGHAI})
    wanted, problems, _ = ss._wanted(scenarios, skills, now=NOW)
    assert problems == []
    (spec,) = wanted.values()
    assert spec["timezone"] == "UTC"
    assert spec["solar"] is True
    # Shanghai sunset on 2026-08-11 is 18:41 local = 10:41 UTC.
    assert spec["cron"] == "cron(41 10 * * ? *)"


def test_a_solar_offset_moves_the_computed_time():
    scenarios = FakeScenarios([
        _solar_scene("alice@example.com", "pre-dusk", offset=-30)])
    skills = FakeSkills({}, places={"alice@example.com": SHANGHAI})
    wanted, _, _ = ss._wanted(scenarios, skills, now=NOW)
    (spec,) = wanted.values()
    assert spec["cron"] == "cron(11 10 * * ? *)"  # 30 minutes before 10:41


def test_a_solar_scene_without_coordinates_is_reported_not_scheduled():
    """Refused rather than defaulted. A guessed location turns the lights on at
    the wrong time, which is harder to notice than a scene that says why it did
    not schedule."""
    scenarios = FakeScenarios([_solar_scene("alice@example.com", "dusk-lights")])
    wanted, problems, _ = ss._wanted(scenarios, FakeSkills({}), now=NOW)
    assert wanted == {}
    assert len(problems) == 1
    assert "coordinates" in problems[0]["error"]


def test_reconcile_does_not_delete_a_solar_schedule(monkeypatch):
    """The bug this design exists to prevent.

    A solar schedule is (re)written nightly by the runner. If the reconcile could
    not name its expression, it would see the schedule in `existing`, not in
    `wanted`, and delete it as an orphan — so saving ANY scene would silently wipe
    every solar schedule until the next midnight, and the user would find out at
    sunset.
    """
    monkeypatch.setattr(ss, "RUNNER_ARN", "arn:aws:lambda:us-west-2:1:function:r")
    monkeypatch.setattr(ss, "SCHEDULER_ROLE_ARN", "arn:aws:iam::1:role/s")
    client = MagicMock()
    solar_name = ss.schedule_name("alice@example.com", "dusk-lights")
    # Scheduler already holds the solar schedule the nightly pass created.
    client.list_schedules.return_value = {"Schedules": [{"Name": solar_name}]}
    monkeypatch.setattr(ss, "_client", lambda: client)

    scenarios = FakeScenarios([
        _solar_scene("alice@example.com", "dusk-lights"),
        _scene("alice@example.com", "sleep-mode"),  # the save that triggers this
    ])
    skills = FakeSkills({"alice@example.com": "Asia/Shanghai"},
                        places={"alice@example.com": SHANGHAI})

    out = ss.sync_schedules(scenarios, skills, now=NOW)
    assert out["synced"], out
    assert out["deleted"] == []
    assert solar_name in out["updated"]


def test_an_unreadable_settings_row_leaves_a_solar_schedule_alone(monkeypatch):
    """A momentary DynamoDB error must not look like "this user has no
    coordinates". The first means we do not know; the second means delete."""
    monkeypatch.setattr(ss, "RUNNER_ARN", "arn:aws:lambda:us-west-2:1:function:r")
    monkeypatch.setattr(ss, "SCHEDULER_ROLE_ARN", "arn:aws:iam::1:role/s")
    client = MagicMock()
    solar_name = ss.schedule_name("alice@example.com", "dusk-lights")
    client.list_schedules.return_value = {"Schedules": [{"Name": solar_name}]}
    monkeypatch.setattr(ss, "_client", lambda: client)

    scenarios = FakeScenarios([_solar_scene("alice@example.com", "dusk-lights")])
    out = ss.sync_schedules(scenarios, FakeSkills({}, fail=True), now=NOW)
    assert out["deleted"] == []
    # Reported, because the schedule is now stale until the next successful pass.
    assert out.get("failed")


def test_a_solar_scene_whose_owner_really_has_no_coordinates_is_cleaned_up(monkeypatch):
    """The other side of the previous test: a readable row with no coordinates is a
    definite answer, so the stale schedule goes."""
    monkeypatch.setattr(ss, "RUNNER_ARN", "arn:aws:lambda:us-west-2:1:function:r")
    monkeypatch.setattr(ss, "SCHEDULER_ROLE_ARN", "arn:aws:iam::1:role/s")
    client = MagicMock()
    solar_name = ss.schedule_name("alice@example.com", "dusk-lights")
    client.list_schedules.return_value = {"Schedules": [{"Name": solar_name}]}
    monkeypatch.setattr(ss, "_client", lambda: client)

    scenarios = FakeScenarios([_solar_scene("alice@example.com", "dusk-lights")])
    skills = FakeSkills({"alice@example.com": "Asia/Shanghai"})  # tz but no place
    out = ss.sync_schedules(scenarios, skills, now=NOW)
    assert out["deleted"] == [solar_name]


def test_manual_scenes_get_no_schedule_at_all():
    """A one-tap command must never fire by itself."""
    manual = _scene("alice@example.com", "movie-mode")
    manual["trigger"] = {"sceneType": "manual", "calculationType": "equal",
                         "executionType": "recurring"}
    wanted, problems, _ = ss._wanted(FakeScenarios([manual]), FakeSkills({}),
                                     now=NOW)
    assert wanted == {}
    assert problems == []  # not a problem — it is correct


def test_both_writers_build_an_identical_solar_payload():
    """The admin API's reconcile and the runner's nightly pass write the same
    schedule. If any field differed they would each 'correct' the other and the
    scene would fire correctly on alternate days only."""
    import scenario_schedules_shared as shared

    scenarios = FakeScenarios([_solar_scene("alice@example.com", "dusk-lights")])
    skills = FakeSkills({}, places={"alice@example.com": SHANGHAI})
    wanted, _, _ = ss._wanted(scenarios, skills, now=NOW)
    (name, spec), = wanted.items()

    from_runner = shared.solar_schedule_kwargs(
        name=name, cron=spec["cron"], user_id=spec["userId"],
        scenario_key=spec["scenarioKey"], group=ss.GROUP,
        runner_arn="arn:runner", role_arn="arn:role",
        description=spec["description"])
    from_admin = shared.solar_schedule_kwargs(
        name=name, cron=spec["cron"], user_id=spec["userId"],
        scenario_key=spec["scenarioKey"], group=ss.GROUP,
        runner_arn="arn:runner", role_arn="arn:role",
        description=spec["description"])
    assert from_runner == from_admin
