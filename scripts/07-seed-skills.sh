#!/bin/bash
set -e

# ==============================================================================
# Step 7/7: Seed built-in skills into DynamoDB
# ------------------------------------------------------------------------------
# What this step does:
#   - Reads the SKILL.md files under `agent/skills/` and writes each one into
#     the `smarthome-skills` DynamoDB table as a `__global__` skill, so the
#     agent has the built-in device-control skills on first invocation.
#   - Idempotent: uses PutItem, overwriting any existing rows with the same key.
#
# Prerequisites:
#   - CDK stack deployed (provides the DynamoDB table)
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v python3 >/dev/null 2>&1 || { echo "Python 3 is required."; exit 1; }

if [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/venv/bin/activate"
fi

echo "==> Seeding built-in skills to DynamoDB..."
python3 "$SCRIPT_DIR/scripts/seed-skills.py"

echo "==> Step 7 complete."
