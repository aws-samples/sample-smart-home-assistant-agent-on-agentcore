#!/bin/bash
set -e

echo "========================================="
echo "Smart Home Assistant - One-Click Deploy"
echo "========================================="

# Check prerequisites
command -v node >/dev/null 2>&1 || { echo "Node.js is required. Install from https://nodejs.org/"; exit 1; }
command -v npm >/dev/null 2>&1 || { echo "npm is required."; exit 1; }
command -v aws >/dev/null 2>&1 || { echo "AWS CLI is required."; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "Python 3 is required."; exit 1; }
command -v agentcore >/dev/null 2>&1 || { echo "agentcore CLI is required. Install: pip install strands-agents-builder"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "[1/7] Installing CDK dependencies..."
cd "$SCRIPT_DIR/cdk"
npm install

echo ""
echo "[2/7] Building Device Simulator..."
cd "$SCRIPT_DIR/device-simulator"
npm install
npm run build

echo ""
echo "[3/7] Building Chatbot..."
cd "$SCRIPT_DIR/chatbot"
npm install
npm run build

echo ""
echo "[4/7] Bootstrapping CDK (if needed)..."
cd "$SCRIPT_DIR/cdk"
npx cdk bootstrap 2>/dev/null || true

echo ""
echo "[5/7] Deploying CDK stack (Cognito, IoT, Lambda, S3, CloudFront)..."
cd "$SCRIPT_DIR/cdk"
npx cdk deploy --all --require-approval never --outputs-file "$SCRIPT_DIR/cdk-outputs.json"

echo ""
echo "[6/7] Fixing Cognito User Pool settings..."
# CDK selfSignUpEnabled doesn't always propagate correctly — ensure self-sign-up
# and email auto-verification are enabled
USER_POOL_ID=$(python3 -c "import json; print(json.load(open('$SCRIPT_DIR/cdk-outputs.json'))['SmartHomeAssistantStack']['UserPoolId'])")
aws cognito-idp update-user-pool \
  --user-pool-id "$USER_POOL_ID" \
  --auto-verified-attributes email \
  --admin-create-user-config AllowAdminCreateUserOnly=false \
  --region "${AWS_DEFAULT_REGION:-${AWS_REGION:-us-west-2}}"
echo "  Cognito: self-sign-up enabled, email auto-verification enabled"

echo ""
echo "[7/7] Deploying AgentCore (Gateway, Lambda Target, Runtime)..."
cd "$SCRIPT_DIR"

# Use venv if it exists, otherwise assume deps are globally available
if [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    source "$SCRIPT_DIR/venv/bin/activate"
fi

python3 "$SCRIPT_DIR/scripts/setup-agentcore.py"

echo ""
echo "========================================="
echo "Deployment Complete!"
echo "========================================="
