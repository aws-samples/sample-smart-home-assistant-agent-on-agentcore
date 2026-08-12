#!/bin/bash
set -e

# ==============================================================================
# Step 7/7: Seed built-in skills into DynamoDB
# ------------------------------------------------------------------------------
# What this step does:
#   - Reads the SKILL.md files under `agent/skills/` and writes each one into
#     the `smarthome-skills` DynamoDB table as a `__global__` skill, so the
#     agent has the built-in device-control skills on first invocation.
#   - Publishes those same skills to AWS Agent Registry as approved SKILL
#     records, so `Admin Console -> Integration Registry -> Skills` shows what
#     is actually deployed. The two stores answer different questions: DynamoDB
#     is what the agent LOADS, the Registry is what a curator can SEE and
#     approve. Seeding only the first left that page holding one record while
#     nine skills were live, which reads as a broken page rather than an
#     unpublished one.
#   - Idempotent: PutItem for DynamoDB; the Registry step keys on a dedup name
#     and updates in place, because recordIds are what `importedFromRegistry`
#     points at and churning them would orphan every import.
#
# Prerequisites:
#   - CDK stack deployed (provides the DynamoDB table)
#   - AgentCore setup run (provides the registry id in agentcore-state.json).
#     The Registry step is skipped with a warning if it has not been.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v python3 >/dev/null 2>&1 || { echo "Python 3 is required."; exit 1; }

if [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/venv/bin/activate"
fi

echo "==> Seeding built-in skills to DynamoDB..."
python3 "$SCRIPT_DIR/scripts/seed-skills.py"

echo "==> Publishing built-in skills to AWS Agent Registry..."
if [ -f "$SCRIPT_DIR/agentcore-state.json" ]; then
    python3 "$SCRIPT_DIR/scripts/publish-builtin-skills.py"
else
    echo "    Skipped: agentcore-state.json not found (run scripts/06-deploy-agentcore.sh"
    echo "    first, then re-run this step). The agent still works — only the"
    echo "    Integration Registry > Skills overview will be missing its records."
fi

echo "==> Step 7 complete."
