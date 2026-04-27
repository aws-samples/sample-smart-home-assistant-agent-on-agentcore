#!/bin/bash
# Voice Agent SESSION-COLD latency test — one-shot runner.
#
# What this measures:
#   Long-lived Playwright session (login ONCE at start), then loop N rounds
#   of stop_session → click voice → measure. Runtime itself stays deployed;
#   we only kill the session's server-side worker so the next click hits a
#   freshly started Python process on the same container pool.
#
# For the OTHER variant (fresh Cognito login + fresh browser every round,
# simulating a brand-new user clicking voice for the first time),
# see run-fresh-login.sh — that's the end-to-end "user journey" measurement.
#
# Prerequisites (already satisfied after `deploy.sh` finishes):
#   1. ../cdk-outputs.json        (contains ChatbotUrl)
#   2. ../agentcore-state.json    (contains voiceRuntimeArn)
#   3. ../venv/                   (has boto3 for stop_session sidecar)
#
# First-time setup inside this folder:
#   npm install
#   npx playwright install chromium
#
# Usage:
#   ./run-session-cold.sh                       # 100 rounds, default output
#   ROUNDS=50 ./run-session-cold.sh             # custom count
#   PROBE_INTER_RUN_MS=3000 ./run-session-cold.sh
#   OUT=my-run.jsonl ./run-session-cold.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

CDK_OUTPUTS="$PROJECT_ROOT/cdk-outputs.json"
AGENTCORE_STATE="$PROJECT_ROOT/agentcore-state.json"
VENV_ACTIVATE="$PROJECT_ROOT/venv/bin/activate"

# ------------ sanity ------------
[ -f "$CDK_OUTPUTS" ] || { echo "[error] $CDK_OUTPUTS not found — run deploy.sh first."; exit 1; }
[ -f "$AGENTCORE_STATE" ] || { echo "[error] $AGENTCORE_STATE not found — run deploy.sh first."; exit 1; }
[ -f "$VENV_ACTIVATE" ] || { echo "[error] venv not found at $VENV_ACTIVATE."; exit 1; }

# ------------ config from state ------------
# ChatbotUrl is written into cdk-outputs.json by the CDK stack.
CHATBOT_URL=$(python3 -c "import json;d=json.load(open('$CDK_OUTPUTS'));print(d['SmartHomeAssistantStack']['ChatbotUrl'])")
# voiceRuntimeArn is written into agentcore-state.json by setup-agentcore.py.
VOICE_RUNTIME_ARN=$(python3 -c "import json;d=json.load(open('$AGENTCORE_STATE'));print(d['voiceRuntimeArn'])")
# Region from the Voice ARN — more reliable than env guess.
AWS_REGION_RESOLVED=$(echo "$VOICE_RUNTIME_ARN" | awk -F: '{print $4}')

# Admin credentials are fixed by the CDK stack (see cdk/lib/smarthome-stack.ts).
PROBE_USERNAME="${PROBE_USERNAME:-admin@smarthome.local}"
PROBE_PASSWORD="${PROBE_PASSWORD:-SmartHome#Admin1}"

# Tunables (all have sensible defaults).
ROUNDS="${ROUNDS:-100}"
PROBE_INTER_RUN_MS="${PROBE_INTER_RUN_MS:-5000}"
OUT="${OUT:-$SCRIPT_DIR/results/session-cold-$(date +%Y%m%d-%H%M%S).jsonl}"

mkdir -p "$(dirname "$OUT")"
: > "$OUT"

echo "----- config -----"
echo "chatbot_url:    $CHATBOT_URL"
echo "voice_runtime:  $VOICE_RUNTIME_ARN"
echo "region:         $AWS_REGION_RESOLVED"
echo "rounds:         $ROUNDS"
echo "inter_run_ms:   $PROBE_INTER_RUN_MS"
echo "output:         $OUT"
echo "------------------"

# Activate venv so the Playwright probe's python sidecar (stop_session) has boto3.
# shellcheck disable=SC1090
source "$VENV_ACTIVATE"

# Ensure the voice runtime's welcome clip is enabled — the probe uses the
# first welcome_audio frame to measure "click→first audio". Without this the
# probe still runs but ws_to_first_audio_ms stays null.
python3 "$SCRIPT_DIR/enable-welcome.py" --region "$AWS_REGION_RESOLVED" --runtime-arn "$VOICE_RUNTIME_ARN"

# Kick off the probe. Playwright reads most config from env vars (see the spec
# file's top for the full list).
cd "$SCRIPT_DIR"

CHATBOT_URL="$CHATBOT_URL" \
VOICE_RUNTIME_ARN="$VOICE_RUNTIME_ARN" \
AWS_REGION="$AWS_REGION_RESOLVED" \
PROBE_PYTHON="$PROJECT_ROOT/venv/bin/python3" \
PROBE_USERNAME="$PROBE_USERNAME" \
PROBE_PASSWORD="$PROBE_PASSWORD" \
PROBE_ROUNDS="$ROUNDS" \
PROBE_INTER_RUN_MS="$PROBE_INTER_RUN_MS" \
PROBE_OUTPUT="$OUT" \
  npx playwright test voice-cold-session.spec.ts

echo ""
echo "----- run complete -----"
echo "Raw JSONL: $OUT"
echo ""
echo "Generating markdown report..."
python3 "$SCRIPT_DIR/aggregate.py" "$OUT" > "${OUT%.jsonl}.md"
echo "Report:    ${OUT%.jsonl}.md"
