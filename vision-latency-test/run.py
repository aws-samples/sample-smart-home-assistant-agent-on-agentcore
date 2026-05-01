#!/usr/bin/env python3
"""Orchestrator for the vision-latency probe.

Loops:  model ∈ {haiku, nova} × image_count ∈ {1, 2, 3} × rounds.
Per cell it:
  1. Swaps the runtime's `VISION_MODEL_ID` env var via update_agent_runtime,
     then waits for the runtime to return to READY (the update triggers a
     new container version).
  2. Runs the Playwright probe with env PROBE_MODEL=<model>, PROBE_IMAGE_COUNT=<N>,
     appending one JSONL row per round to results/<model>_<N>.jsonl.

Usage:
    python3 run.py --rounds 30
    python3 run.py --rounds 3 --models haiku   # smaller dry run
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

MODELS = {
    "haiku": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "nova":  "us.amazon.nova-lite-v1:0",
}


def load_outputs():
    with open(os.path.join(REPO, "cdk-outputs.json")) as f:
        return json.load(f)["SmartHomeAssistantStack"]


def set_vision_model(region, runtime_id, model_id):
    """Patch the runtime's VISION_MODEL_ID, preserving every other config bit.
    Waits for status=READY before returning."""
    import boto3
    c = boto3.client("bedrock-agentcore-control", region_name=region)
    rt = c.get_agent_runtime(agentRuntimeId=runtime_id)
    env = dict(rt.get("environmentVariables") or {})
    if env.get("VISION_MODEL_ID") == model_id:
        print(f"  VISION_MODEL_ID already {model_id}; skipping update")
        return
    env["VISION_MODEL_ID"] = model_id
    kw = dict(
        agentRuntimeId=runtime_id,
        environmentVariables=env,
    )
    # Preserve every other piece of config the runtime currently has.
    for k in ("agentRuntimeArtifact", "roleArn", "networkConfiguration",
              "protocolConfiguration", "authorizerConfiguration"):
        if rt.get(k):
            kw[k] = rt[k]
    rhc = rt.get("requestHeaderConfiguration") or {}
    allowlist = rhc.get("requestHeaderAllowlist") or rt.get("requestHeaderAllowlist")
    if allowlist:
        kw["requestHeaderConfiguration"] = {"requestHeaderAllowlist": allowlist}
    fc = rt.get("filesystemConfigurations")
    if fc:
        kw["filesystemConfigurations"] = fc

    c.update_agent_runtime(**kw)
    # Wait READY
    t0 = time.time()
    while time.time() - t0 < 300:
        status = c.get_agent_runtime(agentRuntimeId=runtime_id).get("status")
        if status == "READY":
            print(f"  runtime READY (+{time.time()-t0:.1f}s)")
            return
        time.sleep(3)
    raise RuntimeError("runtime did not reach READY in 300s")


def run_cell(env, model_tag, image_count, rounds, inter_run_ms):
    out_path = os.path.join(HERE, "results", f"{model_tag}_{image_count}.jsonl")
    env = dict(env)
    env["PROBE_MODEL"] = model_tag
    env["PROBE_IMAGE_COUNT"] = str(image_count)
    env["PROBE_ROUNDS"] = str(rounds)
    env["PROBE_OUTPUT"] = out_path
    env["PROBE_INTER_RUN_MS"] = str(inter_run_ms)
    cmd = ["npx", "playwright", "test", "probe.spec.ts"]
    print(f"\n>>> {model_tag} × {image_count}img × {rounds} → {out_path}")
    subprocess.run(cmd, cwd=HERE, env=env, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=30)
    ap.add_argument("--models", nargs="+", default=list(MODELS.keys()))
    ap.add_argument("--counts", nargs="+", type=int, default=[1, 2, 3])
    ap.add_argument("--inter-run-ms", type=int, default=10_000)
    args = ap.parse_args()

    outs = load_outputs()
    region = os.environ.get("AWS_REGION", "us-west-2")
    # Text runtime ARN isn't in cdk-outputs; derive from AgentCore CLI state
    # or accept from env. We read agentcore-state.json if present.
    arn = os.environ.get("TEXT_RUNTIME_ARN", "")
    if not arn:
        state_path = os.path.join(REPO, "agentcore-state.json")
        if os.path.exists(state_path):
            with open(state_path) as f:
                state = json.load(f)
            arn = state.get("runtimeArn", "")
    if not arn:
        sys.exit("TEXT_RUNTIME_ARN not set and agentcore-state.json missing runtimeArn")
    runtime_id = arn.split("/")[-1]

    chat_url = outs.get("ChatbotUrl") or sys.exit("ChatbotUrl missing")

    env = {
        **os.environ,
        "CHATBOT_URL": chat_url,
        "TEXT_RUNTIME_ARN": arn,
        "AWS_REGION": region,
    }

    for model_tag in args.models:
        model_id = MODELS[model_tag]
        print(f"\n=== model: {model_tag} ({model_id}) ===")
        set_vision_model(region, runtime_id, model_id)
        for count in args.counts:
            run_cell(env, model_tag, count, args.rounds, args.inter_run_ms)


if __name__ == "__main__":
    main()
