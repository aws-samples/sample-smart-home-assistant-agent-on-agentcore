import os
import json
import logging

# ADOT auto-instrumentation: programmatic equivalent of `opentelemetry-instrument python agent.py`.
# Must run before any other imports so ADOT can patch libraries (botocore, requests, etc.).
# On AgentCore managed runtime, ADOT exports spans to CloudWatch automatically.
os.environ.setdefault("OTEL_PYTHON_DISTRO", "aws_distro")
os.environ.setdefault("OTEL_PYTHON_CONFIGURATOR", "aws_configurator")
from opentelemetry.instrumentation.auto_instrumentation import sitecustomize  # noqa: E402
sitecustomize.initialize()

from strands import Agent, AgentSkills  # noqa: E402
from strands.vended_plugins.skills import Skill  # noqa: E402
from strands.models.bedrock import BedrockModel  # noqa: E402
from strands.tools.mcp.mcp_client import MCPClient  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402
from strands.telemetry import StrandsTelemetry  # noqa: E402
from bedrock_agentcore import BedrockAgentCoreApp  # noqa: E402
from memory.session import get_memory_session_manager  # noqa: E402

import boto3  # noqa: E402
from boto3.dynamodb.conditions import Key  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configure Strands to emit OTEL traces (GenAI semantic conventions).
StrandsTelemetry()

# agentcore CLI sets env vars as AGENTCORE_GATEWAY_{GATEWAYNAME}_URL / _ARN
GATEWAY_URL = os.environ.get("AGENTCORE_GATEWAY_URL", "")
GATEWAY_ARN = os.environ.get("AGENTCORE_GATEWAY_ARN", "")
if not GATEWAY_URL:
    for key, val in os.environ.items():
        if key.startswith("AGENTCORE_GATEWAY_") and key.endswith("_URL"):
            GATEWAY_URL = val
        elif key.startswith("AGENTCORE_GATEWAY_") and key.endswith("_ARN"):
            GATEWAY_ARN = val

MODEL_ID = os.environ.get("MODEL_ID", "moonshotai.kimi-k2.5")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
SKILLS_TABLE_NAME = os.environ.get("SKILLS_TABLE_NAME", "")

app = BedrockAgentCoreApp()

_dynamodb_resource = None


def _get_dynamodb():
    global _dynamodb_resource
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb", region_name=AWS_REGION)
    return _dynamodb_resource


def load_skills_from_dynamodb(actor_id: str) -> list:
    """Load global + user-specific skills from DynamoDB, return list of Skill instances."""
    table = _get_dynamodb().Table(SKILLS_TABLE_NAME)
    skills_by_name = {}

    resp = table.query(KeyConditionExpression=Key("userId").eq("__global__"))
    for item in resp.get("Items", []):
        allowed_tools = item.get("allowedTools")
        if isinstance(allowed_tools, set):
            allowed_tools = list(allowed_tools)
        skills_by_name[item["skillName"]] = Skill(
            name=item["skillName"],
            description=item.get("description", ""),
            instructions=item.get("instructions", ""),
            allowed_tools=allowed_tools,
            license=item.get("license"),
            compatibility=item.get("compatibility"),
            metadata=item.get("metadata") or {},
        )

    if actor_id and actor_id not in ("default", "__global__"):
        resp = table.query(KeyConditionExpression=Key("userId").eq(actor_id))
        for item in resp.get("Items", []):
            allowed_tools = item.get("allowedTools")
            if isinstance(allowed_tools, set):
                allowed_tools = list(allowed_tools)
            skills_by_name[item["skillName"]] = Skill(
                name=item["skillName"],
                description=item.get("description", ""),
                instructions=item.get("instructions", ""),
                allowed_tools=allowed_tools,
                license=item.get("license"),
                compatibility=item.get("compatibility"),
                metadata=item.get("metadata") or {},
            )

    return list(skills_by_name.values())


def load_user_settings(actor_id: str) -> dict:
    table = _get_dynamodb().Table(SKILLS_TABLE_NAME)
    for uid in [actor_id, "__global__"]:
        if uid in ("default", ""):
            continue
        resp = table.get_item(Key={"userId": uid, "skillName": "__settings__"})
        item = resp.get("Item")
        if item and item.get("modelId"):
            return {"modelId": item["modelId"]}
    return {}


