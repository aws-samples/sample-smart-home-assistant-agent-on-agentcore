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
REGION="${AWS_DEFAULT_REGION:-${AWS_REGION:-us-west-2}}"

# Use venv if it exists
if [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    source "$SCRIPT_DIR/venv/bin/activate"
fi

echo ""
echo "[1/8] Installing CDK dependencies..."
cd "$SCRIPT_DIR/cdk"
npm install

# Bundle latest boto3 in admin-api Lambda (Lambda runtime's boto3 is too old for AgentCore APIs)
pip install boto3 -t "$SCRIPT_DIR/cdk/lambda/admin-api" -q --upgrade 2>/dev/null || true

echo ""
echo "[2/8] Building Device Simulator..."
cd "$SCRIPT_DIR/device-simulator"
npm install
npm run build

echo ""
echo "[3/8] Building Chatbot..."
cd "$SCRIPT_DIR/chatbot"
npm install
npm run build

echo ""
echo "[4/8] Building Admin Console..."
cd "$SCRIPT_DIR/admin-console"
npm install
npm run build

echo ""
echo "[5/8] Bootstrapping CDK (if needed)..."
cd "$SCRIPT_DIR/cdk"
npx cdk bootstrap 2>/dev/null || true

echo ""
echo "[6/8] Deploying CDK stack (Cognito, IoT, Lambda, DynamoDB, API Gateway, S3, CloudFront)..."
cd "$SCRIPT_DIR/cdk"
npx cdk deploy --all --require-approval never --outputs-file "$SCRIPT_DIR/cdk-outputs.json"

echo ""
echo "[7/8] Fixing Cognito User Pool settings..."
# CDK selfSignUpEnabled doesn't always propagate correctly — ensure self-sign-up
# and email auto-verification are enabled
USER_POOL_ID=$(python3 -c "import json; print(json.load(open('$SCRIPT_DIR/cdk-outputs.json'))['SmartHomeAssistantStack']['UserPoolId'])")
aws cognito-idp update-user-pool \
  --user-pool-id "$USER_POOL_ID" \
  --auto-verified-attributes email \
  --admin-create-user-config AllowAdminCreateUserOnly=false \
  --region "$REGION"
echo "  Cognito: self-sign-up enabled, email auto-verification enabled"

echo ""
echo "[8/8] Deploying AgentCore (Gateway, Lambda Target, Runtime)..."
cd "$SCRIPT_DIR"
python3 "$SCRIPT_DIR/scripts/setup-agentcore.py"

echo ""
echo "Seeding skills to DynamoDB..."
python3 "$SCRIPT_DIR/scripts/seed-skills.py"

# Print deployment summary with all URLs and credentials
echo ""
echo "========================================="
echo "  Deployment Complete!"
echo "========================================="
echo ""

# Read outputs for summary
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
