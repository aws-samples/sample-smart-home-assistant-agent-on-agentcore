#!/bin/bash
set -e

# ==============================================================================
# Step 2/7: Build the four React frontends
# ------------------------------------------------------------------------------
# What this step does:
#   - Builds `device-simulator/` — simulated IoT device dashboard (MQTT client)
#   - Builds `chatbot/`          — end-user chat UI (calls AgentCore Runtime)
#   - Builds `admin-console/`    — Agent Harness Management console
#   - Builds `skill-erp/`        — user-facing Skill ERP (publishes to Registry)
#   Each produces a static bundle in its `dist/` directory. CDK will upload
#   these bundles to S3 + CloudFront in step 4.
#
# What this step DOES NOT do:
#   - Does not deploy anything to AWS.
#   - Does not touch Lambda or CDK dependencies.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v node >/dev/null 2>&1 || { echo "Node.js is required."; exit 1; }
command -v npm  >/dev/null 2>&1 || { echo "npm is required.";   exit 1; }

echo "==> Building device-simulator..."
cd "$SCRIPT_DIR/device-simulator"
npm install
npm run build

echo "==> Building chatbot..."
cd "$SCRIPT_DIR/chatbot"
npm install
npm run build

echo "==> Building admin-console..."
cd "$SCRIPT_DIR/admin-console"
npm install
npm run build

echo "==> Building skill-erp..."
cd "$SCRIPT_DIR/skill-erp"
npm install
npm run build

echo "==> Step 2 complete."