def load_system_prompt(actor_id: str, agent_type: str) -> str | None:
    """Resolve the active system prompt for this user/agent from DynamoDB.

    Additive resolution: the global record and the per-user record are
    concatenated ("global\\n\\nuser"), each record being independently
    editable in the admin console. Global typically holds shared guardrails;
    the per-user record is an addendum with user-specific personalization.

    agent_type ∈ {"text", "voice"}. Sort keys are `__prompt_text__` /
    `__prompt_voice__`. Returns the concatenation when at least one record
    exists; None when both are empty (callers then fall back to the hardcoded
    constant in agent.py / voice_session.py).
    """
    if not SKILLS_TABLE_NAME:
        return None
    sk = f"__prompt_{agent_type}__"
    table = _get_dynamodb().Table(SKILLS_TABLE_NAME)

    def _read(uid: str) -> str:
        if uid in ("default", ""):
            return ""
        resp = table.get_item(Key={"userId": uid, "skillName": sk})
        item = resp.get("Item")
        body = item.get("promptBody") if item else None
        return body.strip() if isinstance(body, str) else ""

    global_body = _read("__global__")
    user_body = _read(actor_id) if actor_id != "__global__" else ""

    parts = [p for p in (global_body, user_body) if p]
    return "\n\n".join(parts) if parts else None


_static_skills_plugin = AgentSkills(skills="./skills/")


SYSTEM_PROMPT = """You are a smart home assistant that controls devices in the user's home.
Be helpful, concise, and confirm actions taken. If a user asks to do something, use the appropriate device control tool.
You can also suggest creative lighting scenes, cooking presets, and comfort settings.
Use what you remember about the user's preferences to personalize your responses.
CRITICAL RULE — TOOL CALLING: When the user asks you to perform ANY action on devices (turn on, turn off, set mode, change settings, etc.), you MUST immediately call the appropriate tool in your VERY FIRST response. Do NOT describe what you plan to do, do NOT explain your steps, do NOT narrate your intentions — just call the tool directly. Action requests require tool calls, not text descriptions of tool calls.
IMPORTANT: Always send the device control command when the user asks, even if you believe the device is already in the requested state. You do not have real-time device state — always execute the command.
IMPORTANT: Never fabricate or assume the result of a tool call. If a tool call fails, is rejected, or returns an error, you MUST honestly report the failure to the user. Do not pretend the action succeeded. Tell the user what went wrong and suggest they contact an administrator if the issue persists.
IMPORTANT: Do NOT list or describe devices from your own knowledge. You MUST use the discover_devices tool to find available devices. If the tool is unavailable or fails, tell the user you cannot access device information and suggest they contact an administrator.
KNOWLEDGE BASE: You have access to an enterprise knowledge base. When users ask questions that may relate to company documents, product manuals, troubleshooting guides, or internal knowledge, use the query_knowledge_base tool to retrieve relevant information. Cite the source document when presenting information from the knowledge base."""


def create_agent(tools=None, session_manager=None, skills=None, model_id=None, system_prompt=None):
    model = BedrockModel(
        model_id=model_id or MODEL_ID,
        region_name=AWS_REGION,
        streaming=True,
    )

    if skills:
        skills_plugin = AgentSkills(skills=skills)
    else:
        skills_plugin = _static_skills_plugin

    agent_kwargs = dict(
        model=model,
        system_prompt=system_prompt or SYSTEM_PROMPT,
        plugins=[skills_plugin],
    )

    if tools:
        agent_kwargs["tools"] = tools
    if session_manager:
        agent_kwargs["session_manager"] = session_manager

    return Agent(**agent_kwargs)


def get_mcp_tools(mcp_client):
    tools = []
    pagination_token = None
    while True:
        result = mcp_client.list_tools_sync(pagination_token=pagination_token)
        tools.extend(result)
        if result.pagination_token is None:
            break
        pagination_token = result.pagination_token
    return tools


def invoke_agent(prompt, session_id="default", actor_id="default", auth_header=None):
    session_manager = get_memory_session_manager(session_id, actor_id)

    skills = None
    user_model_id = None
    user_system_prompt = None
    if SKILLS_TABLE_NAME:
        try:
            skills = load_skills_from_dynamodb(actor_id)
        except Exception as e:
            logger.warning(f"DynamoDB skill load failed, using filesystem fallback: {e}")
        try:
            settings = load_user_settings(actor_id)
            user_model_id = settings.get("modelId") or None
            if user_model_id:
                logger.info(f"Using per-user model: {user_model_id} for actor {actor_id}")
        except Exception as e:
            logger.warning(f"Failed to load user settings: {e}")
        try:
            user_system_prompt = load_system_prompt(actor_id, "text")
            if user_system_prompt:
                logger.info(f"Using per-user/global text system prompt override for actor {actor_id}")
        except Exception as e:
            logger.warning(f"Failed to load system prompt override, using default: {e}")

    if GATEWAY_URL:
        gw_headers = {}
        if auth_header:
            gw_headers["Authorization"] = auth_header
            logger.info("Forwarding user JWT to gateway for policy evaluation")
        else:
            logger.warning("No Authorization header available — gateway per-user policies won't apply")
        mcp_client = MCPClient(lambda: streamablehttp_client(GATEWAY_URL, headers=gw_headers or None))
        with mcp_client:
            mcp_tools = get_mcp_tools(mcp_client)

            kb_tool_present = any(t.tool_name == "query_knowledge_base" for t in mcp_tools)
            if kb_tool_present:
                non_kb_tools = [t for t in mcp_tools if t.tool_name != "query_knowledge_base"]
                kb_user_id = actor_id if actor_id not in ("default", "__global__", "") else None

                from strands import tool as strands_tool

                @strands_tool
                def query_knowledge_base(query: str) -> str:
                    """Query the enterprise knowledge base to retrieve relevant documents.
                    Use this when users ask about company documents, product manuals,
                    troubleshooting guides, or internal knowledge."""
                    import uuid
                    args = {"query": query}
                    if kb_user_id:
                        args["user_id"] = kb_user_id
                    result = mcp_client.call_tool_sync(
                        tool_use_id=str(uuid.uuid4()),
                        name="query_knowledge_base",
                        arguments=args,
                    )
                    if hasattr(result, 'content') and result.content:
                        texts = [c.text for c in result.content if hasattr(c, 'text')]
                        return "\n".join(texts) if texts else json.dumps(result.content, default=str)
                    return str(result)

                all_tools = non_kb_tools + [query_knowledge_base]
                logger.info(f"KB tool wrapped with user_id={kb_user_id} (LLM cannot override)")
            else:
                all_tools = mcp_tools

            agent = create_agent(tools=all_tools, session_manager=session_manager, skills=skills, model_id=user_model_id, system_prompt=user_system_prompt)
            return str(agent(prompt))
    else:
        agent = create_agent(session_manager=session_manager, skills=skills, model_id=user_model_id, system_prompt=user_system_prompt)
        return str(agent(prompt))


