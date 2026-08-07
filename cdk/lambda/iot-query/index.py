"""
Lambda function to read device state and sensor history.
Called as an AgentCore Gateway Lambda target.

This is the read half of the device link. Commands go out over MQTT
(iot-control), the simulator reports back on `.../state`, an IoT topic rule
writes that to DynamoDB, and this reads it. Before the reporting path existed
device state only lived in the browser's React state, so questions like "is the
living room light on" or "what is the temperature" had no data source at all.

Two tools:
  query_device_state   — current state of one device, or all of them
  query_sensor_history — a metric's readings over a time window

Per-user isolation matches iot-control: the partition key is the caller's
Cognito `sub`, taken from the agent-injected `user_id` rather than anything the
LLM can set.
"""

import base64
import json
import logging
import os
import time
from decimal import Decimal

import boto3

import device_catalog

logger = logging.getLogger()
logger.setLevel(logging.INFO)

STATE_TABLE = os.environ.get("DEVICE_STATE_TABLE", "smarthome-device-state")
HISTORY_TABLE = os.environ.get("SENSOR_HISTORY_TABLE", "smarthome-sensor-history")

# A day of 5-minute samples is 288 points per metric; the cap keeps a broad
# question ("temperature this week") from returning thousands of rows into the
# model's context.
MAX_HISTORY_POINTS = 300

_dynamodb = None


def _table(name):
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    return _dynamodb.Table(name)


