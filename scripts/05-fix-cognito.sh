#!/bin/bash
set -e

# ==============================================================================
# Step 5/7: Fix Cognito User Pool settings
# ------------------------------------------------------------------------------
# What this step does:
#   - Enables self-service sign-up (AllowAdminCreateUserOnly=false)
#   - Enables email auto-verification (--auto-verified-attributes email)
#
# Why it's separate from CDK deploy:
#   CDK's `selfSignUpEnabled: true` and auto-verification flags don't always
#   propagate to the User Pool correctly. This step calls
#   `aws cognito-idp update-user-pool` directly to guarantee the final state.
#
# What this step DOES NOT do:
#   - Does not create or modify any users/groups.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_DEFAULT_REGION:-${AWS_REGION:-us-west-2}}"
CDK_OUTPUTS="$SCRIPT_DIR/cdk-outputs.json"

command -v aws     >/dev/null 2>&1 || { echo "AWS CLI is required."; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "Python 3 is required."; exit 1; }

if [ ! -f "$CDK_OUTPUTS" ]; then
    echo "Error: $CDK_OUTPUTS not found. Run scripts/04-cdk-deploy.sh first."
    exit 1
fi

USER_POOL_ID=$(python3 -c "import json; print(json.load(open('$CDK_OUTPUTS'))['SmartHomeAssistantStack']['UserPoolId'])")

echo "==> Enabling self-sign-up + email auto-verification on user pool $USER_POOL_ID..."
aws cognito-idp update-user-pool \
    --user-pool-id "$USER_POOL_ID" \
    --auto-verified-attributes email \
    --admin-create-user-config AllowAdminCreateUserOnly=false \
    --region "$REGION"

echo "==> Step 5 complete."
