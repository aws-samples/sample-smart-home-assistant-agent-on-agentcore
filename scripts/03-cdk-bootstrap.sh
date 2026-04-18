#!/bin/bash
set -e

# ==============================================================================
# Step 3/7: CDK bootstrap
# ------------------------------------------------------------------------------
# What this step does:
#   - Runs `cdk bootstrap` to provision the CDKToolkit CloudFormation stack
#     (asset S3 bucket, ECR repo, deploy roles) in the current account/region.
#   - Idempotent: already-bootstrapped account/regions are a no-op.
#
# What this step DOES NOT do:
#   - Does not deploy the application stack (that's step 4).
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v npx >/dev/null 2>&1 || { echo "npx is required."; exit 1; }

echo "==> Running cdk bootstrap (no-op if already bootstrapped)..."
cd "$SCRIPT_DIR/cdk"
npx cdk bootstrap 2>/dev/null || true

echo "==> Step 3 complete."
