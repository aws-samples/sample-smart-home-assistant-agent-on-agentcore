"""AgentCore Optimization handlers — recommendations, bundles, A/B tests.

Dispatched by `index.py` for any path under `/optimization/*`. State is
stored in the existing `smarthome-skills` DynamoDB table under reserved
sort keys (`__opt_rec_*__`, `__opt_bundle_*__`, `__opt_abtest_*__`); see
`optimization_defaults.opt_sk`.
"""
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, UnknownServiceError

from optimization_defaults import (
    component_arn_for,
    json_path_for,
    opt_sk,
    parse_opt_sk,
)

logger = logging.getLogger(__name__)
SKILLS_TABLE_NAME = os.environ.get("SKILLS_TABLE_NAME", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

# --- Preview-availability probe ----------------------------------------------
PREVIEW_UNAVAILABLE = False
try:
    _probe = boto3.client("bedrock-agentcore", region_name=AWS_REGION)
    _probe.meta.service_model.operation_model("StartRecommendation")
except (UnknownServiceError, Exception) as e:  # noqa: BLE001
    logger.warning("AgentCore Optimization preview not available: %s", e)
    PREVIEW_UNAVAILABLE = True


_data_client = None
_control_client = None


def _agentcore_data():
    global _data_client
    if _data_client is None:
        _data_client = boto3.client("bedrock-agentcore", region_name=AWS_REGION)
    return _data_client


def _agentcore_control():
    global _control_client
    if _control_client is None:
        _control_client = boto3.client("bedrock-agentcore-control", region_name=AWS_REGION)
    return _control_client


def _ddb_table():
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(SKILLS_TABLE_NAME)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _caller_email(event) -> str:
    return event.get("requestContext", {}).get("authorizer", {}).get("claims", {}).get("email", "")


def _resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body, default=str)}


def _preview_guard():
    if PREVIEW_UNAVAILABLE:
        return _resp(501, {
            "error": "AgentCoreOptimizationUnavailable",
            "message": "AgentCore Optimization is in public preview and not yet available in this region.",
        })
    return None


def _client_error_to_resp(e: ClientError):
    code = e.response.get("Error", {}).get("Code", "InternalServerException")
    msg = e.response.get("Error", {}).get("Message", str(e))
    mapping = {
        "ValidationException": 400,
        "ResourceNotFoundException": 404,
        "ConflictException": 409,
        "ServiceQuotaExceededException": 402,
        "ThrottlingException": 429,
    }
    return _resp(mapping.get(code, 500), {"error": code, "message": msg})


# --- DDB helpers -------------------------------------------------------------
def _query_opt_rows(scope: str, kind_prefix: str):
    t = _ddb_table()
    resp = t.query(
        KeyConditionExpression=Key("userId").eq(scope) & Key("skillName").begins_with(f"__opt_{kind_prefix}_"),
    )
    return resp.get("Items", [])


def _write_rec_row(scope: str, rec_id: str, fields: dict):
    t = _ddb_table()
    item = {"userId": scope, "skillName": opt_sk("rec", rec_id), **fields}
    t.put_item(Item=item)


def _load_current_prompt(scope: str, agent_type: str) -> str:
    """Read the active prompt body for this scope/type from the existing
    __prompt_*__ row. Empty string when no override is set."""
    if agent_type == "tool_desc":
        return ""  # tool descriptions live on gateway targets, not DDB
    sk = f"__prompt_{agent_type}__"
    t = _ddb_table()
    resp = t.get_item(Key={"userId": scope, "skillName": sk})
    item = resp.get("Item")
    return (item.get("promptBody") or "") if item else ""


# --- Validation --------------------------------------------------------------
_VALID_AGENT_TYPES = ("text", "voice", "tool_desc")


def _validate_log_group(arn: str, agent_type: str) -> str | None:
    if agent_type == "tool_desc":
        return None  # gateway targets don't write to aws/spans
    if "log-group:aws/spans" not in arn:
        return "logGroupArn must point at the aws/spans log group"
    return None


