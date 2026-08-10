"""Tests for the scheduled-scenario runner.

The whole reason this Lambda goes through the Gateway instead of calling
`iot-control` is governance: an administrator who revokes a user's
`control_device` in Tool Policy must stop that user's 07:30 automation too, not
just their chat commands. Cedar enforces that by not LISTING a tool the user may
not use — so the absence of `control_device` in `tools/list` is the authorization
answer, and treating it as a transient error to retry past would quietly reopen
the hole this design closes.

The other theme is failing closed. Acting as an absent user needs a stored
credential; every way that can go wrong (no secret, revoked refresh token, gateway
rejection) must end with no device commands sent.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# `index` is every Lambda's entry-module name, so it is loaded by path in
# conftest.py under a unique one. Importing the bare name here would get whichever
# Lambda pytest happened to load first.
import scenario_runner_index as index  # noqa: E402
import scenarios as sc  # noqa: E402

USER = "88c1a3e0-b041-707c-01ae-3ac4d821adbd"
CONTROL = "SmartHomeDeviceControl___control_device"


def _scene(scene_type="time", **trigger_extra):
    trigger = {"sceneType": scene_type, "conditionValue": "23:00"}
    trigger.update(trigger_extra)
    norm, _ = sc.validate_trigger(trigger)
    return {
        "userId": USER,
        "scenarioKey": "strategy#sleep-mode",
        "scenarioId": "sleep-mode",
        "name": "Sleep Mode",
        "isActive": True,
        "isTemplate": False,
        "trigger": norm,
        "deviceActions": [
            {"deviceId": "living-led-1", "deviceType": "led_matrix",
             "command": {"action": "setPower", "power": False}},
        ],
    }


class FakeGateway:
    """Records the calls a run makes, and can be told to fail one."""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result if result is not None else {"result": {"ok": True}}
        self.raises = raises

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if self.raises:
            raise self.raises
        return self.result


# ---------------------------------------------------------------------------
# Authorization — the point of the design
# ---------------------------------------------------------------------------

def test_a_user_without_control_device_gets_no_schedule_run():
    """Cedar hides a tool the user is not permitted, so its absence means "not
    authorised". If this ever became "retry" or "call iot-control directly",
    revoking control_device would stop chat commands and not automations."""
    gw = MagicMock()
    gw.initialize.return_value = None
    gw.tool_names.return_value = ["SmartHomeDeviceState___query_device_state"]
    with patch.object(index, "_id_token_for", return_value="id-token"), \
         patch.object(index, "GatewayCall", return_value=gw), \
         patch.object(index, "GATEWAY_URL", "https://gw.example/mcp"):
        session, control, reason = index._gateway_for(USER)
    assert session is None
    assert control == ""
    assert "control_device is not permitted" in reason


def test_the_control_tool_is_matched_by_suffix():
    """The Gateway prefixes a tool with its target name, so an exact-match lookup
    would find nothing and read as "not authorised" for every user."""
    gw = MagicMock()
    gw.tool_names.return_value = ["SomethingElse___discover_devices", CONTROL]
    with patch.object(index, "_id_token_for", return_value="t"), \
         patch.object(index, "GatewayCall", return_value=gw), \
         patch.object(index, "GATEWAY_URL", "https://gw.example/mcp"):
        session, control, reason = index._gateway_for(USER)
    assert session is gw
    assert control == CONTROL
    assert reason == ""


def test_no_stored_credential_means_no_run():
    with patch.object(index, "_id_token_for", return_value=""), \
         patch.object(index, "GATEWAY_URL", "https://gw.example/mcp"):
        session, _, reason = index._gateway_for(USER)
    assert session is None
    assert "credential" in reason


def test_a_gateway_rejection_is_reported_not_retried_past():
    import urllib.error

    gw = MagicMock()
    gw.initialize.side_effect = urllib.error.HTTPError(
        "u", 401, "Unauthorized", {}, None)
    with patch.object(index, "_id_token_for", return_value="stale"), \
         patch.object(index, "GatewayCall", return_value=gw), \
         patch.object(index, "GATEWAY_URL", "https://gw.example/mcp"):
        session, _, reason = index._gateway_for(USER)
    assert session is None
    assert "401" in reason


def test_no_gateway_url_configured_means_no_run():
    with patch.object(index, "GATEWAY_URL", ""):
        session, _, reason = index._gateway_for(USER)
    assert session is None
    assert "gateway url" in reason


# ---------------------------------------------------------------------------
# The credential
# ---------------------------------------------------------------------------

def test_the_refresh_token_is_read_from_a_per_user_secret():
    """Per-user, so one user's credential can be revoked without touching anyone
    else's and the IAM grant can be prefix-scoped."""
    assert index.secret_name_for(USER).endswith(USER)
    assert index.secret_name_for(USER).startswith("smarthome/scenario-tokens/")


