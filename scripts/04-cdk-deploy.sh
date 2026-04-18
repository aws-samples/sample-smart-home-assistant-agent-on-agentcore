#!/bin/bash
set -e

# ==============================================================================
# Step 4/7: Deploy the CDK stack (SmartHomeAssistantStack)
# ------------------------------------------------------------------------------
# What this step creates in AWS:
#   - Cognito User Pool + Identity Pool + "admin" group + default admin user
#   - IoT Core Things (LED matrix, fan, oven, rice cooker) + endpoint
#   - Lambda functions:
#       * iot-control    — validates + publishes MQTT device commands
#       * iot-discovery  — returns available device list
#       * admin-api      — skills/models/tool-access/KB/memory/session API
#       * kb-query       — Bedrock KB retrieval (per-user metadata filter)
#       * user-init      — Cognito post-confirmation trigger (grants tools)
#   - DynamoDB table (smarthome-skills) — skills, user settings, KB config,
#     session tracking
#   - S3 buckets:
#       * smarthome-skill-files — per-skill scripts/references/assets
#       * smarthome-kb-docs     — enterprise KB documents (scoped by prefix)
#   - OpenSearch Serverless collection (smarthome-kb) — KB vector index
#   - Bedrock Knowledge Base + S3 data source (Cohere embed-multilingual-v3)
#   - API Gateway (Cognito authorizer) for the admin API
#   - S3 + CloudFront distributions for the three React frontends
#
#   Writes all outputs (URLs, IDs, admin credentials) to cdk-outputs.json.
#
# What this step DOES NOT do:
#   - Does not deploy AgentCore Gateway/Runtime/Memory (that's step 6).
#   - Does not fix Cognito self-signup (that's step 5).
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v npx >/dev/null 2>&1 || { echo "npx is required."; exit 1; }

echo "==> Deploying CDK stack (Cognito, IoT, Lambda, DynamoDB, KB, API GW, S3+CloudFront)..."
cd "$SCRIPT_DIR/cdk"
npx cdk deploy --all --require-approval never \
    --outputs-file "$SCRIPT_DIR/cdk-outputs.json"

echo "==> CDK outputs written to $SCRIPT_DIR/cdk-outputs.json"
echo "==> Step 4 complete."
