"""
Lambda function to discover available smart home devices.
Called as an AgentCore Gateway Lambda target.

Returns the shared device catalog (copied in next to this file at build time by
scripts/01-install-deps.sh) so the agent sees one definition of the fleet rather
than a second hardcoded list that drifts from the simulator's.

The response includes each device's `capabilities` — ranges, enums and units —
so the agent can pick valid parameters instead of guessing and being rejected.
`deviceType` and `powerOn`/`powerOff` are preserved because
agent/voice_session.py's turn_on_all_devices replays exactly those fields.

The list is the same for every user; the caller's `sub` is echoed back so the
chatbot / voice agent can confirm which simulator session it is addressing.
"""

import base64
import json
import logging

import device_catalog

logger = logging.getLogger()
logger.setLevel(logging.INFO)

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

    devices = device_catalog.discovery_payload()
    return {
        "userId": user_sub,
        "devices": devices,
        "count": len(devices),
        "rooms": device_catalog.load_catalog().get("rooms", {}),
    }
