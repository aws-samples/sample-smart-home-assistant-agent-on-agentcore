#!/usr/bin/env python3
"""
Idempotent helper — ensure VOICE_WELCOME_ENABLED=1 on the voice runtime so
the latency probe can time "click → first welcome_audio frame".

Only no-ops if the env var is already "1"; otherwise patches via
UpdateAgentRuntime and waits for READY.

Usage:
  enable-welcome.py --region <r> --runtime-arn <arn>
"""

import argparse
import sys
import time
import boto3


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--region", required=True)
    p.add_argument("--runtime-arn", required=True, dest="runtime_arn")
    args = p.parse_args()

    # Derive runtime ID from ARN.
    # Format: arn:aws:bedrock-agentcore:<region>:<acct>:runtime/<id>
    rt_id = args.runtime_arn.rsplit("/", 1)[-1]

    c = boto3.client("bedrock-agentcore-control", region_name=args.region)
    rt = c.get_agent_runtime(agentRuntimeId=rt_id)
    env = dict(rt.get("environmentVariables") or {})

    if env.get("VOICE_WELCOME_ENABLED") == "1":
        print(f"[enable-welcome] already enabled on {rt_id} — skipping update")
        return 0

    env["VOICE_WELCOME_ENABLED"] = "1"
    kw = {
        "agentRuntimeId": rt_id,
        "environmentVariables": env,
    }
    # Round-trip these fields unchanged or the API rejects the update.
    for k in (
        "agentRuntimeArtifact",
        "roleArn",
        "networkConfiguration",
        "protocolConfiguration",
        "authorizerConfiguration",
    ):
        if k in rt and rt[k] is not None:
            kw[k] = rt[k]
    # requestHeaderConfiguration round-trip. boto3 returns this either nested
    # under requestHeaderConfiguration or at top-level as requestHeaderAllowlist
    # depending on the API version. Read both and re-wrap into the nested
    # form UpdateAgentRuntime expects; skip this and the allowlist is silently
    # dropped → MCP gateway 401 storm.
    rhc = rt.get("requestHeaderConfiguration") or {}
    allowlist = rhc.get("requestHeaderAllowlist") or rt.get("requestHeaderAllowlist")
    if allowlist:
        kw["requestHeaderConfiguration"] = {"requestHeaderAllowlist": allowlist}

    print(f"[enable-welcome] patching VOICE_WELCOME_ENABLED=1 on {rt_id}...")
    c.update_agent_runtime(**kw)

    # Wait for READY.
    t0 = time.time()
    while time.time() - t0 < 300:
        s = c.get_agent_runtime(agentRuntimeId=rt_id)["status"]
        if s == "READY":
            print(f"[enable-welcome] READY in {time.time()-t0:.1f}s")
            return 0
        time.sleep(3)
    print("[enable-welcome] TIMEOUT waiting for READY", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
