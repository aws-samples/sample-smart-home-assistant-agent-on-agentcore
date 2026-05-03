"""
Lambda function to discover available smart home devices.
Called as an AgentCore Gateway Lambda target.
Returns the mock device list that corresponds to the caller's own IoT topic
scope. The user's Cognito `sub` is derived from the Gateway-forwarded JWT;
the device list itself is static per user (same four devices) but the
response includes the user's sub so the chatbot / voice agent can confirm
which device-simulator session they are addressing.
"""

import base64
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


def _decode_jwt_sub(token):
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
    # Primary path: agent-wrapped call injected the runtime-validated sub.
    uid = event.get("user_id")
    if uid:
        return uid
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
    logger.info(f"Received event: {json.dumps(event, default=str)}")

    user_sub = _extract_user_sub(event, context)
    if not user_sub:
        return {"error": "caller identity missing — cannot list devices for unknown user"}

    return {
        "userId": user_sub,
        "devices": MOCK_DEVICES,
        "count": len(MOCK_DEVICES),
    }
