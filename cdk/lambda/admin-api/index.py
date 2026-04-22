"""Admin API Lambda — CRUD operations for agent skills stored in DynamoDB,
plus per-user tool permission management via AgentCore Policy Engine."""

import json
import os
import re
import logging
import time
from datetime import datetime, timezone
from urllib.parse import unquote

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("SKILLS_TABLE_NAME", "smarthome-skills")
SKILL_FILES_BUCKET = os.environ.get("SKILL_FILES_BUCKET", "")
RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
MEMORY_ID = os.environ.get("MEMORY_ID", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
GATEWAY_ID = os.environ.get("GATEWAY_ID", "")
KB_DOCS_BUCKET = os.environ.get("KB_DOCS_BUCKET", "")
AOSS_ENDPOINT = os.environ.get("AOSS_ENDPOINT", "")
AOSS_COLLECTION_ARN = os.environ.get("AOSS_COLLECTION_ARN", "")
KB_SERVICE_ROLE_ARN = os.environ.get("KB_SERVICE_ROLE_ARN", "")
KB_ID = os.environ.get("KB_ID", "")
KB_DATA_SOURCE_ID = os.environ.get("KB_DATA_SOURCE_ID", "")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
s3_client = boto3.client("s3", region_name=REGION)
agentcore_client = boto3.client("bedrock-agentcore", region_name=REGION)
agentcore_control = boto3.client("bedrock-agentcore-control", region_name=REGION)
cognito_client = boto3.client("cognito-idp", region_name=REGION)
bedrock_agent_client = boto3.client("bedrock-agent", region_name=REGION)
logs_client = boto3.client("logs", region_name=REGION)

# AgentCore emits GenAI spans to this log group with session.id +
# gen_ai.usage.total_tokens attributes on each LLM call span.
SPANS_LOG_GROUP = "aws/spans"

ALLOWED_FILE_DIRS = {"scripts", "references", "assets"}

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
    # Filter out internal records (settings, sessions, permissions, etc.)
    items = [i for i in items if not i.get("skillName", "").startswith("__")]
    # Convert sets to lists for JSON serialisation
    for item in items:
        if "allowedTools" in item and isinstance(item["allowedTools"], set):
            item["allowedTools"] = list(item["allowedTools"])
    return response(200, {"skills": items})


def create_skill(event):
    """POST /skills  body: {userId, skillName, description, instructions, allowedTools, license, compatibility, metadata}"""
    body = json.loads(event.get("body") or "{}")
    user_id = body.get("userId", "__global__")
    skill_name = body.get("skillName", "")
    description = body.get("description", "")
    instructions = body.get("instructions", "")
    allowed_tools = body.get("allowedTools", [])
    skill_license = body.get("license", "")
    compatibility = body.get("compatibility", "")
    metadata = body.get("metadata", {})

    if not skill_name or not SKILL_NAME_RE.match(skill_name):
        return response(400, {
            "error": f"Invalid skillName '{skill_name}'. Must be 1-64 lowercase alphanumeric characters and hyphens, no leading/trailing/consecutive hyphens."
        })
    if not description:
        return response(400, {"error": "description is required"})
    if compatibility and len(compatibility) > 500:
        return response(400, {"error": "compatibility must be at most 500 characters"})
    if not isinstance(metadata, dict):
        return response(400, {"error": "metadata must be a key-value object"})

    ts = now_iso()
    item = {
        "userId": user_id,
        "skillName": skill_name,
        "description": description,
        "instructions": instructions,
        "allowedTools": allowed_tools,
        "createdAt": ts,
        "updatedAt": ts,
    }
    if skill_license:
        item["license"] = skill_license
    if compatibility:
        item["compatibility"] = compatibility
    if metadata:
        item["metadata"] = metadata

    try:
        table.put_item(
            Item=item,
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
    """PUT /skills/{userId}/{skillName}  body: {description, instructions, allowedTools, license, compatibility, metadata}"""
    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")
    skill_name = path_params.get("skillName", "")
    body = json.loads(event.get("body") or "{}")

    if "compatibility" in body and body["compatibility"] and len(body["compatibility"]) > 500:
        return response(400, {"error": "compatibility must be at most 500 characters"})
    if "metadata" in body and not isinstance(body["metadata"], dict):
        return response(400, {"error": "metadata must be a key-value object"})

    # Build update expression dynamically
    update_parts = []
    expr_names = {}
    expr_values = {":updatedAt": now_iso()}
    update_parts.append("#updatedAt = :updatedAt")
    expr_names["#updatedAt"] = "updatedAt"

    for field in ("description", "instructions", "allowedTools", "license", "compatibility", "metadata"):
        if field in body:
            safe = f"#{field}"
            expr_names[safe] = field
            expr_values[f":{field}"] = body[field]
            update_parts.append(f"{safe} = :{field}")

    if len(update_parts) == 1:
        return response(400, {"error": "No fields to update."})

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

    # Cascade-delete S3 files for this skill
    if SKILL_FILES_BUCKET:
        prefix = f"{user_id}/{skill_name}/"
        try:
            resp = s3_client.list_objects_v2(Bucket=SKILL_FILES_BUCKET, Prefix=prefix)
            objects = resp.get("Contents", [])
            if objects:
                s3_client.delete_objects(
                    Bucket=SKILL_FILES_BUCKET,
                    Delete={"Objects": [{"Key": obj["Key"]} for obj in objects]},
                )
        except Exception as e:
            logger.warning(f"Failed to delete S3 files for {user_id}/{skill_name}: {e}")

    return response(200, {"message": f"Skill '{skill_name}' deleted", "userId": user_id, "skillName": skill_name})


def list_users(_event):
    """GET /skills/users — return distinct userIds in the table."""
    resp = table.scan(ProjectionExpression="userId")
    user_ids = sorted(set(
        item["userId"] for item in resp.get("Items", [])
        if not item["userId"].startswith("__")
    ))
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


def _fetch_token_totals_7d():
    """Query CloudWatch Logs Insights over the last 7 days for the sum of
    gen_ai.usage.total_tokens per session.id.

    AgentCore Runtime exports Strands/ADOT spans to the `aws/spans` log group.
    Each `chat` span carries both `attributes.session.id` and
    `attributes.gen_ai.usage.total_tokens` — summing the latter grouped by the
    former yields per-session token consumption shown in the CloudWatch
    GenAI Observability dashboard.

    Returns: dict {sessionId: int}. On failure (e.g. query timeout, permission
    issue) returns {} so the sessions list still renders without token data.
    """
    end_time = int(time.time())
    start_time = end_time - 7 * 24 * 3600
    query = (
        'filter scope.name = "strands.telemetry.tracer"\n'
        '| filter ispresent(attributes.gen_ai.usage.total_tokens)\n'
        '| stats sum(attributes.gen_ai.usage.total_tokens) as totalTokens '
        'by attributes.session.id as sessionId\n'
        '| limit 10000'
    )
    try:
        start = logs_client.start_query(
            logGroupName=SPANS_LOG_GROUP,
            startTime=start_time,
            endTime=end_time,
            queryString=query,
        )
        query_id = start["queryId"]
    except logs_client.exceptions.ResourceNotFoundException:
        logger.info("aws/spans log group not found; returning empty token totals")
        return {}
    except Exception as e:
        logger.warning("Logs Insights start_query failed: %s", e)
        return {}

    # Poll for up to ~20s — Lambda timeout is 30s and the rest of list_sessions
    # is fast, so we can afford to wait.
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            res = logs_client.get_query_results(queryId=query_id)
        except Exception as e:
            logger.warning("Logs Insights get_query_results failed: %s", e)
            return {}
        status = res.get("status", "")
        if status in ("Complete", "Failed", "Cancelled", "Timeout"):
            if status != "Complete":
                logger.warning("Logs Insights query ended with status=%s", status)
                return {}
            break
        time.sleep(0.5)
    else:
        logger.warning("Logs Insights query %s timed out client-side", query_id)
        try:
            logs_client.stop_query(queryId=query_id)
        except Exception:
            pass
        return {}

    totals = {}
    for row in res.get("results", []):
        record = {field["field"]: field["value"] for field in row}
        session_id = record.get("sessionId") or record.get("attributes.session.id", "")
        raw = record.get("totalTokens", "0")
        if not session_id:
            continue
        try:
            totals[session_id] = int(float(raw))
        except (TypeError, ValueError):
            continue
    return totals


def list_sessions(_event):
    """GET /sessions — list all user sessions from DynamoDB, enriched with
    past-7-day token totals pulled from CloudWatch Logs Insights."""
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

    token_totals = _fetch_token_totals_7d()
    for s in sessions:
        s["totalTokens7d"] = token_totals.get(s["sessionId"], 0)

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
# User & Tool Permission Management
# ---------------------------------------------------------------------------

def list_cognito_users(_event):
    """GET /users — list all users from Cognito user pool."""
    if not COGNITO_USER_POOL_ID:
        return response(500, {"error": "COGNITO_USER_POOL_ID not configured"})

    users = []
    params = {"UserPoolId": COGNITO_USER_POOL_ID, "Limit": 60}
    while True:
        resp = cognito_client.list_users(**params)
        for u in resp.get("Users", []):
            attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
            # Fetch groups for this user
            groups = []
            try:
                g_resp = cognito_client.admin_list_groups_for_user(
                    UserPoolId=COGNITO_USER_POOL_ID,
                    Username=u["Username"],
                )
                groups = [g["GroupName"] for g in g_resp.get("Groups", [])]
            except Exception:
                pass
            users.append({
                "username": u["Username"],
                "email": attrs.get("email", ""),
                "sub": attrs.get("sub", u["Username"]),
                "status": u.get("UserStatus", ""),
                "createdAt": u.get("UserCreateDate", "").isoformat() if hasattr(u.get("UserCreateDate", ""), "isoformat") else str(u.get("UserCreateDate", "")),
                "groups": groups,
            })
        token = resp.get("PaginationToken")
        if not token:
            break
        params["PaginationToken"] = token

    return response(200, {"users": users})


def list_gateway_tools(_event):
    """GET /tools — list all tools from AgentCore Gateway targets."""
    if not GATEWAY_ID:
        return response(500, {"error": "GATEWAY_ID not configured"})

    tools = []
    try:
        targets_resp = agentcore_control.list_gateway_targets(
            gatewayIdentifier=GATEWAY_ID
        )
        for target_summary in targets_resp.get("items", []):
            target_id = target_summary["targetId"]
            target_name = target_summary.get("name", "")
            try:
                target = agentcore_control.get_gateway_target(
                    gatewayIdentifier=GATEWAY_ID,
                    targetId=target_id,
                )
                # Extract tools from MCP lambda target configuration
                target_config = target.get("targetConfiguration", {})
                mcp = target_config.get("mcp", {})
                lambda_cfg = mcp.get("lambda", {})
                tool_schema = lambda_cfg.get("toolSchema", {})

                # Tool schema can be inline or in S3
                tool_defs = tool_schema.get("inlinePayload", [])
                if not tool_defs and "s3" in tool_schema:
                    s3_uri = tool_schema["s3"].get("uri", "")
                    if s3_uri.startswith("s3://"):
                        # Parse s3://bucket/key
                        parts = s3_uri[5:].split("/", 1)
                        if len(parts) == 2:
                            obj = s3_client.get_object(Bucket=parts[0], Key=parts[1])
                            tool_defs = json.loads(obj["Body"].read())

                for tool_def in tool_defs:
                    tools.append({
                        "name": tool_def.get("name", ""),
                        "description": tool_def.get("description", ""),
                        "targetName": target_name,
                    })
            except Exception as e:
                logger.warning(f"Failed to get target {target_id}: {e}")
    except Exception as e:
        return response(500, {"error": f"Failed to list gateway tools: {str(e)}"})

    return response(200, {"tools": tools})


def get_user_permissions(event):
    """GET /users/{userId}/permissions — get allowed tools for a user."""
    path_params = event.get("pathParameters") or {}
    user_id = unquote(path_params.get("userId", ""))
    if not user_id:
        return response(400, {"error": "userId is required"})

    resp = table.get_item(Key={"userId": user_id, "skillName": "__permissions__"})
    item = resp.get("Item")
    if not item:
        return response(200, {"userId": user_id, "allowedTools": []})
    allowed = item.get("allowedTools", [])
    if isinstance(allowed, set):
        allowed = list(allowed)
    return response(200, {
        "userId": user_id,
        "allowedTools": allowed,
        "updatedAt": item.get("updatedAt", ""),
    })


def update_user_permissions(event):
    """PUT /users/{userId}/permissions — update allowed tools and sync Cedar policies."""
    path_params = event.get("pathParameters") or {}
    user_id = unquote(path_params.get("userId", ""))
    if not user_id:
        return response(400, {"error": "userId is required"})

    body = json.loads(event.get("body") or "{}")
    new_tools = body.get("allowedTools", [])
    if not isinstance(new_tools, list):
        return response(400, {"error": "allowedTools must be a list"})

    # Read old permissions to determine which tools changed
    old_resp = table.get_item(Key={"userId": user_id, "skillName": "__permissions__"})
    old_item = old_resp.get("Item")
    old_tools = list(old_item.get("allowedTools", [])) if old_item else []

    # Save to DynamoDB
    ts = now_iso()
    if new_tools:
        table.put_item(Item={
            "userId": user_id,
            "skillName": "__permissions__",
            "allowedTools": new_tools,
            "updatedAt": ts,
        })
    else:
        # Remove permissions entry if no tools selected
        table.delete_item(Key={"userId": user_id, "skillName": "__permissions__"})

    # Determine which tools need policy rebuild
    affected_tools = set(old_tools) | set(new_tools)

    if not GATEWAY_ID:
        return response(200, {
            "message": f"Permissions saved for '{user_id}' (policy sync skipped — no GATEWAY_ID)",
            "allowedTools": new_tools,
        })

    # Rebuild Cedar policies for each affected tool
    errors = []
    for tool_name in affected_tools:
        try:
            rebuild_tool_policy(tool_name)
        except Exception as e:
            logger.error(f"Failed to rebuild policy for tool '{tool_name}': {e}")
            errors.append(f"{tool_name}: {str(e)}")

    if errors:
        return response(200, {
            "message": f"Permissions saved for '{user_id}' with policy sync errors",
            "allowedTools": new_tools,
            "policyErrors": errors,
        })

    return response(200, {
        "message": f"Permissions saved for '{user_id}'",
        "allowedTools": new_tools,
    })


# ---------------------------------------------------------------------------
# Memory Management
# ---------------------------------------------------------------------------

def list_memory_actors(_event):
    """GET /memories — list all actors in AgentCore Memory."""
    if not MEMORY_ID:
        return response(500, {"error": "MEMORY_ID not configured"})
    try:
        actors = []
        params = {"memoryId": MEMORY_ID, "maxResults": 100}
        while True:
            resp = agentcore_client.list_actors(**params)
            actors.extend(a["actorId"] for a in resp.get("actorSummaries", []))
            token = resp.get("nextToken")
            if not token:
                break
            params["nextToken"] = token
        return response(200, {"actors": actors})
    except Exception as e:
        return response(500, {"error": f"Failed to list memory actors: {str(e)}"})


def get_memory_records(event):
    """GET /memories/{actorId} — get long-term memory records for a user."""
    if not MEMORY_ID:
        return response(500, {"error": "MEMORY_ID not configured"})

    path_params = event.get("pathParameters") or {}
    actor_id = unquote(path_params.get("actorId", ""))
    if not actor_id:
        return response(400, {"error": "actorId is required"})

    records = []
    for ns_type in ["facts", "preferences"]:
        namespace = f"/users/{actor_id}/{ns_type}"
        try:
            params = {"memoryId": MEMORY_ID, "namespace": namespace, "maxResults": 50}
            while True:
                resp = agentcore_client.list_memory_records(**params)
                for r in resp.get("memoryRecordSummaries", []):
                    content = r.get("content", {})
                    records.append({
                        "id": r.get("memoryRecordId", ""),
                        "type": ns_type,
                        "text": content.get("text", ""),
                        "strategy": r.get("memoryStrategyId", ""),
                        "createdAt": r.get("createdAt", "").isoformat() if hasattr(r.get("createdAt", ""), "isoformat") else str(r.get("createdAt", "")),
                    })
                token = resp.get("nextToken")
                if not token:
                    break
                params["nextToken"] = token
        except Exception as e:
            logger.warning(f"Failed to list memory records for {namespace}: {e}")

    records.sort(key=lambda r: r.get("createdAt", ""), reverse=True)
    return response(200, {"actorId": actor_id, "records": records})


# ---------------------------------------------------------------------------
# Cedar Policy Helpers
# ---------------------------------------------------------------------------

def ensure_policy_engine():
    """Get or create the policy engine. Returns (policyEngineId, policyEngineArn)."""
    # Check DynamoDB first
    resp = table.get_item(Key={"userId": "__system__", "skillName": "__policy_engine__"})
    item = resp.get("Item")
    if item and item.get("policyEngineId"):
        return item["policyEngineId"], item.get("policyEngineArn", "")

    # Create policy engine
    try:
        create_resp = agentcore_control.create_policy_engine(
            name="SmartHomeUserPermissions",
            description="Per-user tool access control for SmartHome Gateway",
        )
        engine_id = create_resp["policyEngineId"]
        engine_arn = create_resp.get("policyEngineArn", "")
    except agentcore_control.exceptions.ConflictException:
        # Already exists — list and find it
        list_resp = agentcore_control.list_policy_engines()
        for eng in list_resp.get("policyEngines", []):
            if eng.get("name") == "SmartHomeUserPermissions":
                engine_id = eng["policyEngineId"]
                engine_arn = eng.get("policyEngineArn", "")
                break
        else:
            raise Exception("Policy engine conflict but could not find existing engine")

    # Poll until ACTIVE (max 30s)
    for _ in range(15):
        try:
            get_resp = agentcore_control.get_policy_engine(policyEngineId=engine_id)
            if get_resp.get("status") == "ACTIVE":
                engine_arn = get_resp.get("policyEngineArn", engine_arn)
                break
        except Exception:
            pass
        time.sleep(2)

    # Store in DynamoDB
    table.put_item(Item={
        "userId": "__system__",
        "skillName": "__policy_engine__",
        "policyEngineId": engine_id,
        "policyEngineArn": engine_arn,
        "updatedAt": now_iso(),
    })

    return engine_id, engine_arn


def ensure_gateway_policy_engine(policy_engine_arn):
    """Associate the policy engine with the gateway if not already."""
    gw = agentcore_control.get_gateway(gatewayIdentifier=GATEWAY_ID)

    existing_config = gw.get("policyEngineConfiguration")
    if existing_config and existing_config.get("arn") == policy_engine_arn:
        return  # Already associated

    # The gateway role needs GetPolicyEngine permission to use the policy engine
    gw_role_arn = gw.get("roleArn", "")
    if gw_role_arn:
        gw_role_name = gw_role_arn.split("/")[-1]
        iam_client = boto3.client("iam", region_name=REGION)
        try:
            iam_client.put_role_policy(
                RoleName=gw_role_name,
                PolicyName="PolicyEngineAccess",
                PolicyDocument=json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [{
                        "Effect": "Allow",
                        "Action": [
                            "bedrock-agentcore:GetPolicyEngine",
                            "bedrock-agentcore:ListPolicies",
                            "bedrock-agentcore:GetPolicy",
                            "bedrock-agentcore:AuthorizeAction",
                            "bedrock-agentcore:PartiallyAuthorizeActions",
                        ],
                        "Resource": "*",
                    }],
                }),
            )
            logger.info(f"Granted PolicyEngineAccess to gateway role {gw_role_name}")
            # IAM policy propagation delay
            time.sleep(10)
        except Exception as e:
            logger.warning(f"Failed to grant PolicyEngineAccess to gateway role: {e}")

    # UpdateGateway requires re-supplying existing fields
    update_kwargs = dict(
        gatewayIdentifier=GATEWAY_ID,
        name=gw["name"],
        roleArn=gw["roleArn"],
        protocolType=gw["protocolType"],
        authorizerType=gw["authorizerType"],
        policyEngineConfiguration={
            "arn": policy_engine_arn,
            "mode": "ENFORCE",
        },
    )
    if gw.get("authorizerConfiguration"):
        update_kwargs["authorizerConfiguration"] = gw["authorizerConfiguration"]
    agentcore_control.update_gateway(**update_kwargs)
    logger.info(f"Associated policy engine {policy_engine_arn} (ENFORCE) with gateway {GATEWAY_ID}")


