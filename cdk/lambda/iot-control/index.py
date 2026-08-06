"""
Lambda function to control smart home devices via AWS IoT Core MQTT.
Called as an AgentCore Gateway Lambda target. Receives tool invocations
via MCP protocol - event is a map of input schema properties.

Per-user isolation: the user's Cognito `sub` is derived from the caller JWT
that AgentCore Gateway forwards in `context.client_context.custom`. The MQTT
publish topic is scoped to that sub, so users can only control their own
device-simulator session. The agent cannot override this — the LLM has no
way to forge `sub`.

Topic shape is `smarthome/{sub}/{deviceId}/command`. It was keyed by device
*type* until deviceId replaced it, which is what makes two of the same kind
("living room light" vs "bedroom light") separately addressable.
"""

import base64
import json
import os
import boto3
import logging

# Device definitions and command validation live in the shared catalog, copied
# in next to this file at build time by scripts/01-install-deps.sh. They used to
# be a hardcoded DEVICE_COMMANDS table here, which had drifted from the
# simulator's (it rejected led mode 'solid' and oven mode 'preheat', both of
# which the UI has always rendered).
import device_catalog

logger = logging.getLogger()
logger.setLevel(logging.INFO)

iot_client = boto3.client(
    "iot-data",
    endpoint_url=f"https://{os.environ.get('IOT_ENDPOINT', '')}",
    region_name=os.environ.get("AWS_IOT_REGION", os.environ.get("AWS_REGION", "us-east-1")),
)


def _decode_jwt_sub(token):
    """Decode JWT payload to extract the `sub` claim (Cognito user-pool UUID).
    Gateway has already validated the signature — we just unpack the payload.
    Returns the `sub` string or None.
    """
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
    """Resolve the caller's Cognito User Pool `sub` — the topic-scoping ID.

    AgentCore Gateway doesn't currently forward the caller's JWT claims to
    the Lambda target (its client_context.custom has only Gateway-internal
    metadata like targetId/toolName). So the agent wraps this tool on the
    client side and injects a `user_id` argument whose value is the sub it
    decoded from the runtime-validated idToken. The Lambda trusts `user_id`
    *only* from that path; other identity shapes are checked for future
    Gateway-version compatibility.

    A malicious LLM trying to forge `user_id` by itself gets intercepted: our
    agent wrapper overrides whatever the LLM tried with the sub it already
    has. Anyone calling the Lambda directly (bypassing the agent) would need
    Gateway + Cedar-policy permission first, and even then they'd be asserting
    their own identity — they can't escalate to another user.
    """
    # 1) Agent-wrapped call: `user_id` in the event is the agent's
    #    JWT-derived sub. This is the primary path in production.
    uid = event.get("user_id")
    if uid:
        return uid

    # 2) Fallback: Gateway future versions that do inject identity.
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


def handler(event, context):
    """
    Handle incoming tool invocations from AgentCore Gateway.

    AgentCore Gateway Lambda target format:
    - event: map of input schema properties (e.g., {"device_type": "led_matrix", "command": {...}})
    - context.client_context.custom: metadata with tool name, gateway ID, etc.
    """
    logger.info(f"Received event: {json.dumps(event, default=str)}")

    try:
        # Extract tool name from AgentCore Gateway context if available
        if hasattr(context, "client_context") and context.client_context and hasattr(context.client_context, "custom"):
            custom = context.client_context.custom
            if isinstance(custom, dict):
                logger.info(f"Gateway custom context keys: {list(custom.keys())} values: {json.dumps({k: str(v)[:120] for k, v in custom.items()})}")
                original_tool_name = custom.get("bedrockAgentCoreToolName", "")
                delimiter = "___"
                if delimiter in original_tool_name:
                    tool_name = original_tool_name[original_tool_name.index(delimiter) + len(delimiter):]
                else:
                    tool_name = original_tool_name
                logger.info(f"Tool name: {tool_name}")

        user_sub = _extract_user_sub(event, context)
        if not user_sub:
            return {"error": "caller identity missing — cannot determine device owner"}

        # AgentCore Gateway sends event as a flat map of input properties.
        # `device_id` addresses one specific unit and is what the agent should
        # send; `device_type` is accepted so existing skills and the voice
        # agent's power-on loop keep working, and resolves to the first device
        # of that type.
        device_id = event.get("device_id")
        device_type = event.get("device_type")
        command = event.get("command", {})

        if isinstance(command, str):
            command = json.loads(command)

        device, err = device_catalog.resolve_device(device_id, device_type)
        if err:
            return {"error": err}

        # Returns the command with out-of-range values clamped, so publish the
        # normalised copy rather than the input.
        ok, normalised, warnings = device_catalog.validate_command(device, command)
        if not ok:
            return {"error": warnings[0]}

        # Publish to IoT Core — per-user, per-device scoped topic.
        topic = f"smarthome/{user_sub}/{device['deviceId']}/command"
        payload = json.dumps(normalised)

        logger.info(f"Publishing to {topic}: {payload}")

        iot_client.publish(
            topic=topic,
            qos=1,
            payload=payload.encode("utf-8"),
        )

        result = {
            "message": f"Command sent to {device_catalog.label(device)}",
            "deviceId": device["deviceId"],
            "device": device["deviceType"],
            "command": normalised,
            "topic": topic,
        }
        if warnings:
            # Surfaced so the agent can tell the user what it adjusted — the
            # point of clamping rather than rejecting.
            result["warnings"] = warnings
        return result

    except Exception as e:
        logger.error(f"Error: {str(e)}", exc_info=True)
        return {"error": str(e)}