def test_a_json_wrapped_or_bare_refresh_token_both_work():
    cognito = MagicMock()
    cognito.initiate_auth.return_value = {"AuthenticationResult": {"IdToken": "ID"}}
    for stored in (json.dumps({"refreshToken": "RT"}), "RT"):
        secrets = MagicMock()
        secrets.get_secret_value.return_value = {"SecretString": stored}

        def fake_client(service, _s=secrets, _c=cognito):
            return _s if service == "secretsmanager" else _c

        with patch.object(index, "_client", side_effect=fake_client):
            assert index._id_token_for(USER) == "ID"
        assert cognito.initiate_auth.call_args.kwargs["AuthParameters"] == {
            "REFRESH_TOKEN": "RT"}


def test_a_revoked_refresh_token_yields_no_token_rather_than_raising():
    """A user who has left should stop having automation run in their name, and
    that must not take the whole sweep down with an exception."""
    secrets = MagicMock()
    secrets.get_secret_value.return_value = {"SecretString": '{"refreshToken":"RT"}'}
    cognito = MagicMock()
    cognito.initiate_auth.side_effect = Exception("NotAuthorizedException")

    with patch.object(index, "_client",
                      side_effect=lambda s: secrets if s == "secretsmanager" else cognito):
        assert index._id_token_for(USER) == ""


def test_a_missing_secret_yields_no_token(caplog):
    secrets = MagicMock()
    secrets.get_secret_value.side_effect = Exception("ResourceNotFoundException")
    with patch.object(index, "_client", return_value=secrets):
        assert index._id_token_for(USER) == ""


def test_the_credential_is_never_logged(caplog):
    """A refresh token in CloudWatch is a 30-day credential in CloudWatch."""
    import logging

    secrets = MagicMock()
    secrets.get_secret_value.return_value = {
        "SecretString": '{"refreshToken":"SUPERSECRETVALUE"}'}
    cognito = MagicMock()
    cognito.initiate_auth.side_effect = Exception("boom SUPERSECRETVALUE echoed")

    with caplog.at_level(logging.DEBUG), \
         patch.object(index, "_client",
                      side_effect=lambda s: secrets if s == "secretsmanager" else cognito):
        index._id_token_for(USER)
    assert "SUPERSECRETVALUE" not in caplog.text


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def test_every_action_carries_the_owners_user_id():
    """Injected from the row's owner, never from the scene body — otherwise a
    scene could be written that acts on another user's devices."""
    gw = FakeGateway()
    report = index.run_scenario(_scene(), gw, CONTROL)
    assert len(gw.calls) == 1
    name, args = gw.calls[0]
    assert name == CONTROL
    assert args["user_id"] == USER
    assert args["device_id"] == "living-led-1"
    assert report["actions"][0]["ok"] is True


def test_a_decimal_command_value_is_sent_as_a_number():
    """Values come back from DynamoDB as Decimal, which json.dumps refuses — the
    request would fail to serialise and the action would silently not run."""
    from decimal import Decimal

    scene = _scene()
    scene["deviceActions"] = [{"deviceId": "living-fan-1", "deviceType": "fan",
                               "command": {"action": "setSpeed",
                                           "speed": Decimal("2")}}]
    gw = FakeGateway()
    index.run_scenario(scene, gw, CONTROL)
    json.dumps(gw.calls[0][1])  # would raise on a Decimal
    assert gw.calls[0][1]["command"]["speed"] == 2