def _get_gateway_arn():
    """Get the gateway ARN (cached in module-level after first call)."""
    global _gateway_arn_cache
    if not hasattr(_get_gateway_arn, '_cache'):
        gw = agentcore_control.get_gateway(gatewayIdentifier=GATEWAY_ID)
        _get_gateway_arn._cache = gw.get("gatewayArn", "")
    return _get_gateway_arn._cache


def _get_tool_action_map():
    """Build a map of tool_name -> Cedar action name ({TargetName}___{toolName})."""
    if not hasattr(_get_tool_action_map, '_cache'):
        action_map = {}
        targets = agentcore_control.list_gateway_targets(gatewayIdentifier=GATEWAY_ID)
        for t in targets.get("items", []):
            target_name = t.get("name", "")
            try:
                target = agentcore_control.get_gateway_target(
                    gatewayIdentifier=GATEWAY_ID, targetId=t["targetId"])
                mcp = target.get("targetConfiguration", {}).get("mcp", {})
                lambda_cfg = mcp.get("lambda", {})
                tool_schema = lambda_cfg.get("toolSchema", {})
                tool_defs = tool_schema.get("inlinePayload", [])
                if not tool_defs and "s3" in tool_schema:
                    s3_uri = tool_schema["s3"].get("uri", "")
                    if s3_uri.startswith("s3://"):
                        parts = s3_uri[5:].split("/", 1)
                        if len(parts) == 2:
                            obj = s3_client.get_object(Bucket=parts[0], Key=parts[1])
                            tool_defs = json.loads(obj["Body"].read())
                for td in tool_defs:
                    tool_name = td.get("name", "")
                    action_map[tool_name] = f"{target_name}___{tool_name}"
            except Exception as e:
                logger.warning(f"Failed to get tools for target {target_name}: {e}")
        _get_tool_action_map._cache = action_map
    return _get_tool_action_map._cache