def _extract_user_auth(context) -> str | None:
    """Read the chatbot-supplied idToken from the custom allowlisted header and
    format it as a Bearer header for downstream gateway MCP calls.

    Header name: X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken.
    We also keep the legacy Authorization-header path for local-dev invocations
    where the header is passed directly.
    """
    rh = getattr(context, "request_headers", None)
    if not rh:
        return None
    headers = rh

    # Custom header (current prod path under AWS_IAM auth). Search
    # case-insensitively across whatever case the runtime forwards.
    lowered = {str(k).lower(): v for k, v in headers.items()} if hasattr(headers, "items") else {}
    token = lowered.get("x-amzn-bedrock-agentcore-runtime-custom-authtoken")
    if token:
        return f"Bearer {token}" if not token.lower().startswith("bearer ") else token

    # Legacy: raw Authorization header (still used in some dev flows).
    return lowered.get("authorization")


def _record_session(actor_id: str, session_id: str) -> None:
    if not SKILLS_TABLE_NAME:
        return
    try:
        from datetime import datetime, timezone
        _get_dynamodb().Table(SKILLS_TABLE_NAME).put_item(Item={
            "userId": actor_id,
            "skillName": "__session__",
            "sessionId": session_id,
            "lastActiveAt": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.warning(f"Failed to record session: {e}")


@app.entrypoint
def handle_invocation(payload, context):
    """POST /invocations handler — text path (synchronous request/response)."""
    prompt = payload.get("prompt", payload.get("inputText", ""))
    if not prompt:
        return {"error": "No prompt provided"}

    session_id = "default"
    if hasattr(context, "session_id") and context.session_id:
        session_id = context.session_id

    actor_id = payload.get("userId", "default")

    # Under AWS_IAM auth on the runtime, the `Authorization` header can't be
    # passthrough-allowlisted, so the chatbot sends the user's idToken in a
    # custom allowlisted header instead. We forward it to the CUSTOM_JWT gateway
    # MCP client as `Bearer <token>` for per-user Cedar policy evaluation.
    auth_header = _extract_user_auth(context)

    # Warm-up ping sent right after login — proves the runtime + JWT are healthy
    # without burning an LLM turn.
    if prompt == "__warmup__":
        logger.info(f"Warmup invocation: actor_id={actor_id}, session_id={session_id}")
        return {"status": "warmup_ok"}

    logger.info(f"Invocation: actor_id={actor_id}, session_id={session_id}")
    _record_session(actor_id, session_id)

    response = invoke_agent(prompt, session_id=session_id, actor_id=actor_id, auth_header=auth_header)
    return {"response": response, "status": "success"}


@app.websocket
async def ws_voice(websocket, context):
    """GET /ws handler — voice mode. Bridges the browser to Nova Sonic."""
    # Lazy import so the HTTP path doesn't pay for BidiAgent imports at cold start.
    from voice_session import handle_voice_session
    await handle_voice_session(
        websocket,
        context,
        gateway_arn=GATEWAY_ARN,
        region=AWS_REGION,
    )


if __name__ == "__main__":
    # Force the 'websockets' ASGI WS implementation. On the managed runtime we
    # observed uvicorn's default auto-detection picking a no-op WS handler,
    # causing WebSocket upgrade requests to be rejected with 400 Bad Request
    # even though the `websockets` library was installed.
    app.run(log_level="info", ws="websockets")
