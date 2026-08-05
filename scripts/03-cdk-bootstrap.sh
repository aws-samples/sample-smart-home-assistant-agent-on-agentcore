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
# `cdk bootstrap` is itself idempotent and exits 0 on an already-bootstrapped
# account, so it needs no `|| true`. This used to be `2>/dev/null || true`,
# which also discarded the failures that are NOT benign — missing IAM
# permissions, a bad region, no credentials — and those then resurfaced in
# step 4 as a confusing asset-upload error instead.
npx cdk bootstrap

echo "==> Step 3 complete."