def build_cedar_statement(tool_name, user_ids):
    """Build a Cedar permit statement for a tool with per-user access control.

    Uses the permit model with default-deny:
      - If any user has the tool enabled → create a permit policy with principal.id checks
      - If no users have the tool → no permit → default-deny blocks the tool

    Requires gateway with authorizerType: CUSTOM_JWT so that principal.id
    is available during policy evaluation.

    Cedar schema (discovered via StartPolicyGeneration):
      action == AgentCore::Action::"{TargetName}___{toolName}"
      resource == AgentCore::Gateway::"{gatewayArn}"
      principal.id for user identity (from JWT)
    """
    action_map = _get_tool_action_map()
    action_name = action_map.get(tool_name, tool_name)
    gateway_arn = _get_gateway_arn()

    if not user_ids:
        return ""  # No permit → default-deny blocks this tool

    conditions = " || ".join(
        f'(principal.id) == "{uid}"' for uid in sorted(user_ids)
    )
    return (
        f'permit(\n'
        f'  principal,\n'
        f'  action == AgentCore::Action::"{action_name}",\n'
        f'  resource == AgentCore::Gateway::"{gateway_arn}"\n'
        f') when {{\n'
        f'  ((principal is AgentCore::OAuthUser) || (principal is AgentCore::IamEntity)) &&\n'
        f'  ({conditions})\n'
        f'}};'
    )


