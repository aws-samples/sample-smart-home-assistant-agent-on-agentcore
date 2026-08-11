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

# scripts/setup-agentcore.py calls CreateRegistry / CreateRegistryRecord against
# AWS Agent Registry, which lives in the `agent-registry-control` namespace since
# it went GA on 2026-08-06.
#
# The floor is 1.43.67 — the first release carrying that service (verified by
# inspecting boto3's own service model). This is now a HARD requirement, not
# forward planning: the Registry code paths build an `agent-registry-control`
# client, and an older boto3 raises UnknownServiceError. The old
# `bedrock-agentcore` namespace stopped being used here when the migration
# landed, and it stops serving Registry entirely on 2026-09-17.
#
# These installs used to end in `2>/dev/null || true`, which discarded both the
# error text and the exit code — hiding the one failure this step exists to
# prevent. They now fail loudly, and the version floor is asserted afterwards so
# a pip that "succeeds" without actually upgrading is caught too.
BOTO3_MIN="1.43.67"

echo "==> Upgrading boto3 in the active venv..."
pip install --upgrade boto3 -q

python - "$BOTO3_MIN" <<'PY'
import sys
import boto3

minimum = sys.argv[1]


def parts(v):
    return tuple(int(x) for x in v.split(".")[:3])


if parts(boto3.__version__) < parts(minimum):
    sys.exit(
        f"boto3 {boto3.__version__} is below the required {minimum}. "
        "AWS Agent Registry needs the agent-registry-control service, "
        "which that release does not carry. "
        "Upgrade the venv (or recreate it) and re-run."
    )
print(f"    -> boto3 {boto3.__version__} (>= {minimum})")
PY

echo "==> Bundling latest boto3 into Lambda code directories..."
for lambda_dir in admin-api user-init kb-query skill-erp-api; do
    pip install boto3 -t "$SCRIPT_DIR/cdk/lambda/$lambda_dir" -q --upgrade
done

# ------------------------------------------------------------------------------
# Copy the shared device catalog into the Lambdas that validate against it.
# CDK packages each Lambda with Code.fromAsset(<dir>), so a file outside the
# directory is not deployed — and the catalog has to stay a SINGLE source of
# truth, because the drift between per-copy device tables is exactly what it
# replaces. Source of truth is shared/; these are build outputs (gitignored).
# ------------------------------------------------------------------------------
# admin-api is in this list because it imports scenarios.py, which imports
# device_catalog — so it validates a scene's actions against the catalog too. Its
# copy was placed by hand once and never refreshed by this script, which meant it
# drifted: a capability added here was enforced by the runner and the control path
# but not by the console's own validation.
echo "==> Copying shared device catalog into IoT Lambda directories..."
for lambda_dir in iot-control iot-discovery iot-query scenario-runner admin-api; do
    target="$SCRIPT_DIR/cdk/lambda/$lambda_dir"
    [ -d "$target" ] || continue
    cp "$SCRIPT_DIR/shared/device-catalog.json" "$target/device-catalog.json"
    cp "$SCRIPT_DIR/shared/device_catalog.py"   "$target/device_catalog.py"
    echo "    -> $lambda_dir"
done

# ------------------------------------------------------------------------------
# The scenario model and its two companions, for the Lambdas that schedule and
# execute a scene. Same reason as the catalog: these decide whether a trigger has
# fired, which actions to apply, what time sunrise is, and what a schedule is
# called. A second copy of any of them would let the stored scene and the executed
# one disagree — unobservable, because nobody is watching at 07:30.
#
# Both Lambdas get all three, and that matters for the two that are new:
#   - solar.py, because the admin API computes a solar cron when a scene is saved
#     and the runner recomputes it nightly.
#   - scenario_schedules_shared.py, because those two writes must be identical. If
#     they differed, each pass would "correct" the other and the scene would fire
#     correctly on alternate days only.
#
# The task-management A2A agent gets its copy of shared/ from deploy.py.
# ------------------------------------------------------------------------------
echo "==> Copying the scenario model into the scenario Lambdas..."
for lambda_dir in scenario-runner admin-api; do
    target="$SCRIPT_DIR/cdk/lambda/$lambda_dir"
    [ -d "$target" ] || continue
    for module in scenarios.py solar.py scenario_schedules_shared.py user_settings.py; do
        cp "$SCRIPT_DIR/shared/$module" "$target/$module"
    done
    echo "    -> $lambda_dir"
done

