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
from strands.models.bedrock import BedrockModel  # noqa: E402
from strands.tools.mcp.mcp_client import MCPClient  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402
from strands.telemetry import StrandsTelemetry  # noqa: E402
from bedrock_agentcore import BedrockAgentCoreApp  # noqa: E402
from memory.session import get_memory_session_manager  # noqa: E402

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

app = BedrockAgentCoreApp()

# Load skills
skills_plugin = AgentSkills(skills="./skills/")


def create_agent(tools=None, session_manager=None):
    """Create a Strands agent with Bedrock model, optional MCP tools, and memory."""
    model = BedrockModel(
        model_id=MODEL_ID,
        region_name=AWS_REGION,
        streaming=True,
    )

    agent_kwargs = dict(
        model=model,
        system_prompt="""You are a smart home assistant that controls devices in the user's home.
You can control: LED Matrix, Rice Cooker, Fan, and Oven.
Be helpful, concise, and confirm actions taken. If a user asks to do something, use the appropriate device control tool.
You can also suggest creative lighting scenes, cooking presets, and comfort settings.
Use what you remember about the user's preferences to personalize your responses.""",
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


def invoke_agent(prompt, session_id="default", actor_id="default"):
    """Run agent with MCP tools from Gateway if available, with memory persistence."""
    session_manager = get_memory_session_manager(session_id, actor_id)

    if GATEWAY_URL:
        mcp_client = MCPClient(lambda: streamablehttp_client(GATEWAY_URL))
        with mcp_client:
            tools = get_mcp_tools(mcp_client)
            agent = create_agent(tools=tools, session_manager=session_manager)
            return str(agent(prompt))
    else:
        agent = create_agent(session_manager=session_manager)
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
    if hasattr(context, 'request_headers') and context.request_headers:
        # Use runtime user ID header if available
        actor_id = context.request_headers.get(
            "x-amzn-bedrock-agentcore-runtime-user-id", actor_id
        )

    response = invoke_agent(prompt, session_id=session_id, actor_id=actor_id)
    return {"response": response, "status": "success"}


if __name__ == "__main__":
    app.run(log_level="info")