# --- Handlers: Recommendations ----------------------------------------------
def start_recommendation(event):
    g = _preview_guard()
    if g:
        return g
    body = json.loads(event.get("body") or "{}")
    scope = body.get("scope", "__global__")
    agent_type = body.get("agentType", "")
    if agent_type not in _VALID_AGENT_TYPES:
        return _resp(400, {"error": "ValidationException", "message": "agentType must be text|voice|tool_desc"})
    log_group_arn = body.get("logGroupArn", "")
    err = _validate_log_group(log_group_arn, agent_type)
    if err:
        return _resp(400, {"error": "ValidationException", "message": err})

    name = body.get("name") or f"opt-{agent_type}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    evaluator_arn = body.get("evaluatorArn", "")
    start_time = body.get("startTime")
    end_time = body.get("endTime")
    rule_filter = body.get("ruleFilter")

    current_prompt = _load_current_prompt(scope, agent_type) or " "  # API rejects empty
    rec_type = "TOOL_DESCRIPTION_RECOMMENDATION" if agent_type == "tool_desc" else "SYSTEM_PROMPT_RECOMMENDATION"
    config_key = "toolDescriptionRecommendationConfig" if agent_type == "tool_desc" else "systemPromptRecommendationConfig"

    # CloudWatch Logs ARNs in CFN/UI often include a trailing `:*` log-stream
    # filter — the AgentCore API validator only accepts the bare log-group ARN.
    normalized_log_group = log_group_arn[:-2] if log_group_arn.endswith(":*") else log_group_arn

    # Build agentTraces.cloudwatchLogs block
    cw = {
        "logGroupArns": [normalized_log_group],
        "serviceNames": [agent_type],  # auto-resolved from agent type
        "startTime": start_time,
        "endTime": end_time,
    }
    if rule_filter:
        cw["rule"] = rule_filter

    inner_config = {
        "agentTraces": {"cloudwatchLogs": cw},
        "evaluationConfig": {"evaluators": [{"evaluatorArn": evaluator_arn}]},
    }
    if agent_type == "tool_desc":
        inner_config["toolDescriptions"] = {"toolDescriptionText": {"tools": []}}
    else:
        inner_config["systemPrompt"] = {"text": current_prompt}

    try:
        resp = _agentcore_data().start_recommendation(
            name=name,
            type=rec_type,
            recommendationConfig={config_key: inner_config},
            clientToken=str(uuid.uuid4()),
        )
    except ClientError as e:
        return _client_error_to_resp(e)

    rec_id = resp["recommendationId"]
    _write_rec_row(scope, rec_id, {
        "recommendationArn": resp["recommendationArn"],
        "agentType": agent_type,
        "status": resp["status"],
        "evaluatorArn": evaluator_arn,
        "logGroupArn": log_group_arn,
        "startTime": start_time,
        "endTime": end_time,
        "ruleFilter": rule_filter,
        "createdAt": _now_iso(),
        "createdBy": _caller_email(event),
    })
    return _resp(202, {
        "recommendationId": rec_id,
        "recommendationArn": resp["recommendationArn"],
        "status": resp["status"],
    })


def list_recommendations(event):
    g = _preview_guard()
    if g:
        return g
    scope = (event.get("queryStringParameters") or {}).get("scope", "__global__")
    rows = _query_opt_rows(scope, "rec")
    out = []
    for r in rows:
        parsed = parse_opt_sk(r["skillName"])
        if not parsed or parsed[0] != "rec":
            continue
        out.append({
            "recommendationId": parsed[1],
            "agentType": r.get("agentType"),
            "status": r.get("status"),
            "createdAt": r.get("createdAt"),
            "evaluatorArn": r.get("evaluatorArn"),
            "appliedAt": r.get("appliedAt"),
        })
    return _resp(200, out)


def _update_rec_status(rec_id: str, status: str):
    """Sync DDB cached status. On transition to FAILED, also set TTL
    (`__opt_expires`) per spec §5: failed recs auto-expire after 7 days."""
    t = _ddb_table()
    sk = opt_sk("rec", rec_id)
    resp = t.scan(FilterExpression=Key("skillName").eq(sk))
    for item in resp.get("Items", []):
        update = "SET #s = :s, updatedAt = :u"
        names = {"#s": "status"}
        vals = {":s": status, ":u": _now_iso()}
        if status == "FAILED":
            update += ", #e = :e"
            names["#e"] = "__opt_expires"
            vals[":e"] = int((datetime.now(timezone.utc) + timedelta(days=7)).timestamp())
        t.update_item(
            Key={"userId": item["userId"], "skillName": sk},
            UpdateExpression=update,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=vals,
        )


def _get_rec_row(rec_id: str) -> dict | None:
    t = _ddb_table()
    sk = opt_sk("rec", rec_id)
    resp = t.scan(FilterExpression=Key("skillName").eq(sk))
    items = resp.get("Items", [])
    return items[0] if items else None


def _delete_rec_row(rec_id: str):
    row = _get_rec_row(rec_id)
    if row:
        _ddb_table().delete_item(
            Key={"userId": row["userId"], "skillName": opt_sk("rec", rec_id)}
        )


def _mark_rec_applied(rec_id: str, version_id: str):
    row = _get_rec_row(rec_id)
    if not row:
        return
    _ddb_table().update_item(
        Key={"userId": row["userId"], "skillName": opt_sk("rec", rec_id)},
        UpdateExpression="SET appliedAt = :a, appliedBundleVersionId = :v",
        ExpressionAttributeValues={":a": _now_iso(), ":v": version_id},
    )


def _write_prompt_row(scope: str, agent_type: str, body_text: str, updated_by: str):
    sk = f"__prompt_{agent_type}__"
    _ddb_table().put_item(Item={
        "userId": scope, "skillName": sk,
        "promptBody": body_text, "updatedAt": _now_iso(), "updatedBy": updated_by,
    })