def test_a_denied_action_is_reported_as_failed():
    gw = FakeGateway(result={"error": {"message": "access denied by policy"}})
    report = index.run_scenario(_scene(), gw, CONTROL)
    assert report["actions"][0]["ok"] is False


def test_a_403_is_recorded_rather_than_crashing_the_run():
    """Cedar refusing one action is the system working; the run reports it."""
    import urllib.error

    gw = FakeGateway(raises=urllib.error.HTTPError("u", 403, "Forbidden", {}, None))
    report = index.run_scenario(_scene(), gw, CONTROL)
    assert report["actions"][0]["ok"] is False
    assert "403" in report["actions"][0]["detail"]


def test_one_failing_action_does_not_stop_the_others():
    scene = _scene()
    scene["deviceActions"].append(
        {"deviceId": "living-fan-1", "deviceType": "fan",
         "command": {"action": "setSpeed", "speed": 1}})

    class Flaky(FakeGateway):
        def call(self, name, arguments):
            self.calls.append((name, arguments))
            if len(self.calls) == 1:
                raise RuntimeError("transient")
            return {"result": {"ok": True}}

    report = index.run_scenario(scene, Flaky(), CONTROL)
    assert [a["ok"] for a in report["actions"]] == [False, True]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def test_an_inactive_scene_is_not_run():
    scene = _scene()
    scene["isActive"] = False
    with patch.object(index, "_load", return_value=scene), \
         patch.object(index, "_gateway_for") as gw_for:
        out = index.handler({"mode": "scenario", "userId": USER,
                             "scenarioKey": scene["scenarioKey"]}, None)
    assert out["ran"] == 0
    gw_for.assert_not_called()


def test_a_deleted_scene_is_not_an_error():
    """A schedule can outlive its scene by moments; the runner should shrug."""
    with patch.object(index, "_load", return_value=None):
        out = index.handler({"mode": "scenario", "userId": USER,
                             "scenarioKey": "strategy#gone"}, None)
    assert out["ran"] == 0
    assert out["reason"] == "not found"


def test_a_once_only_scene_deactivates_itself():
    """Otherwise "at 08:00 tomorrow" fires every day forever."""
    scene = _scene(executionType="once")
    table = MagicMock()
    with patch.object(index, "_load", return_value=scene), \
         patch.object(index, "_gateway_for",
                      return_value=(FakeGateway(), CONTROL, "")), \
         patch.object(index, "_record_run"), \
         patch.object(index, "_table", return_value=table):
        index.handler({"mode": "scenario", "userId": USER,
                       "scenarioKey": scene["scenarioKey"]}, None)
    assert table.update_item.called
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["ExpressionAttributeValues"][":f"] is False


def test_a_recurring_scene_stays_active():
    scene = _scene(executionType="recurring")
    table = MagicMock()
    with patch.object(index, "_load", return_value=scene), \
         patch.object(index, "_gateway_for",
                      return_value=(FakeGateway(), CONTROL, "")), \
         patch.object(index, "_record_run"), \
         patch.object(index, "_table", return_value=table):
        index.handler({"mode": "scenario", "userId": USER,
                       "scenarioKey": scene["scenarioKey"]}, None)
    assert not table.update_item.called


# ---------------------------------------------------------------------------
# The sweep — edge, not level
# ---------------------------------------------------------------------------

def _sensor_scene(last=None):
    trigger, _ = sc.validate_trigger(
        {"sceneType": "sensor", "subject": "temperature",
         "conditionValue": 27, "calculationType": "above"})
    scene = _scene()
    scene["trigger"] = trigger
    scene["scenarioKey"] = "strategy#cool-down"
    if last is not None:
        scene["lastReading"] = last
    return scene