def rebuild_tool_policy(tool_name):
    """Scan DynamoDB for all users with this tool and create/update/delete the Cedar policy."""
    engine_id, engine_arn = ensure_policy_engine()
    ensure_gateway_policy_engine(engine_arn)

    # Scan for all users who have this tool in their permissions
    user_ids = []
    scan_params = {
        "FilterExpression": "skillName = :sk",
        "ExpressionAttributeValues": {":sk": "__permissions__"},
    }
    while True:
        resp = table.scan(**scan_params)
        for item in resp.get("Items", []):
            allowed = item.get("allowedTools", [])
            if isinstance(allowed, set):
                allowed = list(allowed)
            if tool_name in allowed:
                user_ids.append(item["userId"])
        if "LastEvaluatedKey" not in resp:
            break
        scan_params["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    # Look up existing policy for this tool in DynamoDB
    policy_record = table.get_item(
        Key={"userId": "__system__", "skillName": f"__tool_policy_{tool_name}__"}
    ).get("Item")
    existing_policy_id = policy_record.get("policyId") if policy_record else None

    policy_name = f"ToolPolicy_{tool_name.replace('-', '_')}"

    cedar_stmt = build_cedar_statement(tool_name, user_ids)

    if not cedar_stmt:
        # No users have this tool → delete the permit policy (default-deny blocks it)
        if existing_policy_id:
            try:
                agentcore_control.delete_policy(
                    policyEngineId=engine_id, policyId=existing_policy_id)
            except Exception as e:
                logger.warning(f"Failed to delete policy {existing_policy_id}: {e}")
            table.delete_item(
                Key={"userId": "__system__", "skillName": f"__tool_policy_{tool_name}__"})
            logger.info(f"Deleted permit policy for tool '{tool_name}' (no authorized users)")
        return

    if existing_policy_id:
        # Update existing policy
        agentcore_control.update_policy(
            policyEngineId=engine_id,
            policyId=existing_policy_id,
            definition={"cedar": {"statement": cedar_stmt}},
            validationMode="IGNORE_ALL_FINDINGS",
        )
        logger.info(f"Updated policy {existing_policy_id} for tool '{tool_name}' with {len(user_ids)} users")
    else:
        # Create new policy
        create_resp = agentcore_control.create_policy(
            policyEngineId=engine_id,
            name=policy_name,
            definition={"cedar": {"statement": cedar_stmt}},
            description=f"Controls access to the {tool_name} tool",
            validationMode="IGNORE_ALL_FINDINGS",
        )
        new_policy_id = create_resp["policyId"]
        table.put_item(Item={
            "userId": "__system__",
            "skillName": f"__tool_policy_{tool_name}__",
            "policyId": new_policy_id,
            "policyName": policy_name,
            "updatedAt": now_iso(),
        })
        logger.info(f"Created policy {new_policy_id} for tool '{tool_name}' with {len(user_ids)} users")


# ---------------------------------------------------------------------------
# Skill File Management (S3)
# ---------------------------------------------------------------------------

def list_skill_files(event):
    """GET /skills/{userId}/{skillName}/files — list files in skill directory."""
    if not SKILL_FILES_BUCKET:
        return response(500, {"error": "Skill files bucket not configured"})

    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")
    skill_name = path_params.get("skillName", "")
    prefix = f"{user_id}/{skill_name}/"

    resp = s3_client.list_objects_v2(Bucket=SKILL_FILES_BUCKET, Prefix=prefix)
    files = []
    for obj in resp.get("Contents", []):
        key = obj["Key"]
        relative = key[len(prefix):]
        if not relative:
            continue
        files.append({
            "path": relative,
            "size": obj["Size"],
            "lastModified": obj["LastModified"].isoformat(),
        })
    return response(200, {"files": files})


def get_upload_url(event):
    """POST /skills/{userId}/{skillName}/files/upload-url — generate presigned PUT URL."""
    if not SKILL_FILES_BUCKET:
        return response(500, {"error": "Skill files bucket not configured"})

    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")
    skill_name = path_params.get("skillName", "")
    body = json.loads(event.get("body") or "{}")
    directory = body.get("directory", "")
    filename = body.get("filename", "")

    if directory not in ALLOWED_FILE_DIRS:
        return response(400, {"error": f"Invalid directory '{directory}'. Must be one of: {', '.join(sorted(ALLOWED_FILE_DIRS))}"})
    if not filename or "/" in filename or "\\" in filename:
        return response(400, {"error": "Invalid filename"})

    key = f"{user_id}/{skill_name}/{directory}/{filename}"
    content_type = body.get("contentType", "application/octet-stream")

    url = s3_client.generate_presigned_url(
        "put_object",
        Params={"Bucket": SKILL_FILES_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=900,
    )
    return response(200, {"uploadUrl": url, "key": key})


def get_download_url(event):
    """POST /skills/{userId}/{skillName}/files/download-url — generate presigned GET URL."""
    if not SKILL_FILES_BUCKET:
        return response(500, {"error": "Skill files bucket not configured"})

    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")
    skill_name = path_params.get("skillName", "")
    body = json.loads(event.get("body") or "{}")
    file_path = body.get("path", "")

    if not file_path:
        return response(400, {"error": "path is required"})

    key = f"{user_id}/{skill_name}/{file_path}"
    url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": SKILL_FILES_BUCKET, "Key": key},
        ExpiresIn=900,
    )
    return response(200, {"downloadUrl": url})


def delete_skill_file(event):
    """DELETE /skills/{userId}/{skillName}/files?path=... — delete a file."""
    if not SKILL_FILES_BUCKET:
        return response(500, {"error": "Skill files bucket not configured"})

    path_params = event.get("pathParameters") or {}
    user_id = path_params.get("userId", "")
    skill_name = path_params.get("skillName", "")
    params = event.get("queryStringParameters") or {}
    file_path = params.get("path", "")

    if not file_path:
        return response(400, {"error": "path query parameter is required"})

    key = f"{user_id}/{skill_name}/{file_path}"
    s3_client.delete_object(Bucket=SKILL_FILES_BUCKET, Key=key)
    return response(200, {"message": f"File '{file_path}' deleted"})


# ---------------------------------------------------------------------------
# Knowledge Base Management
# ---------------------------------------------------------------------------


def _get_kb_config():
    """Get KB configuration from DynamoDB. Returns (kb_id, data_source_id) or (None, None)."""
    # Prefer env vars (set by setup script)
    if KB_ID and KB_DATA_SOURCE_ID:
        return KB_ID, KB_DATA_SOURCE_ID
    resp = table.get_item(Key={"userId": "__kb_config__", "skillName": "__default__"})
    item = resp.get("Item")
    if item and item.get("knowledgeBaseId"):
        return item["knowledgeBaseId"], item.get("dataSourceId", "")
    return None, None




def get_kb_status(_event):
    """GET /knowledge-bases — return KB status and document counts per scope."""
    kb_id, ds_id = _get_kb_config()

    result = {
        "initialized": bool(kb_id),
        "knowledgeBaseId": kb_id or "",
        "dataSourceId": ds_id or "",
        "status": "NOT_INITIALIZED",
        "scopes": [],
    }

    if kb_id:
        try:
            kb = bedrock_agent_client.get_knowledge_base(knowledgeBaseId=kb_id)
            result["status"] = kb["knowledgeBase"]["status"]
        except Exception as e:
            result["status"] = f"ERROR: {str(e)}"

    # List scopes (top-level prefixes in KB docs bucket)
    if KB_DOCS_BUCKET:
        try:
            resp = s3_client.list_objects_v2(Bucket=KB_DOCS_BUCKET, Delimiter="/")
            for prefix in resp.get("CommonPrefixes", []):
                scope = prefix["Prefix"].rstrip("/")
                # Count files in this scope (exclude .metadata.json files)
                scope_resp = s3_client.list_objects_v2(Bucket=KB_DOCS_BUCKET, Prefix=f"{scope}/")
                doc_count = sum(
                    1 for obj in scope_resp.get("Contents", [])
                    if not obj["Key"].endswith(".metadata.json")
                )
                result["scopes"].append({"scope": scope, "documentCount": doc_count})
        except Exception as e:
            logger.warning(f"Failed to list KB scopes: {e}")

    return response(200, result)


def list_kb_documents(event):
    """GET /knowledge-bases/documents?scope=__shared__ — list documents in a scope."""
    if not KB_DOCS_BUCKET:
        return response(500, {"error": "KB_DOCS_BUCKET not configured"})

    params = event.get("queryStringParameters") or {}
    scope = params.get("scope", "__shared__")

    prefix = f"{scope}/"
    resp = s3_client.list_objects_v2(Bucket=KB_DOCS_BUCKET, Prefix=prefix)

    files = []
    for obj in resp.get("Contents", []):
        key = obj["Key"]
        # Skip metadata sidecar files
        if key.endswith(".metadata.json"):
            continue
        relative = key[len(prefix):]
        if not relative:
            continue
        files.append({
            "name": relative,
            "key": key,
            "size": obj["Size"],
            "lastModified": obj["LastModified"].isoformat(),
        })

    return response(200, {"scope": scope, "documents": files})


def get_kb_upload_url(event):
    """POST /knowledge-bases/documents/upload-url — get presigned PUT URL and create metadata sidecar."""
    if not KB_DOCS_BUCKET:
        return response(500, {"error": "KB_DOCS_BUCKET not configured"})

    body = json.loads(event.get("body") or "{}")
    scope = body.get("scope", "__shared__")
    filename = body.get("filename", "")

    if not filename or "/" in filename or "\\" in filename:
        return response(400, {"error": "Invalid filename"})

    key = f"{scope}/{filename}"
    content_type = body.get("contentType", "application/octet-stream")

    # Generate presigned upload URL
    url = s3_client.generate_presigned_url(
        "put_object",
        Params={"Bucket": KB_DOCS_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=900,
    )

    # Pre-create metadata sidecar file (Bedrock KB expects simple key-value pairs)
    metadata_key = f"{key}.metadata.json"
    metadata_content = {
        "metadataAttributes": {
            "scope": scope,
        }
    }
    s3_client.put_object(
        Bucket=KB_DOCS_BUCKET,
        Key=metadata_key,
        Body=json.dumps(metadata_content),
        ContentType="application/json",
    )

    return response(200, {"uploadUrl": url, "key": key})


def delete_kb_document(event):
    """POST /knowledge-bases/documents/delete — delete a document and its metadata sidecar."""
    if not KB_DOCS_BUCKET:
        return response(500, {"error": "KB_DOCS_BUCKET not configured"})

    body = json.loads(event.get("body") or "{}")
    key = body.get("key", "")

    if not key:
        return response(400, {"error": "key is required"})

    # Delete the document and its metadata sidecar
    objects_to_delete = [{"Key": key}]
    metadata_key = f"{key}.metadata.json"
    objects_to_delete.append({"Key": metadata_key})

    s3_client.delete_objects(
        Bucket=KB_DOCS_BUCKET,
        Delete={"Objects": objects_to_delete},
    )

    return response(200, {"message": f"Document '{key}' deleted"})


def start_kb_sync(event):
    """POST /knowledge-bases/sync — start a data source ingestion job."""
    kb_id, ds_id = _get_kb_config()
    if not kb_id or not ds_id:
        return response(500, {"error": "Knowledge base not initialized. Run scripts/setup-agentcore.py to set up the knowledge base."})

    try:
        resp = bedrock_agent_client.start_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=ds_id,
        )
        job = resp.get("ingestionJob", {})
        return response(200, {
            "message": "Sync started",
            "ingestionJobId": job.get("ingestionJobId", ""),
            "status": job.get("status", ""),
        })
    except Exception as e:
        return response(500, {"error": f"Failed to start sync: {str(e)}"})


def get_kb_sync_status(_event):
    """GET /knowledge-bases/sync — get latest ingestion job status."""
    kb_id, ds_id = _get_kb_config()
    if not kb_id or not ds_id:
        return response(200, {"status": "NOT_INITIALIZED", "jobs": []})

    try:
        resp = bedrock_agent_client.list_ingestion_jobs(
            knowledgeBaseId=kb_id,
            dataSourceId=ds_id,
            maxResults=5,
            sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
        )
        jobs = []
        for job in resp.get("ingestionJobSummaries", []):
            jobs.append({
                "ingestionJobId": job.get("ingestionJobId", ""),
                "status": job.get("status", ""),
                "startedAt": job.get("startedAt", "").isoformat() if hasattr(job.get("startedAt", ""), "isoformat") else str(job.get("startedAt", "")),
                "updatedAt": job.get("updatedAt", "").isoformat() if hasattr(job.get("updatedAt", ""), "isoformat") else str(job.get("updatedAt", "")),
                "statistics": job.get("statistics", {}),
            })
        return response(200, {"status": "OK", "jobs": jobs})
    except Exception as e:
        return response(500, {"error": f"Failed to get sync status: {str(e)}"})


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def handler(event, context):
    logger.info("Event: %s", json.dumps(event, default=str))

    if not check_admin(event):
        return response(403, {"error": "Forbidden: admin group required"})

    method = event.get("httpMethod", "")
    resource = event.get("resource", "")

    # User & tool permission routes
    if resource == "/users" and method == "GET":
        return list_cognito_users(event)
    if resource == "/tools" and method == "GET":
        return list_gateway_tools(event)
    if resource == "/memories" and method == "GET":
        return list_memory_actors(event)
    if resource == "/memories/{actorId}" and method == "GET":
        return get_memory_records(event)
    if resource == "/users/{userId}/permissions" and method == "GET":
        return get_user_permissions(event)
    if resource == "/users/{userId}/permissions" and method == "PUT":
        return update_user_permissions(event)

    # Skill routes
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
    if resource == "/skills/{userId}/{skillName}/files" and method == "GET":
        return list_skill_files(event)
    if resource == "/skills/{userId}/{skillName}/files" and method == "DELETE":
        return delete_skill_file(event)
    if resource == "/skills/{userId}/{skillName}/files/upload-url" and method == "POST":
        return get_upload_url(event)
    if resource == "/skills/{userId}/{skillName}/files/download-url" and method == "POST":
        return get_download_url(event)
    if resource == "/settings/{userId}" and method == "GET":
        return get_settings(event)
    if resource == "/settings/{userId}" and method == "PUT":
        return update_settings(event)
    if resource == "/sessions" and method == "GET":
        return list_sessions(event)
    if resource == "/sessions/{sessionId}/stop" and method == "POST":
        return stop_session(event)

    # Knowledge Base routes (consolidated — action-based dispatch)
    if resource == "/knowledge-bases" and method == "GET":
        action = (event.get("queryStringParameters") or {}).get("action", "status")
        if action == "documents":
            return list_kb_documents(event)
        elif action == "sync-status":
            return get_kb_sync_status(event)
        else:
            return get_kb_status(event)
    if resource == "/knowledge-bases" and method == "POST":
        body = json.loads(event.get("body") or "{}")
        action = body.get("action", "")
        if action == "upload-url":
            return get_kb_upload_url(event)
        elif action == "delete":
            return delete_kb_document(event)
        elif action == "sync":
            return start_kb_sync(event)
        else:
            return response(400, {"error": f"Unknown KB action: {action}"})

    return response(400, {"error": f"Unknown route: {method} {resource}"})
