#!/usr/bin/env python3
"""Demo reset — wipe everything for ONE A2A agent so the Step 1-5 demo walkthrough
(see README.md) can run again from a clean slate.

Removes (per-agent only):
  - DynamoDB ``__a2a_permissions__`` rows that reference this agent's recordId
  - The agent's real Registry record (from ``deployed-state.json``)
  - Any same-name placeholder records still in the Registry
  - The CFN stack (Runtime + exec role)
  - The workload identity
  - The local ``.agentcore-project/<slug>/`` dir
  - The entry in ``deployed-state.json``

Keeps (so Step 1 is fast on rerun):
  - Cognito m2m app client / resource server / Secrets Manager secret
  - Other agents' Runtimes / Registry records
  - The text-agent's ``A2A_*`` env vars (still valid for remaining agents)

Usage:
    python demo_reset.py --agent energy-optimization
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import boto3

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
DEPLOYED_STATE = HERE / "deployed-state.json"
AGENTCORE_STATE = PROJECT_ROOT / "agentcore-state.json"
AC_PROJECT_DIR = HERE / ".agentcore-project"

AGENT_NAMES = ("energy-optimization", "home-security", "appliance-maintenance")
AGENT_LONG_NAMES = {
    "energy-optimization": "energy-optimization-agent",
    "home-security": "home-security-agent",
    "appliance-maintenance": "appliance-maintenance-agent",
}
AGENT_SHORT_SLUG = {
    "energy-optimization": "sha2aenergy",
    "home-security": "sha2asecurity",
    "appliance-maintenance": "sha2amaintenance",
}

SKILLS_TABLE = "smarthome-skills"


def log(msg: str) -> None:
    print(msg, flush=True)


def _region_from_state() -> str:
    if AGENTCORE_STATE.exists():
        arn = json.loads(AGENTCORE_STATE.read_text()).get("runtimeArn", "")
        if arn:
            parts = arn.split(":")
            if len(parts) >= 4:
                return parts[3]
    return "us-east-1"


def _registry_id() -> str:
    if AGENTCORE_STATE.exists():
        return json.loads(AGENTCORE_STATE.read_text()).get("registryId", "")
    return ""


def revoke_grants(agent: str, region: str, record_ids: set[str]) -> None:
    """Scan __a2a_permissions__ rows; drop this record's entry from each.

    Leaves other recordId grants intact. Deletes the row entirely if it ends
    up empty.
    """
    if not record_ids:
        return
    ddb = boto3.resource("dynamodb", region_name=region).Table(SKILLS_TABLE)
    from boto3.dynamodb.conditions import Attr

    scan_kwargs = {"FilterExpression": Attr("skillName").eq("__a2a_permissions__")}
    touched = 0
    while True:
        resp = ddb.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            grants = item.get("a2aGrants") or {}
            changed = False
            for rid in record_ids:
                if rid in grants:
                    grants.pop(rid)
                    changed = True
            if not changed:
                continue
            touched += 1
            user_id = item["userId"]
            if grants:
                ddb.put_item(Item={
                    "userId": user_id,
                    "skillName": "__a2a_permissions__",
                    "a2aGrants": grants,
                    "updatedAt": item.get("updatedAt", ""),
                })
            else:
                ddb.delete_item(Key={"userId": user_id, "skillName": "__a2a_permissions__"})
        token = resp.get("LastEvaluatedKey")
        if not token:
            break
        scan_kwargs["ExclusiveStartKey"] = token
    log(f"  cleaned A2A grants from {touched} user row(s)")


def delete_registry_records(agent: str, region: str, registry_id: str, known_record_id: str | None) -> None:
    """Delete both the real record (known_record_id) and any same-name placeholders."""
    if not registry_id:
        log("  no registryId; skipping Registry cleanup")
        return
    ac = boto3.client("bedrock-agentcore-control", region_name=region)
    long_name = AGENT_LONG_NAMES[agent]
    deleted = 0
    try:
        paginator = ac.get_paginator("list_registry_records")
        for page in paginator.paginate(registryId=registry_id, descriptorType="A2A", maxResults=50):
            for rec in page.get("registryRecords", []):
                if rec.get("name") != long_name:
                    continue
                rid = rec.get("recordId", "")
                if not rid:
                    continue
                try:
                    ac.delete_registry_record(registryId=registry_id, recordId=rid)
                    deleted += 1
                    log(f"  deleted Registry record {rid}")
                except Exception as e:
                    log(f"  delete {rid} failed: {e}")
    except Exception as e:
        log(f"  list Registry records failed: {e}")
    if known_record_id and deleted == 0:
        try:
            ac.delete_registry_record(registryId=registry_id, recordId=known_record_id)
            log(f"  deleted known Registry record {known_record_id}")
        except Exception as e:
            log(f"  delete known {known_record_id}: {e}")


def delete_runtime_stack(agent: str, region: str, cfn_stack: str) -> None:
    cf = boto3.client("cloudformation", region_name=region)
    try:
        cf.describe_stacks(StackName=cfn_stack)
    except cf.exceptions.ClientError as e:
        if "does not exist" in str(e):
            log(f"  stack {cfn_stack} already gone")
            return
        raise
    cf.delete_stack(StackName=cfn_stack)
    log(f"  delete_stack: {cfn_stack}")
    waiter = cf.get_waiter("stack_delete_complete")
    waiter.wait(StackName=cfn_stack, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    log("  stack deleted")


def delete_workload_identity(agent: str, region: str) -> None:
    ac = boto3.client("bedrock-agentcore-control", region_name=region)
    wid = AGENT_SHORT_SLUG[agent]
    try:
        ac.delete_workload_identity(name=wid)
        log(f"  deleted workload identity {wid}")
    except Exception as e:
        log(f"  workload identity {wid}: {e}")


def remove_local_project_dir(agent: str) -> None:
    slug = AGENT_SHORT_SLUG[agent]
    p = AC_PROJECT_DIR / slug
    if p.exists():
        shutil.rmtree(p)
        log(f"  removed {p}")


def drop_from_deployed_state(agent: str) -> None:
    if not DEPLOYED_STATE.exists():
        return
    state = json.loads(DEPLOYED_STATE.read_text())
    state["agents"] = [a for a in state.get("agents", []) if a.get("agent") != agent]
    DEPLOYED_STATE.write_text(json.dumps(state, indent=2) + "\n")
    log(f"  updated {DEPLOYED_STATE}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", required=True, choices=AGENT_NAMES,
                    help="Agent to reset.")
    args = ap.parse_args(argv)
    agent = args.agent
    long_name = AGENT_LONG_NAMES[agent]
    slug = AGENT_SHORT_SLUG[agent]

    log(f"\n=== demo_reset: {agent} ({long_name}) ===")

    region = _region_from_state()
    registry_id = _registry_id()

    # Load the current entry (may or may not exist).
    entry: dict = {}
    if DEPLOYED_STATE.exists():
        state = json.loads(DEPLOYED_STATE.read_text())
        entry = next(
            (a for a in state.get("agents", []) if a.get("agent") == agent), {}
        )

    known_rid = entry.get("recordId")
    cfn_stack = entry.get("cfnStack") or f"AgentCore-{slug}-default"

    # Collect recordIds to revoke grants for: primary + anything named the same
    # (placeholders). We scan Registry once; safer than skipping if Registry list
    # hiccups.
    record_ids_to_clean = set()
    if known_rid:
        record_ids_to_clean.add(known_rid)
    if registry_id:
        try:
            ac = boto3.client("bedrock-agentcore-control", region_name=region)
            paginator = ac.get_paginator("list_registry_records")
            for page in paginator.paginate(registryId=registry_id, descriptorType="A2A", maxResults=50):
                for rec in page.get("registryRecords", []):
                    if rec.get("name") == long_name and rec.get("recordId"):
                        record_ids_to_clean.add(rec["recordId"])
        except Exception as e:
            log(f"  list Registry records failed (non-fatal): {e}")

    revoke_grants(agent, region, record_ids_to_clean)
    delete_registry_records(agent, region, registry_id, known_rid)
    delete_runtime_stack(agent, region, cfn_stack)
    delete_workload_identity(agent, region)
    remove_local_project_dir(agent)
    drop_from_deployed_state(agent)

    log("\nDone. You can now rerun the demo from Step 1:")
    log(f"  python deploy.py --agent {agent} --skip registry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