def test_a_threshold_fires_once_on_the_crossing_not_every_sweep():
    """The sweep runs every five minutes. Without edge detection a "temperature
    above 27" scene would re-run all afternoon."""
    already_hot = _sensor_scene(last=28)
    with patch.object(index, "_all_condition_scenarios", return_value=[already_hot]), \
         patch.object(index, "_reading_for", return_value=29), \
         patch.object(index, "_gateway_for") as gw_for:
        out = index.handler({"mode": "sweep"}, None)
    assert out["ran"] == 0
    gw_for.assert_not_called()


def test_a_threshold_fires_when_it_was_previously_below():
    fresh = _sensor_scene(last=22)
    with patch.object(index, "_all_condition_scenarios", return_value=[fresh]), \
         patch.object(index, "_reading_for", return_value=28), \
         patch.object(index, "_gateway_for",
                      return_value=(FakeGateway(), CONTROL, "")), \
         patch.object(index, "_record_run"):
        out = index.handler({"mode": "sweep"}, None)
    assert out["ran"] == 1


def test_a_threshold_with_no_history_fires_on_its_first_true_reading():
    with patch.object(index, "_all_condition_scenarios",
                      return_value=[_sensor_scene()]), \
         patch.object(index, "_reading_for", return_value=28), \
         patch.object(index, "_gateway_for",
                      return_value=(FakeGateway(), CONTROL, "")), \
         patch.object(index, "_record_run"):
        assert index.handler({"mode": "sweep"}, None)["ran"] == 1


def test_a_sweep_does_not_fire_a_scene_whose_condition_is_false():
    with patch.object(index, "_all_condition_scenarios",
                      return_value=[_sensor_scene(last=22)]), \
         patch.object(index, "_reading_for", return_value=20), \
         patch.object(index, "_gateway_for") as gw_for:
        assert index.handler({"mode": "sweep"}, None)["ran"] == 0
    gw_for.assert_not_called()


def test_one_unauthorised_user_does_not_stop_the_sweep():
    """A sweep spans users. One missing credential must not silence everyone."""
    a = _sensor_scene(last=20)
    b = _sensor_scene(last=20)
    b["userId"] = "other-user"
    b["scenarioKey"] = "strategy#other"

    def gw_for(user_id):
        if user_id == USER:
            return None, "", "no usable scheduling credential"
        return FakeGateway(), CONTROL, ""

    with patch.object(index, "_all_condition_scenarios", return_value=[a, b]), \
         patch.object(index, "_reading_for", return_value=30), \
         patch.object(index, "_gateway_for", side_effect=gw_for), \
         patch.object(index, "_record_run"):
        out = index.handler({"mode": "sweep"}, None)
    assert out["ran"] == 1


def test_a_gateway_session_is_reused_across_one_users_scenes():
    """Each session costs a token exchange and a handshake; two scenes for the
    same user should not pay it twice."""
    a = _sensor_scene(last=20)
    b = _sensor_scene(last=20)
    b["scenarioKey"] = "strategy#second"
    calls = []

    def gw_for(user_id):
        calls.append(user_id)
        return FakeGateway(), CONTROL, ""

    with patch.object(index, "_all_condition_scenarios", return_value=[a, b]), \
         patch.object(index, "_reading_for", return_value=30), \
         patch.object(index, "_gateway_for", side_effect=gw_for), \
         patch.object(index, "_record_run"):
        index.handler({"mode": "sweep"}, None)
    assert calls == [USER]


def test_the_sweep_only_considers_active_non_template_condition_scenes():
    """A template is a library entry, not an automation, and an inactive scene is
    switched off — firing either would be acting on something nobody armed."""
    import inspect

    src = inspect.getsource(index._all_condition_scenarios)
    assert 'item.get("isActive")' in src
    assert 'item.get("isTemplate")' in src
    assert "SCENE_SENSOR" in src and "SCENE_DEVICE_STATE" in src
    assert "SCENE_TIME" not in src  # those have their own schedules


def test_the_runner_holds_no_iot_client():
    """The design claim, asserted: this Lambda reaches devices only through the
    Gateway, so a Cedar denial is a real denial. An `iot-data` client here would
    be a bypass."""
    import inspect

    src = inspect.getsource(index)
    assert "iot-data" not in src
    assert "iot_data" not in src
    assert "publish" not in src.lower().replace("published", "")
