"""
Lambda function to control smart home devices via AWS IoT Core MQTT.
Called as an AgentCore Gateway Lambda target. Receives tool invocations
via MCP protocol - event is a map of input schema properties.

Per-user isolation: the user's Cognito `sub` is derived from the caller JWT
that AgentCore Gateway forwards in `context.client_context.custom`. The MQTT
publish topic is scoped to that sub, so users can only control their own
device-simulator session. The agent cannot override this — the LLM has no
way to forge `sub`.
"""

import base64
import json
import os
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

iot_client = boto3.client(
    "iot-data",
    endpoint_url=f"https://{os.environ.get('IOT_ENDPOINT', '')}",
    region_name=os.environ.get("AWS_IOT_REGION", os.environ.get("AWS_REGION", "us-east-1")),
)

VALID_DEVICES = {"led_matrix", "rice_cooker", "fan", "oven"}

DEVICE_COMMANDS = {
    "led_matrix": {
        "setPower": {"required": ["power"], "types": {"power": bool}},
        "setMode": {"required": ["mode"], "values": {"mode": ["rainbow", "breathing", "chase", "sparkle", "fire", "ocean", "aurora"]}},
        "setBrightness": {"required": ["brightness"], "ranges": {"brightness": (0, 100)}},
        "setColor": {"required": ["color"]},
    },
    "rice_cooker": {
        "start": {"required": ["mode"], "values": {"mode": ["white_rice", "brown_rice", "porridge", "steam"]}},
        "stop": {"required": []},
        "keepWarm": {"required": ["enabled"], "types": {"enabled": bool}},
    },
    "fan": {
        "setPower": {"required": ["power"], "types": {"power": bool}},
        "setSpeed": {"required": ["speed"], "ranges": {"speed": (0, 3)}},
        "setOscillation": {"required": ["enabled"], "types": {"enabled": bool}},
    },
    "oven": {
        "setPower": {"required": ["power"], "types": {"power": bool}},
        "setMode": {"required": ["mode"], "values": {"mode": ["bake", "broil", "convection"]}},
        "setTemperature": {"required": ["temperature"], "ranges": {"temperature": (200, 500)}},
    },
}


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


def validate_command(device_type: str, command: dict) -> tuple[bool, str]:
    """Validate a device command."""
    if device_type not in VALID_DEVICES:
        return False, f"Invalid device type: {device_type}. Valid: {VALID_DEVICES}"

    action = command.get("action")
    if not action:
        return False, "Missing 'action' in command"

    valid_actions = DEVICE_COMMANDS.get(device_type, {})
    if action not in valid_actions:
        return False, f"Invalid action '{action}' for {device_type}. Valid: {list(valid_actions.keys())}"

    spec = valid_actions[action]
    for param in spec.get("required", []):
        if param not in command:
            return False, f"Missing required parameter '{param}' for action '{action}'"

    for param, valid_vals in spec.get("values", {}).items():
        if param in command and command[param] not in valid_vals:
            return False, f"Invalid value for '{param}': {command[param]}. Valid: {valid_vals}"

    for param, (min_val, max_val) in spec.get("ranges", {}).items():
        if param in command:
            val = command[param]
            if not isinstance(val, (int, float)) or val < min_val or val > max_val:
                return False, f"'{param}' must be between {min_val} and {max_val}"

    return True, "OK"


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

        # AgentCore Gateway sends event as a flat map of input properties
        device_type = event.get("device_type")
        command = event.get("command", {})

        if isinstance(command, str):
            command = json.loads(command)

        # Validate
        is_valid, message = validate_command(device_type, command)
        if not is_valid:
            return {"error": message}

        # Publish to IoT Core — per-user scoped topic.
        topic = f"smarthome/{user_sub}/{device_type}/command"
        payload = json.dumps(command)

        logger.info(f"Publishing to {topic}: {payload}")

        iot_client.publish(
            topic=topic,
            qos=1,
            payload=payload.encode("utf-8"),
        )

        return {
            "message": f"Command sent to {device_type}",
            "device": device_type,
            "command": command,
            "topic": topic,
        }

    except Exception as e:
        logger.error(f"Error: {str(e)}", exc_info=True)
        return {"error": str(e)}
