#!/bin/bash
set -e

# ==============================================================================
# Smart Home Assistant — one-click deploy
# ------------------------------------------------------------------------------
# This script is a thin wrapper that runs the 7 split deployment scripts under
# `scripts/0[1-7]-*.sh` in order. Each split script prints exactly what AWS
# resources it creates, so you can also run them one at a time for debugging.
#
# Steps:
#   1. scripts/01-install-deps.sh      — CDK deps + bundle boto3 into Lambdas
#   2. scripts/02-build-frontends.sh   — Build device-simulator / chatbot / admin-console
#   3. scripts/03-cdk-bootstrap.sh     — CDK bootstrap (idempotent)
#   4. scripts/04-cdk-deploy.sh        — Cognito, IoT, Lambda, DynamoDB, KB,
#                                         API Gateway, S3+CloudFront
#   5. scripts/05-fix-cognito.sh       — Enable self-signup + email verification
#   6. scripts/06-deploy-agentcore.sh  — Gateway, Targets, Runtime, Memory, KB init
#   7. scripts/07-seed-skills.sh       — Seed built-in skills to DynamoDB
# ==============================================================================

echo "========================================="
echo "Smart Home Assistant - One-Click Deploy"
echo "========================================="

# Prerequisites (checked here so we fail fast before any step starts)
command -v node      >/dev/null 2>&1 || { echo "Node.js is required. Install from https://nodejs.org/"; exit 1; }
command -v npm       >/dev/null 2>&1 || { echo "npm is required."; exit 1; }
command -v aws       >/dev/null 2>&1 || { echo "AWS CLI is required."; exit 1; }
command -v python3   >/dev/null 2>&1 || { echo "Python 3 is required."; exit 1; }
command -v agentcore >/dev/null 2>&1 || { echo "agentcore CLI is required. Install: pip install strands-agents-builder"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Activate venv once so the split scripts inherit the interpreter
if [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/venv/bin/activate"
fi

run_step() {
    local num="$1"; shift
    local title="$1"; shift
    local script="$1"; shift
    echo ""
    echo "-----------------------------------------"
    echo "[$num/7] $title"
    echo "-----------------------------------------"
    bash "$SCRIPT_DIR/scripts/$script"
}

run_step 1 "Install CDK deps + bundle boto3 into Lambdas"       "01-install-deps.sh"
run_step 2 "Build React frontends"                              "02-build-frontends.sh"
run_step 3 "CDK bootstrap"                                      "03-cdk-bootstrap.sh"
run_step 4 "Deploy CDK stack (Cognito/IoT/Lambda/KB/S3/CF)"     "04-cdk-deploy.sh"
run_step 5 "Fix Cognito self-signup + email verification"       "05-fix-cognito.sh"
run_step 6 "Deploy AgentCore (Gateway/Targets/Runtime/Memory)"  "06-deploy-agentcore.sh"
run_step 7 "Seed built-in skills to DynamoDB"                   "07-seed-skills.sh"

echo ""
echo "========================================="
echo "  Deployment Complete!"
echo "========================================="
echo ""

CDK_OUTPUTS="$SCRIPT_DIR/cdk-outputs.json"
if [ -f "$CDK_OUTPUTS" ]; then
    DEVICE_SIM_URL=$(python3 -c "import json; print(json.load(open('$CDK_OUTPUTS'))['SmartHomeAssistantStack'].get('DeviceSimulatorUrl',''))")
    CHATBOT_URL=$(python3 -c "import json; print(json.load(open('$CDK_OUTPUTS'))['SmartHomeAssistantStack'].get('ChatbotUrl',''))")
    ADMIN_URL=$(python3 -c "import json; print(json.load(open('$CDK_OUTPUTS'))['SmartHomeAssistantStack'].get('AdminConsoleUrl',''))")
    ADMIN_USER=$(python3 -c "import json; print(json.load(open('$CDK_OUTPUTS'))['SmartHomeAssistantStack'].get('AdminUsername',''))")
    ADMIN_PASS=$(python3 -c "import json; print(json.load(open('$CDK_OUTPUTS'))['SmartHomeAssistantStack'].get('AdminPassword',''))")

    echo "  Device Simulator:  $DEVICE_SIM_URL"
    echo "  Chatbot:           $CHATBOT_URL"
    echo "  Admin Console:     $ADMIN_URL"
    echo ""
    echo "  Admin Login:       $ADMIN_USER / $ADMIN_PASS"
    echo ""
    echo "  Sign up as a new user on the Chatbot, then"
    echo "  log in to the Admin Console to manage skills."
fi
