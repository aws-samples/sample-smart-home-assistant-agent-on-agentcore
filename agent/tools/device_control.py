import json
import os
import boto3
from strands.types.tools import ToolResult, ToolUse

GATEWAY_URL = os.environ.get("AGENTCORE_GATEWAY_URL", "")

lambda_client = boto3.client("lambda")

def device_control_tool(tool: ToolUse) -> ToolResult:
    """Send control command to a smart home device via the IoT control Lambda."""
    device_type = tool.input.get("device_type")
    command = tool.input.get("command")

    # Invoke the Lambda function directly
    response = lambda_client.invoke(
        FunctionName=os.environ.get("IOT_CONTROL_LAMBDA_NAME", ""),
        InvocationType="RequestResponse",
        Payload=json.dumps({
            "device_type": device_type,
            "command": command
        })
    )

    result = json.loads(response["Payload"].read())

    return {
        "toolUseId": tool.toolUseId,
        "status": "success",
        "content": [{"text": json.dumps(result)}]
    }

DEVICE_CONTROL_TOOL_SPEC = {
    "name": "device_control",
    "description": "Send a control command to a smart home device. Supported devices: led_matrix, rice_cooker, fan, oven. Each device accepts specific commands.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "device_type": {
                "type": "string",
                "enum": ["led_matrix", "rice_cooker", "fan", "oven"],
                "description": "The type of device to control"
            },
            "command": {
                "type": "object",
                "description": "The command to send. For led_matrix: {action: 'setMode'|'setPower'|'setBrightness'|'setColor', ...params}. For rice_cooker: {action: 'start'|'stop'|'keepWarm', ...params}. For fan: {action: 'setPower'|'setSpeed'|'setOscillation', ...params}. For oven: {action: 'setMode'|'setTemperature'|'setPower', ...params}."
            }
        },
        "required": ["device_type", "command"]
    }
}
