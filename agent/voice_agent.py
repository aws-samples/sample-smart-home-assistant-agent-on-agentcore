"""Voice-only AgentCore Runtime entrypoint.

This entrypoint runs on a dedicated AgentCore Runtime (`smarthome-voice`). It
serves the `/ws` WebSocket path for Nova Sonic bi-directional streaming and a
short-circuit `/invocations` path that exists only so the chatbot's post-login
warmup can pre-heat this runtime's Python process (triggering the eager
imports below).

Why a separate runtime: see docs/superpowers/specs/2026-04-23-voice-agent-split-design.md.

ADOT auto-instrumentation is disabled here — voice sessions are long streams
where per-event spans add little triage value, and `sitecustomize.initialize()`
adds 100-300ms to cold start. The DISABLE_ADOT env var is also set by
setup-agentcore.py on the runtime; the line below is belt-and-braces for
local-dev runs.
"""
import os

os.environ.setdefault("DISABLE_ADOT", "1")

# Eager imports: the login-time `POST /invocations {"prompt":"__warmup__"}` ping
# lands on this process and triggers Python module init for everything below,
# so the first real `/ws` handshake doesn't pay the strands.bidi import cost.
from strands.experimental.bidi.agent import BidiAgent  # noqa: F401
from strands.experimental.bidi.models.nova_sonic import BidiNovaSonicModel  # noqa: F401
from strands.tools.mcp.mcp_client import MCPClient  # noqa: F401
from mcp.client.streamable_http import streamablehttp_client  # noqa: F401

import logging

from bedrock_agentcore import BedrockAgentCoreApp

# Reuse helpers from the text runtime's module (single-package, two-entrypoint
# layout — change-in-one-place wins even though the voice runtime doesn't need
# the BedrockModel / AgentSkills code paths).
from agent import (
    AWS_REGION,
    GATEWAY_ARN,
    _get_dynamodb,
    SKILLS_TABLE_NAME,
)

import boto3

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = BedrockAgentCoreApp()


def _preheat_boto3_clients() -> None:
    """Force boto3 client init + endpoint resolution + connection pool setup
    so the first /ws handshake doesn't pay it.

    `boto3.client("...")` lazily:
      (1) resolves the service endpoint from a JSON model in botocore
      (2) parses the IAM role credentials from the runtime's exec role
      (3) builds the urllib3 HTTPS connection pool (no actual socket yet)
    These add ~50-200ms on first use. A cheap API call forces all three plus
    the TLS handshake, leaving the client warm for later calls.
    """
    try:
        ddb = _get_dynamodb()  # shared resource cached in agent.py
        if SKILLS_TABLE_NAME:
            # Minimal read that requires no specific item. ProjectionExpression
            # limits the payload to practically nothing.
            ddb.Table(SKILLS_TABLE_NAME).scan(Limit=1, ProjectionExpression="userId")
        logger.info("Voice warmup: DynamoDB client preheated")
    except Exception as e:
        logger.warning(f"Voice warmup: DynamoDB preheat failed: {e}")

    # Bedrock-runtime is what Nova Sonic's bi-directional stream calls.
    # `describe_endpoint`-style no-op isn't available; use list_foundation_models
    # on the control-plane (cheap, read-only) to force credential + endpoint
    # resolution and TLS handshake on the same regional Bedrock endpoint.
    try:
        boto3.client("bedrock", region_name=AWS_REGION).list_foundation_models(byOutputModality="TEXT")
        logger.info("Voice warmup: Bedrock control-plane client preheated")
    except Exception as e:
        logger.warning(f"Voice warmup: Bedrock preheat failed: {e}")


@app.entrypoint
def handle_invocation(payload, context):
    """Voice runtime only serves /ws; /invocations is warmup-only.

    The chatbot fires a SigV4-signed `POST /invocations {"prompt":"__warmup__"}`
    right after login to pre-heat this container — any non-warmup request is an
    error (text chat belongs on the `smarthome` runtime).
    """
    if payload.get("prompt") == "__warmup__":
        logger.info("Voice runtime warmup OK")
        _preheat_boto3_clients()
        return {"status": "warmup_ok"}
    return {
        "error": "voice runtime only serves /ws; use the text runtime for text chat",
    }


@app.websocket
async def ws_voice(websocket, context):
    """GET /ws handler — bridges browser mic → Nova Sonic bi-directional stream."""
    from voice_session import handle_voice_session
    await handle_voice_session(
        websocket,
        context,
        gateway_arn=GATEWAY_ARN,
        region=AWS_REGION,
    )


if __name__ == "__main__":
    # Force the 'websockets' ASGI WS implementation (see agent.py for the
    # managed-runtime regression this guards against).
    app.run(log_level="info", ws="websockets")
