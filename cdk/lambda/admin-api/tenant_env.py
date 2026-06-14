"""Admin API — per-tenant entryEnvironment configuration.

DDB layout (existing smarthome-skills table):
    PK: __global__   SK: __tenant_env_{email}__
    Fields: mode ("default" | "ab-bundles" | "ab-targets"),
            updatedAt, updatedBy.

Missing row resolves to "default" (server-side and client-side).
"""
import datetime as _dt
import json
import logging
import os
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

VALID_MODES = ("default", "ab-bundles", "ab-targets")
SK_PREFIX = "__tenant_env_"
SK_SUFFIX = "__"

_table_resource = None


def _table():
    global _table_resource
    if _table_resource is None:
        import boto3
        _table_resource = boto3.resource("dynamodb").Table(os.environ["SKILLS_TABLE_NAME"])
    return _table_resource


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body),
    }


def _claims(event) -> dict:
    return ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}


def _email_from_sk(sk: str) -> str | None:
    if sk.startswith(SK_PREFIX) and sk.endswith(SK_SUFFIX):
        return sk[len(SK_PREFIX):-len(SK_SUFFIX)]
    return None


def list_tenant_envs(event):
    """GET /skills?tenantEnv=1 — list all per-tenant overrides."""
    items = _table().query(
        KeyConditionExpression=Key("userId").eq("__global__")
            & Key("skillName").begins_with(SK_PREFIX),
    ).get("Items", [])

    overrides = []
    for it in items:
        email = _email_from_sk(it.get("skillName", ""))
        if not email:
            continue
        overrides.append({
            "email": email,
            "mode": it.get("mode", "default"),
            "updatedAt": it.get("updatedAt"),
            "updatedBy": it.get("updatedBy"),
        })
    return _resp(200, {"overrides": overrides})


def get_tenant_env(event):
    """GET /skills?tenantEnv=1&userId={email} — single tenant's mode."""
    qs = event.get("queryStringParameters") or {}
    email = qs.get("userId", "")
    if not email:
        return _resp(400, {"error": "userId required"})

    sk = f"{SK_PREFIX}{email}{SK_SUFFIX}"
    item = _table().get_item(Key={"userId": "__global__", "skillName": sk}).get("Item")
    if not item:
        return _resp(200, {"mode": "default"})
    return _resp(200, {
        "mode": item.get("mode", "default"),
        "updatedAt": item.get("updatedAt"),
        "updatedBy": item.get("updatedBy"),
    })


def put_tenant_env(event):
    """PUT /skills/{userId}/__tenant_env__ body: {mode, acknowledgeMaskedOverride?}"""
    path = event.get("pathParameters") or {}
    email = path.get("userId", "")
    if not email:
        return _resp(400, {"error": "userId path param required"})

    body = json.loads(event.get("body") or "{}")
    mode = body.get("mode")
    if mode not in VALID_MODES:
        return _resp(400, {"error": f"mode must be one of {VALID_MODES}"})

    if mode == "ab-bundles" and not body.get("acknowledgeMaskedOverride"):
        existing_prompt = _table().get_item(
            Key={"userId": email, "skillName": "__prompt_text__"}
        ).get("Item")
        if existing_prompt and existing_prompt.get("promptBody"):
            return _resp(409, {
                "error": "PerUserPromptWillBeMasked",
                "message": (f"{email} has a per-user system prompt override. "
                            "ab-bundles mode replaces it via the hook. "
                            "Re-submit with acknowledgeMaskedOverride: true to confirm."),
            })

    sk = f"{SK_PREFIX}{email}{SK_SUFFIX}"
    now = _dt.datetime.utcnow().isoformat() + "Z"
    updated_by = _claims(event).get("email", "admin")
    _table().put_item(Item={
        "userId": "__global__",
        "skillName": sk,
        "mode": mode,
        "updatedAt": now,
        "updatedBy": updated_by,
    })
    return _resp(200, {"email": email, "mode": mode, "updatedAt": now, "updatedBy": updated_by})


def delete_tenant_env(event):
    """DELETE /skills/{userId}/__tenant_env__ — fall back to default."""
    path = event.get("pathParameters") or {}
    email = path.get("userId", "")
    if not email:
        return _resp(400, {"error": "userId path param required"})
    sk = f"{SK_PREFIX}{email}{SK_SUFFIX}"
    _table().delete_item(Key={"userId": "__global__", "skillName": sk})
    return _resp(200, {"email": email, "mode": "default"})