def _plain(value):
    """DynamoDB numbers come back as Decimal, which json.dumps refuses."""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _decode_jwt_sub(token):
    try:
        raw = (token or "").replace("Bearer ", "").split(".")
        if len(raw) < 2:
            return None
        payload = raw[1] + "=" * (-len(raw[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("sub")
    except Exception:
        return None


def _extract_user_sub(event, context):
    """Same resolution order as iot-control — see its docstring for why the
    agent-injected `user_id` is trusted and the LLM cannot forge it."""
    uid = event.get("user_id")
    if uid:
        return uid
    if hasattr(context, "client_context") and context.client_context:
        custom = getattr(context.client_context, "custom", None)
        if isinstance(custom, str):
            try:
                custom = json.loads(custom)
            except (json.JSONDecodeError, TypeError):
                custom = None
        if isinstance(custom, dict):
            for f in ("sub", "principalId", "bedrockAgentCorePrincipalId"):
                if custom.get(f):
                    return custom[f]
            token = custom.get("token") or custom.get("jwt") or custom.get("idToken")
            if token:
                sub = _decode_jwt_sub(token)
                if sub:
                    return sub
    for field in ("callerIdentity", "identity", "userIdentity"):
        identity = event.get(field)
        if isinstance(identity, dict):
            sub = identity.get("sub") or identity.get("principalId")
            if sub:
                return sub
    return None


def _tool_name(context):
    if hasattr(context, "client_context") and context.client_context:
        custom = getattr(context.client_context, "custom", None)
        if isinstance(custom, dict):
            raw = custom.get("bedrockAgentCoreToolName", "")
            delimiter = "___"
            if delimiter in raw:
                return raw[raw.index(delimiter) + len(delimiter):]
            return raw
    return ""


def query_device_state(user_sub, event):
    device_id = event.get("device_id")
    device_type = event.get("device_type")

    if device_id or device_type:
        device, err = device_catalog.resolve_device(device_id, device_type)
        if err:
            return {"error": err}
        wanted = [device]
    else:
        wanted = device_catalog.devices()

    table = _table(STATE_TABLE)
    out = []
    for d in wanted:
        try:
            resp = table.get_item(Key={"userId": user_sub, "deviceId": d["deviceId"]})
        except Exception as e:
            logger.warning("state read failed for %s: %s", d["deviceId"], e)
            resp = {}
        item = resp.get("Item")
        entry = {
            "deviceId": d["deviceId"],
            "deviceType": d["deviceType"],
            "displayName": device_catalog.label(d),
            "room": d.get("room"),
        }
        if item:
            entry["state"] = _plain(item.get("state", {}))
            entry["online"] = bool(item.get("online", True))
            entry["reportedAt"] = item.get("reportedAt")
        else:
            # No row means the simulator has never reported this device (or the
            # row aged out). Saying so beats returning a fabricated default —
            # the agent should tell the user to open the simulator.
            entry["state"] = None
            entry["online"] = False
            entry["reason"] = "no state reported yet — the device simulator may be closed"
        out.append(entry)

    return {"userId": user_sub, "devices": out, "count": len(out)}


def query_sensor_history(user_sub, event):
    device_id = event.get("device_id")
    metric = event.get("metric")
    hours = event.get("hours", 24)

    device, err = device_catalog.resolve_device(device_id, event.get("device_type") or "sensor")
    if err:
        return {"error": err}

    caps = device.get("capabilities", {})
    readable = [name for name, cap in caps.items() if cap.get("type") == "readonly"]
    if not readable:
        return {"error": f"{device_catalog.label(device)} reports no sensor metrics"}
    if metric and metric not in readable:
        return {"error": f"Unknown metric '{metric}' for {device['deviceId']}. Valid: {', '.join(readable)}"}

    try:
        hours = max(1, min(168, int(hours)))
    except (TypeError, ValueError):
        hours = 24
    since = int(time.time()) - hours * 3600

    from boto3.dynamodb.conditions import Key

    # One query per metric, because each metric is its own partition — they are
    # sampled at the same instant, so a shared partition would have them
    # overwriting each other on (device, ts).
    table = _table(HISTORY_TABLE)
    wanted_metrics = [metric] if metric else readable
    series = {}
    for name in wanted_metrics:
        try:
            resp = table.query(
                KeyConditionExpression=Key("metricKey").eq(
                    f"{user_sub}#{device['deviceId']}#{name}"
                ) & Key("ts").gte(since),
                # Newest first, so a long window drops the oldest points rather
                # than the recent ones the user most likely means.
                ScanIndexForward=False,
                Limit=MAX_HISTORY_POINTS,
            )
        except Exception as e:
            logger.warning("history query failed for %s: %s", name, e)
            continue
        points = [
            {"ts": _plain(i.get("ts")), "value": _plain(i.get("value"))}
            for i in resp.get("Items", [])
        ]
        if points:
            points.sort(key=lambda p: p["ts"])
            series[name] = points

    if not series:
        return {
            "deviceId": device["deviceId"],
            "hours": hours,
            "series": {},
            "reason": "no readings recorded in this window — the device simulator may be closed",
        }

    summary = {
        name: {
            "latest": points[-1]["value"],
            "min": min(p["value"] for p in points),
            "max": max(p["value"] for p in points),
            "average": round(sum(p["value"] for p in points) / len(points), 2),
            "points": len(points),
        }
        for name, points in series.items()
    }

    return {
        "deviceId": device["deviceId"],
        "displayName": device_catalog.label(device),
        "hours": hours,
        "units": {name: caps[name].get("unit") for name in series},
        "summary": summary,
        "series": series,
    }


def handler(event, context):
    logger.info("Received event: %s", json.dumps(event, default=str))

    try:
        user_sub = _extract_user_sub(event, context)
        if not user_sub:
            return {"error": "caller identity missing — cannot read devices for unknown user"}

        tool = _tool_name(context)
        logger.info("Tool name: %s", tool)

        # Route on the tool name, falling back to the shape of the request so a
        # direct invocation (tests, CLI) still works.
        if "history" in tool or event.get("metric") or event.get("hours"):
            return query_sensor_history(user_sub, event)
        return query_device_state(user_sub, event)

    except Exception as e:
        logger.error("Error: %s", e, exc_info=True)
        return {"error": str(e)}
