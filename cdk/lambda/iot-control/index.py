"""
Lambda function to control smart home devices via AWS IoT Core MQTT.
Called as an AgentCore Gateway Lambda target. Receives tool invocations
via MCP protocol - event is a map of input schema properties.
"""

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
        tool_name = None
        if hasattr(context, "client_context") and context.client_context and hasattr(context.client_context, "custom"):
            custom = context.client_context.custom
            if isinstance(custom, dict):
                original_tool_name = custom.get("bedrockAgentCoreToolName", "")
                delimiter = "___"
                if delimiter in original_tool_name:
                    tool_name = original_tool_name[original_tool_name.index(delimiter) + len(delimiter):]
                else:
                    tool_name = original_tool_name
                logger.info(f"Tool name: {tool_name}")

        # AgentCore Gateway sends event as a flat map of input properties
        device_type = event.get("device_type")
        command = event.get("command", {})

        if isinstance(command, str):
            command = json.loads(command)

        # Validate
        is_valid, message = validate_command(device_type, command)
        if not is_valid:
            return {"error": message}

        # Publish to IoT Core
        topic = f"smarthome/{device_type}/command"
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
