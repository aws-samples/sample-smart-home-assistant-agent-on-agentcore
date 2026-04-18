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
# ADOT auto-instrumentation (above) handles exporting these spans to CloudWatch.
StrandsTelemetry()

# agentcore CLI sets env vars as AGENTCORE_GATEWAY_{GATEWAYNAME}_URL
GATEWAY_URL = os.environ.get("AGENTCORE_GATEWAY_URL", "")
if not GATEWAY_URL:
    for key, val in os.environ.items():
        if key.startswith("AGENTCORE_GATEWAY_") and key.endswith("_URL"):
            GATEWAY_URL = val
            break
MODEL_ID = os.environ.get("MODEL_ID", "moonshotai.kimi-k2.5")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
SKILLS_TABLE_NAME = os.environ.get("SKILLS_TABLE_NAME", "")

app = BedrockAgentCoreApp()

# DynamoDB resource for skill loading (initialised lazily)
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

    # Global skills first
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

    # User-specific skills override global by name
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
    """Load user settings (e.g., modelId) from DynamoDB.

    Checks user-specific settings first, then falls back to __global__.
    """
    table = _get_dynamodb().Table(SKILLS_TABLE_NAME)
    for uid in [actor_id, "__global__"]:
        if uid in ("default", ""):
            continue
        resp = table.get_item(Key={"userId": uid, "skillName": "__settings__"})
        item = resp.get("Item")
        if item and item.get("modelId"):
            return {"modelId": item["modelId"]}
    return {}


# Fallback: static skills from filesystem
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


def create_agent(tools=None, session_manager=None, skills=None, model_id=None):
    """Create a Strands agent with Bedrock model, optional MCP tools, and memory."""
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
        system_prompt=SYSTEM_PROMPT,
        plugins=[skills_plugin],
    )

    if tools:
        agent_kwargs["tools"] = tools
    if session_manager:
        agent_kwargs["session_manager"] = session_manager

    return Agent(**agent_kwargs)


def get_mcp_tools(mcp_client):
    """Get all tools from MCP client with pagination."""
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
    """Run agent with MCP tools from Gateway if available, with memory persistence."""
    session_manager = get_memory_session_manager(session_id, actor_id)

    # Load skills and user settings from DynamoDB
    skills = None
    user_model_id = None
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

    if GATEWAY_URL:
        # Forward user's JWT to the CUSTOM_JWT gateway for per-user policy evaluation.
        # Requires runtime requestHeaderConfiguration.requestHeaderAllowlist: ["Authorization"]
        gw_headers = {}
        if auth_header:
            gw_headers["Authorization"] = auth_header
            logger.info(f"Forwarding user JWT to gateway for policy evaluation")
        else:
            logger.warning("No Authorization header available — gateway per-user policies won't apply")
        mcp_client = MCPClient(lambda: streamablehttp_client(GATEWAY_URL, headers=gw_headers or None))
        with mcp_client:
            mcp_tools = get_mcp_tools(mcp_client)

            # Secure KB tool: replace the MCP query_knowledge_base with a local wrapper
            # that auto-injects user_id from the verified actor_id (from JWT → Runtime context).
            # This prevents the LLM from fabricating or omitting the user identity.
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
                    # MCPToolResult has .content list; extract text
                    if hasattr(result, 'content') and result.content:
                        texts = [c.text for c in result.content if hasattr(c, 'text')]
                        return "\n".join(texts) if texts else json.dumps(result.content, default=str)
                    return str(result)

                all_tools = non_kb_tools + [query_knowledge_base]
                logger.info(f"KB tool wrapped with user_id={kb_user_id} (LLM cannot override)")
            else:
                all_tools = mcp_tools

            agent = create_agent(tools=all_tools, session_manager=session_manager, skills=skills, model_id=user_model_id)
            return str(agent(prompt))
    else:
        agent = create_agent(session_manager=session_manager, skills=skills, model_id=user_model_id)
        return str(agent(prompt))


@app.entrypoint
def handle_invocation(payload, context):
    """Handle HTTP POST /invocations requests."""
    prompt = payload.get("prompt", payload.get("inputText", ""))
    if not prompt:
        return {"error": "No prompt provided"}

    # Extract session and actor IDs from request context
    session_id = "default"
    actor_id = "default"
    if hasattr(context, 'session_id') and context.session_id:
        session_id = context.session_id

    # The runtime strips the X-Amzn-Bedrock-AgentCore-Runtime-User-Id header
    # before forwarding to the agent. Read user ID from the payload instead.
    actor_id = payload.get("userId", actor_id)

    logger.info(f"Invocation: actor_id={actor_id}, session_id={session_id}")

    # Record session to DynamoDB for admin visibility
    if SKILLS_TABLE_NAME:
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

    # Extract user's JWT from request headers (propagated via requestHeaderConfiguration)
    auth_header = None
    if hasattr(context, 'request_headers') and context.request_headers:
        auth_header = context.request_headers.get("Authorization") or context.request_headers.get("authorization")

    response = invoke_agent(prompt, session_id=session_id, actor_id=actor_id, auth_header=auth_header)
    return {"response": response, "status": "success"}


if __name__ == "__main__":
    app.run(log_level="info")
