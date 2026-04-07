"""
Lambda function to discover available smart home devices.
Called as an AgentCore Gateway Lambda target.
Returns a mock list of IoT things registered for the user.
"""

import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MOCK_DEVICES = [
    {
        "thingName": "smarthome-led_matrix",
        "deviceType": "led_matrix",
        "displayName": "LED Matrix",
        "actions": ["setPower", "setMode", "setBrightness", "setColor"],
        "powerOn": {"action": "setPower", "power": True},
        "powerOff": {"action": "setPower", "power": False},
    },
    {
        "thingName": "smarthome-rice_cooker",
        "deviceType": "rice_cooker",
        "displayName": "Rice Cooker",
        "actions": ["start", "stop", "keepWarm"],
        "powerOn": {"action": "start", "mode": "white_rice"},
        "powerOff": {"action": "stop"},
    },
    {
        "thingName": "smarthome-fan",
        "deviceType": "fan",
        "displayName": "Fan",
        "actions": ["setPower", "setSpeed", "setOscillation"],
        "powerOn": {"action": "setPower", "power": True},
        "powerOff": {"action": "setPower", "power": False},
    },
    {
        "thingName": "smarthome-oven",
        "deviceType": "oven",
        "displayName": "Oven",
        "actions": ["setPower", "setMode", "setTemperature"],
        "powerOn": {"action": "setPower", "power": True},
        "powerOff": {"action": "setPower", "power": False},
    },
]


def handler(event, context):
    logger.info(f"Received event: {json.dumps(event, default=str)}")

    return {
        "devices": MOCK_DEVICES,
        "count": len(MOCK_DEVICES),
    }
