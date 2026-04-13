"""Cognito Post-Confirmation trigger — auto-provision all gateway tool
permissions for newly confirmed users.

Keeps the default-deny Cedar policy model: each user is explicitly added to
per-tool permit statements so the AgentCore Gateway authorises their requests.
"""

import json
import os
import logging
import time
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("SKILLS_TABLE_NAME", "smarthome-skills")
GATEWAY_ID = os.environ.get("GATEWAY_ID", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
s3_client = boto3.client("s3", region_name=REGION)
agentcore_control = boto3.client("bedrock-agentcore-control", region_name=REGION)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Cedar / Gateway helpers (mirrors admin-api logic)
# ---------------------------------------------------------------------------

def _get_gateway_arn():
    if not hasattr(_get_gateway_arn, "_cache"):
        gw = agentcore_control.get_gateway(gatewayIdentifier=GATEWAY_ID)
        _get_gateway_arn._cache = gw.get("gatewayArn", "")
    return _get_gateway_arn._cache


def _get_tool_action_map():
    """Build a map of tool_name -> Cedar action name ({TargetName}___{toolName})."""
    if not hasattr(_get_tool_action_map, "_cache"):
        action_map = {}
        targets = agentcore_control.list_gateway_targets(gatewayIdentifier=GATEWAY_ID)
        for t in targets.get("items", []):
            target_name = t.get("name", "")
            try:
                target = agentcore_control.get_gateway_target(
                    gatewayIdentifier=GATEWAY_ID, targetId=t["targetId"]
                )
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


def ensure_policy_engine():
    """Get or create the policy engine. Returns (policyEngineId, policyEngineArn)."""
    resp = table.get_item(Key={"userId": "__system__", "skillName": "__policy_engine__"})
    item = resp.get("Item")
    if item and item.get("policyEngineId"):
        return item["policyEngineId"], item.get("policyEngineArn", "")

    try:
        create_resp = agentcore_control.create_policy_engine(
            name="SmartHomeUserPermissions",
            description="Per-user tool access control for SmartHome Gateway",
        )
        engine_id = create_resp["policyEngineId"]
        engine_arn = create_resp.get("policyEngineArn", "")
    except agentcore_control.exceptions.ConflictException:
        list_resp = agentcore_control.list_policy_engines()
        for eng in list_resp.get("policyEngines", []):
            if eng.get("name") == "SmartHomeUserPermissions":
                engine_id = eng["policyEngineId"]
                engine_arn = eng.get("policyEngineArn", "")
                break
        else:
            raise Exception("Policy engine conflict but could not find existing engine")

    for _ in range(15):
        try:
            get_resp = agentcore_control.get_policy_engine(policyEngineId=engine_id)
            if get_resp.get("status") == "ACTIVE":
                engine_arn = get_resp.get("policyEngineArn", engine_arn)
                break
        except Exception:
            pass
        time.sleep(2)

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
        return

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
            time.sleep(10)
        except Exception as e:
            logger.warning(f"Failed to grant PolicyEngineAccess to gateway role: {e}")

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


def build_cedar_statement(tool_name, user_ids):
    """Build a Cedar permit statement for a tool with per-user access control."""
    action_map = _get_tool_action_map()
    action_name = action_map.get(tool_name, tool_name)
    gateway_arn = _get_gateway_arn()

    if not user_ids:
        return ""

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
    """Scan DynamoDB for all users with this tool and create/update the Cedar policy."""
    engine_id, engine_arn = ensure_policy_engine()
    ensure_gateway_policy_engine(engine_arn)

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

    policy_record = table.get_item(
        Key={"userId": "__system__", "skillName": f"__tool_policy_{tool_name}__"}
    ).get("Item")
    existing_policy_id = policy_record.get("policyId") if policy_record else None

    policy_name = f"ToolPolicy_{tool_name.replace('-', '_')}"
    cedar_stmt = build_cedar_statement(tool_name, user_ids)

    if not cedar_stmt:
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
        agentcore_control.update_policy(
            policyEngineId=engine_id,
            policyId=existing_policy_id,
            definition={"cedar": {"statement": cedar_stmt}},
            validationMode="IGNORE_ALL_FINDINGS",
        )
        logger.info(f"Updated policy {existing_policy_id} for tool '{tool_name}' "
                     f"with {len(user_ids)} users")
    else:
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
        logger.info(f"Created policy {new_policy_id} for tool '{tool_name}' "
                     f"with {len(user_ids)} users")


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(event, context):
    """Cognito Post-Confirmation trigger handler."""
    logger.info("Event: %s", json.dumps(event, default=str))

    trigger_source = event.get("triggerSource", "")
    if trigger_source != "PostConfirmation_ConfirmSignUp":
        logger.info(f"Ignoring trigger source: {trigger_source}")
        return event

    user_sub = event.get("request", {}).get("userAttributes", {}).get("sub", "")
    username = event.get("userName", "")

    if not user_sub:
        logger.warning(f"No sub in trigger event for user '{username}', skipping")
        return event

    if not GATEWAY_ID or GATEWAY_ID == "PLACEHOLDER_SET_BY_SETUP_SCRIPT":
        logger.warning("GATEWAY_ID not configured, skipping auto-provision")
        return event

    # Don't overwrite manually-configured permissions
    existing = table.get_item(
        Key={"userId": user_sub, "skillName": "__permissions__"}
    ).get("Item")
    if existing:
        logger.info(f"User '{username}' (sub={user_sub}) already has permissions, skipping")
        return event

    try:
        action_map = _get_tool_action_map()
        all_tools = list(action_map.keys())

        if not all_tools:
            logger.warning("No gateway tools found, skipping auto-provision")
            return event

        table.put_item(Item={
            "userId": user_sub,
            "skillName": "__permissions__",
            "allowedTools": all_tools,
            "updatedAt": now_iso(),
        })
        logger.info(
            f"Auto-provisioned {len(all_tools)} tools for user '{username}' (sub={user_sub})"
        )

        for tool_name in all_tools:
            try:
                rebuild_tool_policy(tool_name)
            except Exception as e:
                logger.error(f"Failed to rebuild policy for tool '{tool_name}': {e}")

    except Exception as e:
        logger.error(f"Auto-provision failed for user '{username}': {e}")

    return event
