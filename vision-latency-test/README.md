# Vision-latency probe

Measures chatbot end-to-end latency for image turns across two Bedrock vision
models (Claude Haiku 4.5, Amazon Nova Lite) and three image counts (1/2/3).

Every iteration starts from a cold state: the user's text session is stopped,
a fresh Chromium context logs in (which triggers the chatbot's built-in
runtime warmup), images are attached, and we time *click-send → first
non-empty agent bubble*.

## Directory layout

```
vision-latency-test/
├── README.md              # this file
├── .gitignore             # excludes images/, results/, node_modules/, etc.
├── package.json           # Playwright dep
├── playwright.config.ts   # headless, single-worker
├── probe.spec.ts          # the per-iteration probe
├── download_images.py     # one-off: grab 20 Picsum images, normalize to ~200 KB
├── manifest.json          # written by download_images.py (tracked; listing only)
├── run.py                 # orchestrator: flips VISION_MODEL_ID, calls probe
├── aggregate.py           # reads results/*.jsonl, writes test_result_summary.md
└── test_result_summary.md             # generated report (tracked, overwritten by aggregate.py)
```

## Prerequisites

- Python venv at `../venv/` with `boto3` and `Pillow`.
- Node 20+, `npm install` in this directory.
- AWS credentials with `bedrock-agentcore-control:UpdateAgentRuntime`,
  `bedrock-agentcore:StopRuntimeSession`, `dynamodb:Scan` on the skills table.
- The chatbot deployed; `../cdk-outputs.json` and `../agentcore-state.json` present.

## Run

```bash
cd vision-latency-test

# One-time: fetch and normalize 20 test images (~200 KB each) into images/.
source ../venv/bin/activate
python3 download_images.py

# Install Playwright (includes Chromium browser).
npm install
npx playwright install chromium

# Full 2 models × 3 counts × 30 rounds = 180 iterations (~90–120 min).
python3 run.py --rounds 30

# Or a quick sanity run: 2 rounds per cell, single model.
python3 run.py --rounds 2 --models haiku

# Aggregate any completed results.
python3 aggregate.py
```

## What's measured

Per iteration, the probe records one JSONL row:

```json
{
  "ts_iso": "...",
  "round": 12,
  "model": "haiku",
  "image_count": 2,
  "ok": true,
  "images": ["img-13.jpg", "img-14.jpg"],
  "stop_session_ms": 13802,       // StopRuntimeSession + DDB scan roundtrip
  "login_to_ready_ms": 1540,      // browser navigate → chat UI mounted
  "warmup_ms": 6810,              // chatbot's internal text-runtime warmup call
  "click_to_reply_ms": 3412,      // *the headline metric*
  "reply_chars": 487              // sanity that we got a real reply
}
```

Only `click_to_reply_ms` is aggregated into `test_result_summary.md`; the others are kept
for diagnostics (e.g., distinguishing cold-startup drift from model latency).

## How the model swap works

The orchestrator patches the runtime's `VISION_MODEL_ID` env var via
`update_agent_runtime`. This triggers a new container version and we wait
for the runtime's status to return to READY before starting that model's
cells. Each cell then stops-session per-iteration so every iteration sees a
cold container running the selected model.

## Reproducing

All inputs are deterministic:
- `download_images.py` uses fixed Picsum seeds.
- `probe.spec.ts` picks images by `(round + i) % N`, so round 0 image-count 1
  always uses `img-01.jpg`, round 0 image-count 2 uses `img-01, img-02`, etc.
- Per-cell output is appended to `results/<model>_<count>.jsonl` (gitignored).

To rerun a single cell with different parameters:

```bash
CHATBOT_URL=https://... TEXT_RUNTIME_ARN=arn:... AWS_REGION=us-west-2 \
PROBE_MODEL=nova PROBE_IMAGE_COUNT=2 PROBE_ROUNDS=10 \
PROBE_OUTPUT=results/nova_2.jsonl \
npx playwright test probe.spec.ts
```
