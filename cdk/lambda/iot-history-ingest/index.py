"""
Expands a batch of sensor readings into DynamoDB rows.

Invoked by the `smarthome_sensor_history` IoT topic rule. Device *state* goes
straight to DynamoDB via the rule's dynamoDBv2 action with no Lambda, because
one message is one row. History cannot use that path: seeding 24h of 5-minute
samples is ~288 points per metric, and publishing them one MQTT message at a
time would be over a thousand messages per session. So the simulator sends one
message carrying an array, and this fans it out with BatchWriteItem.

Identity comes from the TOPIC, not the payload. The rule is configured to pass
`topic()` through, so a client cannot write history against another user's
partition key even though it controls the body.
"""

import json
import logging
import os
import time
from decimal import Decimal

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HISTORY_TABLE = os.environ.get("SENSOR_HISTORY_TABLE", "smarthome-sensor-history")
# Matches the table's TTL intent: a week of history is enough for "this week's
# trend" without keeping simulator noise around indefinitely.
TTL_SECONDS = 7 * 24 * 3600
# BatchWriteItem's hard limit.
BATCH_SIZE = 25
# A malformed or hostile message should not be able to write unbounded rows.
MAX_POINTS = 2000

_table = None


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource(
            "dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2")
        ).Table(HISTORY_TABLE)
    return _table


def _split_topic(topic):
    """`smarthome/{userSub}/{deviceId}/history` -> (userSub, deviceId)."""
    parts = (topic or "").split("/")
    if len(parts) < 4 or parts[0] != "smarthome":
        return None, None
    return parts[1], parts[2]


def handler(event, context):
    logger.info("Received event: %s", json.dumps(event, default=str)[:2000])

    # The rule's SQL adds `topic` alongside the published payload.
    user_sub, device_id = _split_topic(event.get("topic"))
    if not user_sub or not device_id:
        logger.warning("unroutable topic: %r", event.get("topic"))
        return {"written": 0, "error": "could not derive user and device from topic"}

    points = event.get("points") or []
    if not isinstance(points, list):
        return {"written": 0, "error": "points must be a list"}
    if len(points) > MAX_POINTS:
        logger.warning("truncating %d points to %d", len(points), MAX_POINTS)
        points = points[:MAX_POINTS]

    now = int(time.time())
    expires = now + TTL_SECONDS

    rows = []
    for p in points:
        if not isinstance(p, dict):
            continue
        metric = p.get("metric")
        value = p.get("value")
        ts = p.get("ts", now)
        if not metric or value is None:
            continue
        try:
            ts = int(ts)
            # DynamoDB has no float type; Decimal built from a string keeps two
            # decimal places without float-repr noise.
            value = Decimal(str(round(float(value), 2)))
        except (TypeError, ValueError):
            continue
        rows.append({
            # One partition per metric: a sensor samples all of its metrics at
            # the same instant, so keying on (device, ts) would let them
            # overwrite each other and leave only one metric per timestamp.
            "metricKey": f"{user_sub}#{device_id}#{metric}",
            "ts": ts,
            "metric": metric,
            "value": value,
            "ttl": expires,
        })

    if not rows:
        return {"written": 0}

    table = _get_table()
    written = 0
    try:
        with table.batch_writer(overwrite_by_pkeys=["metricKey", "ts"]) as batch:
            for row in rows:
                batch.put_item(Item=row)
                written += 1
    except Exception as e:
        logger.error("batch write failed after %d rows: %s", written, e, exc_info=True)
        return {"written": written, "error": str(e)}

    logger.info("wrote %d history rows for %s/%s", written, user_sub, device_id)
    return {"written": written}
