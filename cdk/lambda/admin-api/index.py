"""Admin API Lambda — CRUD operations for agent skills stored in DynamoDB."""

import json
import os
import re
import logging
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("SKILLS_TABLE_NAME", "smarthome-skills")
RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
agentcore_client = boto3.client("bedrock-agentcore", region_name=REGION)

# Strands SDK skill name pattern: lowercase alphanumeric + hyphens, 1-64 chars
SKILL_NAME_RE = re.compile(r"^(?!-)(?!.*--)(?!.*-$)[a-z0-9-]{1,64}$")


def response(status_code, body):
    """Return an API Gateway proxy response with CORS headers."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body),
    }


def check_admin(event):
    """Return True if the caller belongs to the 'admin' Cognito group."""
    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
    groups = claims.get("cognito:groups", "")
    return "admin" in groups


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def list_skills(event):
    """GET /skills?userId=__global__"""
    params = event.get("queryStringParameters") or {}
    user_id = params.get("userId", "__global__")

    resp = table.query(KeyConditionExpression=Key("userId").eq(user_id))
    items = resp.get("Items", [])
    # Convert sets to lists for JSON serialisation
    for item in items:
        if "allowedTools" in item and isinstance(item["allowedTools"], set):
            item["allowedTools"] = list(item["allowedTools"])
    return response(200, {"skills": items})


def create_skill(event):
    """POST /skills  body: {userId, skillName, description, instructions, allowedTools}"""
    body = json.loads(event.get("body") or "{}")
    user_id = body.get("userId", "__global__")
    skill_name = body.get("skillName", "")
    description = body.get("description", "")
    instructions = body.get("instructions", "")
    allowed_tools = body.get("allowedTools", [])

    if not skill_name or not SKILL_NAME_RE.match(skill_name):
        return response(400, {
            "error": f"Invalid skillName '{skill_name}'. Must be 1-64 lowercase alphanumeric characters and hyphens, no leading/trailing/consecutive hyphens."
        })
    if not description:
        return response(400, {"error": "description is required"})

    ts = now_iso()
    try:
        table.put_item(
            Item={
                "userId": user_id,
                "skillName": skill_name,
                "description": description,
                "instructions": instructions,
                "allowedTools": allowed_tools,
                "createdAt": ts,
                "updatedAt": ts,
            },
            ConditionExpression="attribute_not_exists(userId) AND attribute_not_exists(skillName)",
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return response(409, {"error": f"Skill '{skill_name}' already exists for userId '{user_id}'"})

    return response(201, {"message": f"Skill '{skill_name}' created", "userId": user_id, "skillName": skill_name})


def get_skill(event):
    """GET /skills/{userId}/{skillName}"""
    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")
    skill_name = path_params.get("skillName", "")

    resp = table.get_item(Key={"userId": user_id, "skillName": skill_name})
    item = resp.get("Item")
    if not item:
        return response(404, {"error": f"Skill '{skill_name}' not found for userId '{user_id}'"})
    if "allowedTools" in item and isinstance(item["allowedTools"], set):
        item["allowedTools"] = list(item["allowedTools"])
    return response(200, item)


def update_skill(event):
    """PUT /skills/{userId}/{skillName}  body: {description, instructions, allowedTools}"""
    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")
    skill_name = path_params.get("skillName", "")
    body = json.loads(event.get("body") or "{}")

    # Build update expression dynamically
    update_parts = []
    expr_names = {}
    expr_values = {":updatedAt": now_iso()}
    update_parts.append("#updatedAt = :updatedAt")
    expr_names["#updatedAt"] = "updatedAt"

    for field in ("description", "instructions", "allowedTools"):
        if field in body:
            safe = f"#{field}"
            expr_names[safe] = field
            expr_values[f":{field}"] = body[field]
            update_parts.append(f"{safe} = :{field}")

    if len(update_parts) == 1:
        return response(400, {"error": "No fields to update. Provide description, instructions, or allowedTools."})

    try:
        table.update_item(
            Key={"userId": user_id, "skillName": skill_name},
            UpdateExpression="SET " + ", ".join(update_parts),
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
            ConditionExpression="attribute_exists(userId) AND attribute_exists(skillName)",
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return response(404, {"error": f"Skill '{skill_name}' not found for userId '{user_id}'"})

    return response(200, {"message": f"Skill '{skill_name}' updated", "userId": user_id, "skillName": skill_name})


def delete_skill(event):
    """DELETE /skills/{userId}/{skillName}"""
    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")
    skill_name = path_params.get("skillName", "")

    table.delete_item(Key={"userId": user_id, "skillName": skill_name})
    return response(200, {"message": f"Skill '{skill_name}' deleted", "userId": user_id, "skillName": skill_name})


def list_users(_event):
    """GET /skills/users — return distinct userIds in the table."""
    resp = table.scan(ProjectionExpression="userId")
    user_ids = sorted(set(item["userId"] for item in resp.get("Items", [])))
    return response(200, {"userIds": user_ids})


def get_settings(event):
    """GET /settings/{userId} — return user settings (e.g., modelId)."""
    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")

    resp = table.get_item(Key={"userId": user_id, "skillName": "__settings__"})
    item = resp.get("Item")
    if not item:
        return response(200, {"userId": user_id, "modelId": ""})
    return response(200, {"userId": item["userId"], "modelId": item.get("modelId", "")})


def update_settings(event):
    """PUT /settings/{userId} — update user settings (e.g., modelId)."""
    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")
    body = json.loads(event.get("body") or "{}")

    model_id = body.get("modelId", "")
    ts = now_iso()

    table.put_item(Item={
        "userId": user_id,
        "skillName": "__settings__",
        "modelId": model_id,
        "updatedAt": ts,
    })
    return response(200, {"message": f"Settings updated for '{user_id}'", "modelId": model_id})


def list_sessions(_event):
    """GET /sessions — list all user sessions from DynamoDB."""
    resp = table.scan(
        FilterExpression="skillName = :sk",
        ExpressionAttributeValues={":sk": "__session__"},
    )
    sessions = []
    for item in resp.get("Items", []):
        sessions.append({
            "userId": item["userId"],
            "sessionId": item.get("sessionId", ""),
            "lastActiveAt": item.get("lastActiveAt", ""),
        })
    sessions.sort(key=lambda s: s.get("lastActiveAt", ""), reverse=True)
    return response(200, {"sessions": sessions})


def stop_session(event):
    """POST /sessions/{sessionId}/stop — stop an AgentCore runtime session."""
    path_params = event.get("pathParameters") or {}
    session_id = path_params.get("sessionId", "")

    if not session_id:
        return response(400, {"error": "sessionId is required"})
    if not RUNTIME_ARN:
        return response(500, {"error": "AGENT_RUNTIME_ARN not configured"})

    try:
        agentcore_client.stop_runtime_session(
            runtimeSessionId=session_id,
            agentRuntimeArn=RUNTIME_ARN,
        )
        return response(200, {"message": f"Session '{session_id}' stop requested"})
    except agentcore_client.exceptions.ResourceNotFoundException:
        return response(404, {"error": f"Session '{session_id}' not found"})
    except Exception as e:
        return response(500, {"error": f"Failed to stop session: {str(e)}"})


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def handler(event, context):
    logger.info("Event: %s", json.dumps(event, default=str))

    if not check_admin(event):
        return response(403, {"error": "Forbidden: admin group required"})

    method = event.get("httpMethod", "")
    resource = event.get("resource", "")

    if resource == "/skills" and method == "GET":
        return list_skills(event)
    if resource == "/skills" and method == "POST":
        return create_skill(event)
    if resource == "/skills/users" and method == "GET":
        return list_users(event)
    if resource == "/skills/{userId}/{skillName}" and method == "GET":
        return get_skill(event)
    if resource == "/skills/{userId}/{skillName}" and method == "PUT":
        return update_skill(event)
    if resource == "/skills/{userId}/{skillName}" and method == "DELETE":
        return delete_skill(event)
    if resource == "/settings/{userId}" and method == "GET":
        return get_settings(event)
    if resource == "/settings/{userId}" and method == "PUT":
        return update_settings(event)
    if resource == "/sessions" and method == "GET":
        return list_sessions(event)
    if resource == "/sessions/{sessionId}/stop" and method == "POST":
        return stop_session(event)

    return response(400, {"error": f"Unknown route: {method} {resource}"})
