#!/bin/bash
set -e

# ==============================================================================
# Step 6/7: Deploy AgentCore resources
# ------------------------------------------------------------------------------
# What this step creates in AWS (via scripts/setup-agentcore.py):
#   - AgentCore Gateway (MCP server) with CUSTOM_JWT auth (Cognito)
#   - Gateway Lambda Targets for iot-control, iot-discovery, kb-query
#   - AgentCore Runtime — hosts the Strands agent (Python 3.13 CodeZip)
#   - AgentCore Memory — semantic / summary / user-preference strategies
#   - AgentCore Policy Engine + per-tool Cedar permit policies
#   - Initializes the enterprise KB (AOSS vector index, Bedrock KB data source)
#   - Patches the Runtime with SKILLS_TABLE_NAME + requestHeaderAllowlist
#     (so user JWTs are forwarded to the gateway for Cedar enforcement)
#
# Prerequisites:
#   - CDK stack already deployed (cdk-outputs.json must exist)
#   - agentcore CLI installed (`pip install strands-agents-builder`)
#   - Bedrock model access to Kimi K2.5 granted in the AWS account
#
# What this step DOES NOT do:
#   - Does not seed DynamoDB skills (that's step 7).
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v python3   >/dev/null 2>&1 || { echo "Python 3 is required."; exit 1; }
command -v agentcore >/dev/null 2>&1 || { echo "agentcore CLI is required. Install: pip install strands-agents-builder"; exit 1; }

if [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/venv/bin/activate"
fi

if [ ! -f "$SCRIPT_DIR/cdk-outputs.json" ]; then
    echo "Error: cdk-outputs.json not found. Run scripts/04-cdk-deploy.sh first."
    exit 1
fi

echo "==> Deploying AgentCore (Gateway, Targets, Runtime, Memory, KB init)..."
cd "$SCRIPT_DIR"
python3 "$SCRIPT_DIR/scripts/setup-agentcore.py"

echo "==> Step 6 complete."
