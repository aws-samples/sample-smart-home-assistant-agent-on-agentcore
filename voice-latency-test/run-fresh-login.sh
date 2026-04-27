#!/bin/bash
# Voice Agent FRESH-LOGIN cold latency test — one-shot runner.
#
# What this measures:
#   End-to-end user journey. Per round: stop_session + UpdateRuntime nonce
#   bump + wait READY + FRESH browser context + Cognito sign-in + click voice.
#   Tears down the browser context at round end. This is the honest answer
#   to "how long does a first-time user wait?" and is the right metric for
#   evaluating frontend optimizations (warmup parallelisation, pre-signed
#   WS URL, preconnect hints).
#
# Slow: ~60s/round (15s runtime update + 8s fresh login + 7s WS cold +
# teardown). 100 rounds ≈ 100 min.
#
# For the faster "session-only" variant (login once, reuse page, only
# stop_session between rounds), see run-session-cold.sh.
#
# Prerequisites (already satisfied after `deploy.sh` finishes):
#   1. ../cdk-outputs.json        (contains ChatbotUrl)
#   2. ../agentcore-state.json    (contains voiceRuntimeArn)
#   3. ../venv/                   (has boto3 for sidecar)
#
# First-time setup inside this folder:
#   npm install
#   npx playwright install chromium
#
# Usage:
#   ./run-fresh-login.sh                       # 100 rounds, default output
#   ROUNDS=50 ./run-fresh-login.sh             # custom count
#   PROBE_INTER_RUN_MS=3000 ./run-fresh-login.sh
#   OUT=my-run.jsonl ./run-fresh-login.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

CDK_OUTPUTS="$PROJECT_ROOT/cdk-outputs.json"
AGENTCORE_STATE="$PROJECT_ROOT/agentcore-state.json"
VENV_ACTIVATE="$PROJECT_ROOT/venv/bin/activate"

[ -f "$CDK_OUTPUTS" ] || { echo "[error] $CDK_OUTPUTS not found — run deploy.sh first."; exit 1; }
[ -f "$AGENTCORE_STATE" ] || { echo "[error] $AGENTCORE_STATE not found — run deploy.sh first."; exit 1; }
[ -f "$VENV_ACTIVATE" ] || { echo "[error] venv not found at $VENV_ACTIVATE."; exit 1; }

CHATBOT_URL=$(python3 -c "import json;d=json.load(open('$CDK_OUTPUTS'));print(d['SmartHomeAssistantStack']['ChatbotUrl'])")
VOICE_RUNTIME_ARN=$(python3 -c "import json;d=json.load(open('$AGENTCORE_STATE'));print(d['voiceRuntimeArn'])")
AWS_REGION_RESOLVED=$(echo "$VOICE_RUNTIME_ARN" | awk -F: '{print $4}')

PROBE_USERNAME="${PROBE_USERNAME:-admin@smarthome.local}"
PROBE_PASSWORD="${PROBE_PASSWORD:-SmartHome#Admin1}"
ROUNDS="${ROUNDS:-100}"
PROBE_INTER_RUN_MS="${PROBE_INTER_RUN_MS:-2000}"
OUT="${OUT:-$SCRIPT_DIR/results/fresh-login-$(date +%Y%m%d-%H%M%S).jsonl}"

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

# shellcheck disable=SC1090
source "$VENV_ACTIVATE"

python3 "$SCRIPT_DIR/enable-welcome.py" --region "$AWS_REGION_RESOLVED" --runtime-arn "$VOICE_RUNTIME_ARN"

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
  npx playwright test voice-cold-fresh-login.spec.ts

echo ""
echo "----- run complete -----"
echo "Raw JSONL: $OUT"

echo "Generating markdown report..."
python3 "$SCRIPT_DIR/aggregate.py" "$OUT" > "${OUT%.jsonl}.md"
echo "Report:    ${OUT%.jsonl}.md"
