"""Execute a due scenario as its owner, through the Gateway, under Cedar.

This is what makes a saved scene an automation rather than a note. EventBridge
Scheduler invokes it — one schedule per time-triggered scene, plus one sweep for
the condition-triggered ones — and it applies that scene's device actions.

The design question is whose authority the commands run under, and the answer
determines whether the Admin Console governs automation at all:

    Scheduler -> this Lambda -> Gateway (as the user) -> Cedar -> iot-control

Calling `iot-control` directly would have been simpler and would have made
scheduled scenes the one device path Cedar never sees: an administrator who
revokes a user's `control_device` in Tool Policy would stop their chat commands
and not their 07:30 automation. So the Lambda holds NO IoT permission at all. It
authenticates as the scene's owner and is refused exactly as that user would be.

Acting as an absent user needs a credential, and this was measured rather than
assumed:

  - `GetWorkloadAccessTokenForUserId` mints an AgentCore workload token for a
    user id with nobody present, which is the shape this problem wants — but the
    Gateway's CUSTOM_JWT authorizer rejects it with 401 (`Invalid Bearer token`).
    It is an opaque KMS-encrypted token, not a JWT with the expected audience.
  - There is no public "ask Cedar" API to consult instead; Verified Permissions is
    a different service and AgentCore's `AuthorizeAction` is Gateway-internal.
  - A Cognito refresh token exchanged through `REFRESH_TOKEN_AUTH` yields a real
    idToken with the right audience, and the Gateway accepts it (verified: 200,
    six tools, filtered by Cedar for that user).

So a refresh token is what gets stored, and it is a genuine new attack surface —
a 30-day credential for a real user, at rest. It is therefore kept in Secrets
Manager under a per-user key rather than in the scenarios table, resolved at
runtime, and never logged. Only this Lambda's role may read it. A user with no
stored token simply has no scheduled scenes execute, which fails closed.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

import boto3

import scenarios as sc

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-west-2")
SCENARIOS_TABLE = os.environ.get("SCENARIOS_TABLE_NAME", "smarthome-scenarios")
STATE_TABLE = os.environ.get("DEVICE_STATE_TABLE", "smarthome-device-state")
HISTORY_TABLE = os.environ.get("SENSOR_HISTORY_TABLE", "smarthome-sensor-history")
GATEWAY_URL = os.environ.get("SCENARIO_GATEWAY_URL", "")
USER_POOL_CLIENT_ID = os.environ.get("USER_POOL_CLIENT_ID", "")
# One secret per user, so a single user's credential can be revoked or rotated
# without touching anyone else's, and so the IAM grant can be prefix-scoped.
SECRET_PREFIX = os.environ.get("SCENARIO_SECRET_PREFIX", "smarthome/scenario-tokens/")

# The Gateway tool this Lambda is allowed to reach. Named explicitly rather than
# calling whatever `tools/list` happens to return: a scheduled run should apply
# device actions and nothing else, even if the user is granted more.
CONTROL_TOOL_SUFFIX = "control_device"

_ddb = None
_secrets = None
_cognito = None


def _table(name):
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=REGION)
    return _ddb.Table(name)


def _client(service):
    global _secrets, _cognito
    if service == "secretsmanager":
        if _secrets is None:
            _secrets = boto3.client("secretsmanager", region_name=REGION)
        return _secrets
    if _cognito is None:
        _cognito = boto3.client("cognito-idp", region_name=REGION)
    return _cognito


def _plain(value):
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def secret_name_for(user_id: str) -> str:
    return f"{SECRET_PREFIX}{user_id}"


def _id_token_for(user_id: str) -> str:
    """Exchange the user's stored refresh token for a fresh idToken.

    Returns "" when there is nothing stored or the exchange fails, which stops the
    run. Failing closed is the right direction: an expired or revoked refresh
    token is exactly how a user who has left should stop having automation run in
    their name.

    The token itself is never logged, and neither is the exception text from the
    exchange — a Cognito error can echo the parameter back.
    """
    try:
        raw = _client("secretsmanager").get_secret_value(
            SecretId=secret_name_for(user_id))["SecretString"]
    except Exception as exc:  # noqa: BLE001
        # The AWS error code, not just the exception type. A missing secret and a
        # denied KMS decrypt are the same `ClientError`, and they need opposite
        # fixes — logging only the type sent the first diagnosis looking for an
        # absent secret when the key grant was what was missing. Safe to include:
        # this call has not returned a secret value, so there is nothing to leak.
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        logger.warning("no scheduling credential for %s...: %s %s",
                       user_id[:8], type(exc).__name__, code)
        return ""

    try:
        refresh = json.loads(raw).get("refreshToken", "")
    except (ValueError, AttributeError):
        refresh = raw
    if not refresh:
        logger.warning("empty scheduling credential for %s...", user_id[:8])
        return ""

    try:
        resp = _client("cognito-idp").initiate_auth(
            ClientId=USER_POOL_CLIENT_ID,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": refresh},
        )
        return resp["AuthenticationResult"]["IdToken"]
    except Exception as exc:  # noqa: BLE001
        # Type only. The message can quote the token back.
        logger.warning("refresh exchange failed for %s...: %s",
                       user_id[:8], type(exc).__name__)
        return ""


# ---------------------------------------------------------------------------
# The Gateway
# ---------------------------------------------------------------------------

class GatewayCall:
    """A minimal MCP client — initialize, tools/list, tools/call.

    Hand-rolled rather than using the MCP SDK because this Lambda makes at most a
    handful of calls per run and the SDK would add a dependency (and a cold start)
    for a JSON-RPC POST. The three-step handshake is required: the Gateway rejects
    a `tools/call` on a session that never initialized.
    """

    def __init__(self, url: str, id_token: str):
        self.url = url
        self.headers = {
            "Authorization": f"Bearer {id_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.session_id = ""
        self._next_id = 0

    def _post(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._next_id,
                           "method": method, "params": params or {}}).encode()
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            text = resp.read().decode()
        # The Gateway may answer as SSE even for a single response.
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
        return json.loads(text) if text.strip() else {}

    def initialize(self) -> None:
        self._post("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "scenario-runner", "version": "1.0.0"},
        })

    def tool_names(self) -> list[str]:
        out = self._post("tools/list")
        return [t.get("name", "")
                for t in (out.get("result") or {}).get("tools") or []]

    def call(self, name: str, arguments: dict) -> dict:
        return self._post("tools/call", {"name": name, "arguments": arguments})


# ---------------------------------------------------------------------------
# Readings, for condition triggers
# ---------------------------------------------------------------------------

def _device_state(user_id: str, device_id: str):
    """The device's reported state map, or None when it has never reported."""
    from boto3.dynamodb.conditions import Key

    try:
        resp = _table(STATE_TABLE).query(
            KeyConditionExpression=Key("userId").eq(user_id)
            & Key("deviceId").eq(device_id), Limit=1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read state for %s: %s", device_id, exc)
        return None
    items = resp.get("Items") or []
    return _plain(items[0].get("state") or {}) if items else None


def _latest_metric(user_id: str, metric: str):
    """The most recent sample of a sensor metric, or None.

    Each (device, metric) is its own partition — the sensor samples every metric
    at the same instant, so a shared partition would have them overwriting each
    other on (device, timestamp). Same layout iot-query reads.
    """
    from boto3.dynamodb.conditions import Key

    for device in sc._sensor_metrics().get(metric, []):
        try:
            resp = _table(HISTORY_TABLE).query(
                # `metricKey`, the same composite iot-history-ingest writes and
                # iot-query reads. Guessing this name would have made the sweep
                # find no readings and therefore never fire, with no error.
                KeyConditionExpression=Key("metricKey").eq(
                    f"{user_id}#{device}#{metric}"),
                ScanIndexForward=False, Limit=1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read %s history: %s", metric, exc)
            continue
        items = resp.get("Items") or []
        if items:
            return _plain(items[0].get("value"))
    return None


def _reading_for(user_id: str, trigger: dict):
    scene_type = trigger.get("sceneType")
    if scene_type == sc.SCENE_SENSOR:
        return _latest_metric(user_id, trigger.get("subject", ""))
    if scene_type == sc.SCENE_DEVICE_STATE:
        state = _device_state(user_id, trigger.get("subject", ""))
        if not state:
            return None
        # `power` is what "on"/"off" means for every actuator in the catalog.
        return state.get("power", state)
    return None


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def run_scenario(item: dict, gateway: GatewayCall, control_tool: str) -> dict:
    """Apply one scene's actions. Returns a per-action report."""
    results = []
    for action in sc.pending_actions(item):
        args = {"device_id": action["deviceId"],
                "command": _plain(action["command"]),
                # The partition key device state is written under. Injected from
                # the row's owner, never from anything the scene author supplied.
                "user_id": item["userId"]}
        try:
            out = gateway.call(control_tool, args)
            error = out.get("error")
            body = json.dumps(out.get("result", out))[:400]
            ok = error is None and "denied" not in body.lower()
            results.append({"deviceId": action["deviceId"], "ok": ok,
                            "detail": (error or body)[:300]})
        except urllib.error.HTTPError as exc:
            # 403 here is the system working: Cedar refused this user this tool.
            results.append({"deviceId": action["deviceId"], "ok": False,
                            "detail": f"HTTP {exc.code}"})
        except Exception as exc:  # noqa: BLE001
            logger.exception("action failed for %s", action["deviceId"])
            results.append({"deviceId": action["deviceId"], "ok": False,
                            "detail": type(exc).__name__})
    return {"scenarioId": item.get("scenarioId", ""),
            "name": item.get("name", ""), "actions": results}


def _record_run(item: dict, report: dict, reading=None) -> None:
    """Stamp the row with what happened, so a run is visible after the fact.

    Without this an automation is unobservable: nobody is watching at 07:30, and
    "did it fire" would only be answerable from CloudWatch. `lastReading` also
    gives the next condition sweep something to compare against for `change`.
    """
    from decimal import Decimal

    failed = [a for a in report["actions"] if not a["ok"]]
    update = {
        "lastRunAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lastRunOk": not failed,
        "lastRunDetail": json.dumps(report["actions"])[:900],
    }
    if reading is not None:
        update["lastReading"] = (Decimal(str(reading))
                                 if isinstance(reading, float) else reading)
    try:
        _table(SCENARIOS_TABLE).update_item(
            Key={"userId": item["userId"], "scenarioKey": item["scenarioKey"]},
            UpdateExpression="SET " + ", ".join(f"#{k}=:{k}" for k in update),
            ExpressionAttributeNames={f"#{k}": k for k in update},
            ExpressionAttributeValues={f":{k}": v for k, v in update.items()},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not record the run: %s", exc)


def _load(user_id: str, scenario_key: str) -> dict | None:
    try:
        return _table(SCENARIOS_TABLE).get_item(
            Key={"userId": user_id, "scenarioKey": scenario_key}).get("Item")
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not load %s/%s: %s", user_id[:8], scenario_key, exc)
        return None


def _all_condition_scenarios() -> list[dict]:
    """Every active scene whose trigger is a condition rather than a clock.

    A scan, and honest about it: these rows are bounded by scenes-per-user and the
    sweep runs every five minutes. A GSI on (isActive, sceneType) would be the
    right answer at scale and is not worth a second index at this one.
    """
    out = []
    try:
        kwargs = {}
        while True:
            resp = _table(SCENARIOS_TABLE).scan(**kwargs)
            for item in resp.get("Items", []):
                trigger = item.get("trigger") or {}
                if (item.get("isActive")
                        and not item.get("isTemplate")
                        and trigger.get("sceneType") in (sc.SCENE_SENSOR,
                                                         sc.SCENE_DEVICE_STATE)):
                    out.append(item)
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not sweep scenarios: %s", exc)
    return out


def _gateway_for(user_id: str) -> tuple[GatewayCall | None, str, str]:
    """An initialized Gateway session as `user_id`, plus the control tool's name.

    Returns (None, "", reason) when the user cannot be acted for. The tool name is
    resolved from `tools/list` because the Gateway prefixes it with its target
    (`SmartHomeDeviceControl___control_device`) — and because default-deny hides a
    tool the user is not permitted, its ABSENCE is the authorization answer, not
    an error to work around.
    """
    if not GATEWAY_URL:
        return None, "", "no gateway url configured"
    id_token = _id_token_for(user_id)
    if not id_token:
        return None, "", "no usable scheduling credential"
    gateway = GatewayCall(GATEWAY_URL, id_token)
    try:
        gateway.initialize()
        names = gateway.tool_names()
    except urllib.error.HTTPError as exc:
        return None, "", f"gateway rejected the user token (HTTP {exc.code})"
    except Exception as exc:  # noqa: BLE001
        return None, "", f"gateway unreachable: {type(exc).__name__}"

    control = next((n for n in names
                    if n == CONTROL_TOOL_SUFFIX
                    or n.endswith("___" + CONTROL_TOOL_SUFFIX)), "")
    if not control:
        # The point of the whole design: revoking control_device in Tool Policy
        # stops this user's automation, because Cedar stops listing the tool.
        return None, "", ("control_device is not permitted for this user — "
                          "scheduled actions will not run")
    return gateway, control, ""


def handler(event, context):
    """Two entry points, distinguished by the payload Scheduler sends.

      {"mode": "scenario", "userId": ..., "scenarioKey": ...}  one time-triggered scene
      {"mode": "sweep"}                                        all condition triggers
    """
    logger.info("event: %s", json.dumps(event, default=str))
    mode = (event or {}).get("mode") or "sweep"

    if mode == "scenario":
        user_id = event.get("userId", "")
        scenario_key = event.get("scenarioKey", "")
        item = _load(user_id, scenario_key)
        if not item:
            logger.warning("scenario %s/%s is gone — nothing to run",
                           user_id[:8], scenario_key)
            return {"ran": 0, "reason": "not found"}
        if not item.get("isActive"):
            return {"ran": 0, "reason": "inactive"}

        gateway, control, reason = _gateway_for(user_id)
        if gateway is None:
            logger.warning("cannot run %s for %s...: %s",
                           scenario_key, user_id[:8], reason)
            _record_run(item, {"scenarioId": item.get("scenarioId", ""),
                               "name": item.get("name", ""),
                               "actions": [{"deviceId": "-", "ok": False,
                                            "detail": reason}]})
            return {"ran": 0, "reason": reason}

        report = run_scenario(item, gateway, control)
        _record_run(item, report)
        # A `once` scene disables itself, so it does not fire again tomorrow.
        if (item.get("trigger") or {}).get("executionType") == sc.EXEC_ONCE:
            try:
                _table(SCENARIOS_TABLE).update_item(
                    Key={"userId": user_id, "scenarioKey": scenario_key},
                    UpdateExpression="SET isActive = :f",
                    ExpressionAttributeValues={":f": False})
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not deactivate a once-only scene: %s", exc)
        logger.info("ran %s: %s", scenario_key, json.dumps(report))
        return {"ran": 1, "report": report}

    # Sweep: evaluate every condition trigger against the current reading.
    reports = []
    sessions: dict[str, tuple] = {}
    for item in _all_condition_scenarios():
        trigger = item.get("trigger") or {}
        user_id = item["userId"]
        reading = _reading_for(user_id, trigger)
        if not sc.should_fire(trigger, reading):
            continue
        # Edge, not level: without this a "temperature above 27" scene would
        # re-run every five minutes for as long as it stays warm.
        if trigger.get("calculationType") != sc.CALC_CHANGE:
            if item.get("lastReading") is not None and sc.should_fire(
                    trigger, _plain(item["lastReading"])):
                continue
        elif str(_plain(item.get("lastReading"))) == str(reading):
            continue

        if user_id not in sessions:
            sessions[user_id] = _gateway_for(user_id)
        gateway, control, reason = sessions[user_id]
        if gateway is None:
            logger.warning("cannot run %s for %s...: %s",
                           item["scenarioKey"], user_id[:8], reason)
            continue
        report = run_scenario(item, gateway, control)
        _record_run(item, report, reading=reading)
        reports.append(report)

    logger.info("sweep fired %d scenario(s)", len(reports))
    return {"ran": len(reports), "reports": reports}