def _create_bundle_version(scope: str, agent_type: str, content,
                           source_rec_id: str | None = None) -> tuple[str, str]:
    """Create-or-update a bundle for this scope+agentType. Returns
    (bundleArn, versionId). First call creates the bundle; subsequent calls
    create a new version chained off the latest."""
    bundle_name = f"smarthome_{scope.replace('@', '_at_').replace('.', '_')}_{agent_type}"
    if agent_type == "tool_desc":
        component_arn = "tool_desc"  # gateway-target ARNs handled per-tool by caller
        cfg = {"tools": content}
    else:
        component_arn = component_arn_for(agent_type)
        # Use just the leaf key from the JSON path; full path is for recommendation
        # input, the bundle stores the value under that same key.
        cfg = {"system_prompt": content}
    ctrl = _agentcore_control()
    try:
        resp = ctrl.create_configuration_bundle(
            name=bundle_name,
            components=[{"componentArn": component_arn, "configuration": cfg}],
        )
        return resp["bundleArn"], resp["versionId"]
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConflictException":
            raise
        existing = ctrl.list_configuration_bundles(nameContains=bundle_name)
        bundle_arn = existing["bundles"][0]["bundleArn"]
        latest = ctrl.list_configuration_bundle_versions(bundleArn=bundle_arn)["versions"][0]["versionId"]
        v = ctrl.create_configuration_bundle_version(
            bundleArn=bundle_arn,
            parentVersionId=latest,
            components=[{"componentArn": component_arn, "configuration": cfg}],
        )
        return bundle_arn, v["versionId"]


def get_recommendation(event):
    g = _preview_guard()
    if g:
        return g
    rec_id = (event.get("pathParameters") or {}).get("recId", "")
    try:
        full = _agentcore_data().get_recommendation(recommendationId=rec_id)
    except ClientError as e:
        return _client_error_to_resp(e)
    _update_rec_status(rec_id, full["status"])
    out = {
        "recommendationId": full["recommendationId"],
        "status": full["status"],
        "type": full["type"],
        "createdAt": full.get("createdAt"),
        "updatedAt": full.get("updatedAt"),
    }
    result = full.get("recommendationResult", {})
    sys_r = result.get("systemPromptRecommendationResult")
    tool_r = result.get("toolDescriptionRecommendationResult")
    if sys_r:
        out["recommendedSystemPrompt"] = sys_r.get("recommendedSystemPrompt")
        out["errorCode"] = sys_r.get("errorCode")
        out["errorMessage"] = sys_r.get("errorMessage")
    if tool_r:
        out["tools"] = tool_r.get("tools", [])
        out["errorCode"] = tool_r.get("errorCode")
        out["errorMessage"] = tool_r.get("errorMessage")
    return _resp(200, out)


def delete_recommendation(event):
    g = _preview_guard()
    if g:
        return g
    rec_id = (event.get("pathParameters") or {}).get("recId", "")
    try:
        _agentcore_data().delete_recommendation(recommendationId=rec_id)
    except ClientError as e:
        return _client_error_to_resp(e)
    _delete_rec_row(rec_id)
    return _resp(204, {})


def apply_recommendation(event):
    g = _preview_guard()
    if g:
        return g
    rec_id = (event.get("pathParameters") or {}).get("recId", "")
    row = _get_rec_row(rec_id)
    if not row:
        return _resp(404, {"error": "ResourceNotFoundException",
                           "message": f"Recommendation {rec_id} not found"})
    agent_type = row["agentType"]
    scope = row["userId"]
    full = _agentcore_data().get_recommendation(recommendationId=rec_id)
    if full["status"] != "COMPLETED":
        return _resp(409, {"error": "ConflictException",
                           "message": f"Recommendation status is {full['status']}, must be COMPLETED before apply"})
    result = full.get("recommendationResult", {})
    if agent_type == "tool_desc":
        tools = result.get("toolDescriptionRecommendationResult", {}).get("tools", [])
        for tool in tools:
            try:
                _agentcore_control().update_gateway_target(
                    gatewayIdentifier=os.environ.get("GATEWAY_ID", ""),
                    targetId=tool["toolName"],
                    toolSchema={"description": tool["recommendedToolDescription"]},
                )
            except ClientError as e:
                logger.warning("update_gateway_target failed for %s: %s", tool["toolName"], e)
        bundle_arn, version = _create_bundle_version(scope, "tool_desc", tools, source_rec_id=rec_id)
    else:
        new_prompt = result.get("systemPromptRecommendationResult", {}).get("recommendedSystemPrompt", "")
        _write_prompt_row(scope, agent_type, new_prompt, _caller_email(event))
        bundle_arn, version = _create_bundle_version(scope, agent_type, new_prompt, source_rec_id=rec_id)
    _mark_rec_applied(rec_id, version)
    return _resp(200, {"appliedBundleArn": bundle_arn, "appliedBundleVersionId": version})
