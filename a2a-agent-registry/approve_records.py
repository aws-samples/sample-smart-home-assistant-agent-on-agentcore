#!/usr/bin/env python3
"""Test-only helper: auto-approve the A2A records written by deploy.py.

Uses the Registry control plane's ``update_registry_record_status`` to flip
each record listed in ``deployed-state.json`` to ``APPROVED``. This bypasses
the intended admin-curation step — **only** for test/dev loops and E2E
smoke tests. Production deployments should approve through the AWS Console.

Usage:
    python approve_records.py
    python approve_records.py --agent energy-optimization
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import boto3

HERE = Path(__file__).resolve().parent
DEPLOYED_STATE = HERE / "deployed-state.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", action="append", default=[],
                    help="Agent short name (repeatable). Approves all agents if omitted.")
    args = ap.parse_args(argv)

    if not DEPLOYED_STATE.exists():
        print(f"{DEPLOYED_STATE} not found — run deploy.py first.", file=sys.stderr)
        return 1

    state = json.loads(DEPLOYED_STATE.read_text())
    agents = state.get("agents", [])
    if not agents:
        print("No agents in deployed-state.json.", file=sys.stderr)
        return 1

    wanted: set[str] = set()
    for raw in args.agent:
        wanted.update(x.strip() for x in raw.split(",") if x.strip())

    # Derive region from the runtime ARN (deploy-state may or may not carry it).
    region = "us-east-1"
    arn = agents[0].get("runtimeArn", "")
    if arn:
        parts = arn.split(":")
        if len(parts) >= 4:
            region = parts[3]

    # registryId sits in the repo-level agentcore-state.json written by
    # setup-agentcore.py.
    agentcore_state = HERE.parent / "agentcore-state.json"
    registry_id = ""
    if agentcore_state.exists():
        registry_id = json.loads(agentcore_state.read_text()).get("registryId", "")
    if not registry_id:
        print("registryId missing from agentcore-state.json", file=sys.stderr)
        return 1

    ac = boto3.client("bedrock-agentcore-control", region_name=region)
    for entry in agents:
        name = entry.get("agent", "")
        if wanted and name not in wanted:
            continue
        rid = entry.get("recordId")
        if not rid:
            print(f"  [{name}] no recordId in deployed-state.json — skip")
            continue
        try:
            resp = ac.update_registry_record_status(
                registryId=registry_id,
                recordId=rid,
                status="APPROVED",
                statusReason="test helper: approve_records.py",
            )
            print(f"  [{name}] {rid}: {resp.get('status')}")
        except Exception as exc:
            print(f"  [{name}] {rid}: FAILED — {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
