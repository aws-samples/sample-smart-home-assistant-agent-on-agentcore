import os
import json
import logging
from strands import Agent, AgentSkills
from strands.models.bedrock import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client
from bedrock_agentcore import BedrockAgentCoreApp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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


def create_agent(tools=None):
    """Create a Strands agent with Bedrock model and optional MCP tools."""
    model = BedrockModel(
        model_id=MODEL_ID,
        region_name=AWS_REGION,
        streaming=True,
    )

    agent_kwargs = dict(
        model=model,
        system_prompt="""You are a smart home assistant that controls devices in the user's home.
You can control: LED Matrix (Govee RGBIC), Rice Cooker, Fan, and Oven.
Be helpful, concise, and confirm actions taken. If a user asks to do something, use the appropriate device control tool.
You can also suggest creative lighting scenes, cooking presets, and comfort settings.""",
        plugins=[skills_plugin],
    )

    if tools:
        agent_kwargs["tools"] = tools

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


def invoke_agent(prompt):
    """Run agent with MCP tools from Gateway if available, otherwise without tools."""
    if GATEWAY_URL:
        mcp_client = MCPClient(lambda: streamablehttp_client(GATEWAY_URL))
        with mcp_client:
            tools = get_mcp_tools(mcp_client)
            agent = create_agent(tools=tools)
            return str(agent(prompt))
    else:
        agent = create_agent()
        return str(agent(prompt))


@app.entrypoint
def handle_invocation(payload):
    """Handle HTTP POST /invocations requests."""
    prompt = payload.get("prompt", payload.get("inputText", ""))
    if not prompt:
        return {"error": "No prompt provided"}

    response = invoke_agent(prompt)
    return {"response": response, "status": "success"}


if __name__ == "__main__":
    app.run(log_level="info")