# ------------------------------------------------------------------------------
# The Memory actor-id rule, for the orchestrator. Its container is built from
# agent/ alone, while the A2A sub-agents get all of shared/ from deploy.py — and
# both read the same Memory namespaces. Two containers that sanitize the same user
# differently each get a working, private, half-empty memory: no error, just an
# agent that never remembers what the other one was told. See
# shared/memory_actor.py.
# ------------------------------------------------------------------------------
echo "==> Copying the Memory actor rule into the agent..."
cp "$SCRIPT_DIR/shared/memory_actor.py" "$SCRIPT_DIR/agent/memory_actor.py"
echo "    -> agent"

# ------------------------------------------------------------------------------
# The device brief, for the orchestrator. It names the relevant devices in each
# delegated request so the specialist skips its opening `discover_devices` cycle
# — measured at ~1.2s of an LLM turn whose only output was that one call.
#
# Needs the catalog itself alongside it, since the brief is generated from the
# same `shared/device-catalog.json` the validating Lambda reads. That shared
# source is the point: a brief built from a second copy could name a capability
# the control Lambda would then reject, and the user would see a specialist
# confidently issue a command that fails.
# ------------------------------------------------------------------------------
echo "==> Copying the device brief + catalog into the agent..."
cp "$SCRIPT_DIR/shared/device_brief.py"    "$SCRIPT_DIR/agent/device_brief.py"
cp "$SCRIPT_DIR/shared/device_catalog.py"  "$SCRIPT_DIR/agent/device_catalog.py"
cp "$SCRIPT_DIR/shared/device-catalog.json" "$SCRIPT_DIR/agent/device-catalog.json"
echo "    -> agent"

# ------------------------------------------------------------------------------
# Copy the AWS Agent Registry helper into the Lambdas that talk to the Registry.
# Same reason as the catalog above: Code.fromAsset(<dir>) only packages the
# directory, and the GA record shapes must not be re-derived per caller — the
# preview-to-GA change renamed fields AND inverted the SKILL descriptor, so a
# second hand-written copy would be a second chance to get it wrong.
# ------------------------------------------------------------------------------
echo "==> Copying the Agent Registry helper into Registry Lambda directories..."
for lambda_dir in skill-erp-api admin-api; do
    target="$SCRIPT_DIR/cdk/lambda/$lambda_dir"
    [ -d "$target" ] || continue
    cp "$SCRIPT_DIR/shared/agent_registry.py" "$target/agent_registry.py"
    echo "    -> $lambda_dir"
done

# ------------------------------------------------------------------------------
# Fetch the Amazon DCV Web Client SDK into chatbot/public/dcvjs/.
# Required for the BrowserPanel live-view feature: the chatbot's DcvViewer
# component loads /dcvjs/dcv.js at runtime to render the AgentCore browser
# live-view stream. The SDK is not redistributed in this repo (EULA terms);
# we download a fresh copy on every deploy from the public AWS CloudFront.
# Skip if already present to keep re-deploys fast.
# ------------------------------------------------------------------------------
DCV_DIR="$SCRIPT_DIR/chatbot/public/dcvjs"
DCV_URL="https://d1uj6qtbmh3dt5.cloudfront.net/webclientsdk/nice-dcv-web-client-sdk-1.9.100-952.zip"
if [ -f "$DCV_DIR/dcv.js" ]; then
    echo "==> DCV Web SDK already present in chatbot/public/dcvjs/ — skipping download."
else
    echo "==> Downloading Amazon DCV Web Client SDK..."
    command -v curl   >/dev/null 2>&1 || { echo "curl is required for DCV SDK download."; exit 1; }
    command -v unzip  >/dev/null 2>&1 || { echo "unzip is required for DCV SDK download."; exit 1; }
    TMPDIR=$(mktemp -d)
    trap "rm -rf \"$TMPDIR\"" EXIT
    curl -sL -o "$TMPDIR/dcv-sdk.zip" "$DCV_URL"
    unzip -q "$TMPDIR/dcv-sdk.zip" -d "$TMPDIR"
    mkdir -p "$DCV_DIR"
    cp -r "$TMPDIR/nice-dcv-web-client-sdk/dcvjs-umd/"* "$DCV_DIR/"
    echo "    -> $DCV_DIR populated ($(du -sh "$DCV_DIR" | awk '{print $1}'))"
    echo "    NOTE: the SDK ships under Amazon DCV EULA — see chatbot/public/dcvjs/EULA.txt"
fi

echo "==> Step 1 complete."
