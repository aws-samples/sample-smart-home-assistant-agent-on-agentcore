#!/usr/bin/env python3
"""Teardown the A2A sample agents.

Reverse of ``deploy.py``. Idempotent. Per-agent steps run first; global
Cognito/Secret/text-agent-env cleanup runs only when all agents have been
removed.

CLI:
  python teardown.py                              # remove everything
  python teardown.py --agent energy-optimization  # remove one only
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import boto3

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
CDK_OUTPUTS = PROJECT_ROOT / "cdk-outputs.json"
AGENTCORE_STATE = PROJECT_ROOT / "agentcore-state.json"
DEPLOYED_STATE = HERE / "deployed-state.json"
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

RESOURCE_SERVER_ID = "a2a-server"
M2M_CLIENT_NAME = "smarthome-a2a-m2m"
SECRET_NAME = "smarthome/a2a/m2m-credentials"


def log(msg: str) -> None:
    print(msg, flush=True)


def load_deployed() -> dict[str, Any]:
    if not DEPLOYED_STATE.exists():
        log(f"{DEPLOYED_STATE} not found — nothing to tear down")
        return {"agents": [], "cognito": {}}
    return json.loads(DEPLOYED_STATE.read_text())


def save_deployed(d: dict[str, Any]) -> None:
    DEPLOYED_STATE.write_text(json.dumps(d, indent=2) + "\n")


def load_region() -> str:
    if AGENTCORE_STATE.exists():
        ac = json.loads(AGENTCORE_STATE.read_text())
        arn = ac.get("runtimeArn", "")
        if arn:
            parts = arn.split(":")
            if len(parts) >= 4:
                return parts[3]
    return os.environ.get("AWS_REGION", "us-east-1")


def load_user_pool_id() -> str:
    if CDK_OUTPUTS.exists():
        cdk = json.loads(CDK_OUTPUTS.read_text())
        stack_name = next(iter(cdk))
        return cdk[stack_name].get("UserPoolId", "")
    return ""


def teardown_agent(agent: str, entry: dict[str, Any], region: str, registry_id: str) -> None:
    log(f"\n=== {agent} ===")
    ac = boto3.client("bedrock-agentcore-control", region_name=region)
    cf = boto3.client("cloudformation", region_name=region)

    rec_id = entry.get("recordId")
    if rec_id and registry_id:
        try:
            ac.delete_registry_record(registryId=registry_id, recordId=rec_id)
            log(f"  deleted registry record {rec_id}")
        except Exception as e:
            log(f"  registry record delete failed — {e}")

    cfn_stack = entry.get("cfnStack") or f"AgentCore-{AGENT_SHORT_SLUG[agent]}-default"
    try:
        cf.describe_stacks(StackName=cfn_stack)
        cf.delete_stack(StackName=cfn_stack)
        log(f"  delete_stack: {cfn_stack}")
        waiter = cf.get_waiter("stack_delete_complete")
        waiter.wait(StackName=cfn_stack, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
        log("  stack deleted")
    except cf.exceptions.ClientError as e:
        if "does not exist" in str(e):
            log(f"  stack {cfn_stack} already gone")
        else:
            log(f"  stack delete failed — {e}")

    # Workload identity
    wid_name = f"{AGENT_SHORT_SLUG[agent]}"
    try:
        ac.delete_workload_identity(name=wid_name)
        log(f"  deleted workload identity {wid_name}")
    except Exception:
        pass

    # Local project dir
    project_dir = Path(entry.get("projectDir") or (AC_PROJECT_DIR / f"{AGENT_SHORT_SLUG[agent]}"))
    if project_dir.exists():
        shutil.rmtree(project_dir)
        log(f"  removed {project_dir}")


def teardown_global(deployed: dict[str, Any], region: str, user_pool_id: str) -> None:
    """Runs only when there are no agents left."""
    log("\n=== global cleanup ===")
    cognito = boto3.client("cognito-idp", region_name=region)
    secrets = boto3.client("secretsmanager", region_name=region)
    ac = boto3.client("bedrock-agentcore-control", region_name=region)
    iam = boto3.client("iam")

    # Detach A2A_* envs from text agent runtime
    try:
        if AGENTCORE_STATE.exists():
            state = json.loads(AGENTCORE_STATE.read_text())
            runtime_id = state.get("runtimeId", "")
            if runtime_id:
                rt = ac.get_agent_runtime(agentRuntimeId=runtime_id)
                env = rt.get("environmentVariables", {}) or {}
                changed = False
                for key in ("A2A_M2M_SECRET_ARN", "A2A_COGNITO_TOKEN_URL", "A2A_COGNITO_SCOPE"):
                    if key in env:
                        env.pop(key)
                        changed = True
                if changed:
                    ac.update_agent_runtime(
                        agentRuntimeId=runtime_id,
                        agentRuntimeArtifact=rt["agentRuntimeArtifact"],
                        roleArn=rt["roleArn"],
                        networkConfiguration=rt.get("networkConfiguration", {"networkMode": "PUBLIC"}),
                        environmentVariables=env,
                        **({"authorizerConfiguration": rt["authorizerConfiguration"]}
                           if rt.get("authorizerConfiguration") else {}),
                    )
                    log("  removed A2A_* envs from text agent runtime")
                role_arn = rt["roleArn"]
                role_name = role_arn.split("/")[-1]
                try:
                    iam.delete_role_policy(RoleName=role_name, PolicyName="A2AM2MSecretRead")
                    log("  detached A2AM2MSecretRead inline policy")
                except Exception:
                    pass
    except Exception as e:
        log(f"  text agent env cleanup skipped — {e}")

    # Delete secret
    try:
        secrets.delete_secret(SecretId=SECRET_NAME, ForceDeleteWithoutRecovery=True)
        log(f"  deleted Secret {SECRET_NAME}")
    except Exception as e:
        log(f"  secret delete skipped — {e}")

    # Delete m2m client + resource server
    if user_pool_id:
        try:
            paginator = cognito.get_paginator("list_user_pool_clients")
            for page in paginator.paginate(UserPoolId=user_pool_id, MaxResults=60):
                for c in page["UserPoolClients"]:
                    if c["ClientName"] == M2M_CLIENT_NAME:
                        cognito.delete_user_pool_client(
                            UserPoolId=user_pool_id, ClientId=c["ClientId"]
                        )
                        log(f"  deleted m2m app client {c['ClientId']}")
        except Exception as e:
            log(f"  m2m client delete skipped — {e}")
        try:
            cognito.delete_resource_server(
                UserPoolId=user_pool_id, Identifier=RESOURCE_SERVER_ID
            )
            log(f"  deleted resource server {RESOURCE_SERVER_ID}")
        except Exception:
            pass

    if DEPLOYED_STATE.exists():
        DEPLOYED_STATE.unlink()
        log(f"  removed {DEPLOYED_STATE}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Teardown A2A sample agents.")
    ap.add_argument("--agent", action="append", default=[], help="Agent(s) to remove.")
    args = ap.parse_args(argv)

    deployed = load_deployed()
    if not deployed.get("agents"):
        log("No agents deployed — exiting.")
        return 0

    region = load_region()
    user_pool_id = load_user_pool_id()
    registry_id = ""
    if AGENTCORE_STATE.exists():
        registry_id = json.loads(AGENTCORE_STATE.read_text()).get("registryId", "")

    # Flatten agent list
    flat: list[str] = []
    for raw in args.agent:
        flat.extend(x.strip() for x in raw.split(",") if x.strip())
    if flat:
        targets = [a for a in deployed["agents"] if a.get("agent") in flat]
    else:
        targets = list(deployed["agents"])

    for entry in targets:
        agent = entry.get("agent", "?")
        teardown_agent(agent, entry, region, registry_id)
        deployed["agents"] = [a for a in deployed.get("agents", []) if a.get("agent") != agent]
        save_deployed(deployed)

    if not deployed.get("agents"):
        teardown_global(deployed, region, user_pool_id)

    log("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
