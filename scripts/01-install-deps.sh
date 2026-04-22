#!/bin/bash
set -e

# ==============================================================================
# Step 1/7: Install CDK dependencies and bundle latest boto3 into Lambda code
# ------------------------------------------------------------------------------
# What this step does:
#   - Runs `npm install` in cdk/ to fetch CDK TypeScript + construct libraries.
#   - Installs the latest boto3 into the admin-api, user-init, and kb-query
#     Lambda directories (Lambda's built-in boto3 is too old for the AgentCore
#     control-plane APIs used by admin-api).
#
# What this step DOES NOT do:
#   - Does not build the React frontends (that's step 2).
#   - Does not deploy anything to AWS.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v node >/dev/null 2>&1 || { echo "Node.js is required. Install from https://nodejs.org/"; exit 1; }
command -v npm  >/dev/null 2>&1 || { echo "npm is required."; exit 1; }
command -v pip  >/dev/null 2>&1 || { echo "pip is required."; exit 1; }

# Activate venv if present (for pip to install into the right interpreter)
if [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/venv/bin/activate"
fi

echo "==> Installing CDK npm dependencies..."
cd "$SCRIPT_DIR/cdk"
npm install

# scripts/setup-agentcore.py calls CreateRegistry / CreateRegistryRecord, which
# require boto3 >= 1.42.93. Older venvs (including the 1.42.82 that still
# ships in some environments) silently no-op the registry section because
# hasattr(client, 'create_registry') returns False.
echo "==> Upgrading boto3 in the active venv..."
pip install --upgrade boto3 -q 2>/dev/null || true

echo "==> Bundling latest boto3 into Lambda code directories..."
pip install boto3 -t "$SCRIPT_DIR/cdk/lambda/admin-api"     -q --upgrade 2>/dev/null || true
pip install boto3 -t "$SCRIPT_DIR/cdk/lambda/user-init"     -q --upgrade 2>/dev/null || true
pip install boto3 -t "$SCRIPT_DIR/cdk/lambda/kb-query"      -q --upgrade 2>/dev/null || true
pip install boto3 -t "$SCRIPT_DIR/cdk/lambda/skill-erp-api" -q --upgrade 2>/dev/null || true

echo "==> Step 1 complete."
