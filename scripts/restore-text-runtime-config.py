#!/usr/bin/env python3
"""Put back what `agentcore deploy` strips off the orchestrator runtime.

`agentcore deploy` rewrites the runtime from `agentcore.json`, and that file
carries only what the CLI knows about. Everything else this deployment needs is
applied AFTER the deploy by `setup-agentcore.py` — so a deploy run on its own
leaves the runtime looking healthy and quietly broken:

  - the A2A_* vars go, so no `a2a_*` tool is ever registered and every delegation
    silently becomes the orchestrator answering from its own knowledge
  - REGISTRY_ID goes, so the A2A feature gate stays half-armed
  - SKILLS_TABLE_NAME goes, so per-user prompts, models and grants stop being read
  - the requestHeaderAllowlist goes, so the Runtime edge DROPS the chatbot's
    forwarded idToken and every Gateway call loses the end user's identity
  - filesystemConfigurations goes, so /mnt/workspace is unmounted and the code
    interpreter's artifacts have nowhere to land
  - protocolConfiguration goes

None of those fail loudly. This script exists so the fix is one command rather
than a hand-assembled boto3 call each time, and so the list cannot be
half-remembered.

Values come from the live deployment (cdk-outputs.json, agentcore-state.json, the
A2A deployed-state.json) rather than being hardcoded, and are MERGED onto whatever
env survived — the CLI does legitimately set the gateway URL and memory id itself.

    ./venv/bin/python scripts/restore-text-runtime-config.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
CDK_OUTPUTS = ROOT / "cdk-outputs.json"
AGENTCORE_STATE = ROOT / "agentcore-state.json"
A2A_STATE = ROOT / "a2a-agent-registry" / "deployed-state.json"


def _load(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"{path} not found — deploy first.")
    return json.loads(path.read_text())


def main() -> int:
    ac_state = _load(AGENTCORE_STATE)
    cdk = _load(CDK_OUTPUTS)
    cdk_out = cdk[next(iter(cdk))]

    region = ac_state.get("region") or os.environ.get("AWS_REGION", "us-west-2")
    runtime_id = ac_state.get("runtimeId", "")
    if not runtime_id:
        sys.exit("no runtimeId in agentcore-state.json")

    env_wanted = {
        "AWS_REGION": region,
        "MODEL_ID": ac_state.get("modelId") or "moonshotai.kimi-k2.5",
        "NOVA_SONIC_MODEL_ID": "amazon.nova-sonic-v1:0",
        "BYPASS_TOOL_CONSENT": "true",
        "SKILLS_TABLE_NAME": cdk_out.get("SkillsTableName", "smarthome-skills"),
        "RUNTIME_SESSIONS_TABLE_NAME": cdk_out.get(
            "RuntimeSessionsTableName", "smarthome-runtime-sessions"),
        "CODE_SESSIONS_TABLE_NAME": cdk_out.get(
            "CodeSessionsTableName", "smarthome-code-sessions"),
    }
    if ac_state.get("registryId"):
        env_wanted["REGISTRY_ID"] = ac_state["registryId"]
    # Composed from the gateway ID, because that is what the state file records —
    # there is no gatewayArn in it. Composing beats omitting: the A2A sub-agents
    # read this var off the main runtime to find the Gateway, so a missing value
    # leaves them with no device tools.
    account = boto3.client("sts").get_caller_identity()["Account"]
    if ac_state.get("gatewayId"):
        env_wanted["AGENTCORE_GATEWAY_ARN"] = (
            f"arn:aws:bedrock-agentcore:{region}:{account}:"
            f"gateway/{ac_state['gatewayId']}")

    # The A2A wiring, which is what makes every specialist reachable. Absent it,
    # the orchestrator registers no `a2a_*` tools and answers everything itself.
    if A2A_STATE.exists():
        cognito = (json.loads(A2A_STATE.read_text()).get("cognito") or {})
        if cognito.get("m2mSecretArn"):
            env_wanted["A2A_M2M_SECRET_ARN"] = cognito["m2mSecretArn"]
        if cognito.get("tokenUrl"):
            env_wanted["A2A_COGNITO_TOKEN_URL"] = cognito["tokenUrl"]
        if cognito.get("scope"):
            env_wanted["A2A_COGNITO_SCOPE"] = cognito["scope"]

    ac = boto3.client("bedrock-agentcore-control", region_name=region)
    rt = ac.get_agent_runtime(agentRuntimeId=runtime_id)

    env = dict(rt.get("environmentVariables") or {})
    before = set(env)
    env.update(env_wanted)

    ac.update_agent_runtime(
        agentRuntimeId=runtime_id,
        agentRuntimeArtifact=rt["agentRuntimeArtifact"],
        roleArn=rt["roleArn"],
        networkConfiguration=rt.get("networkConfiguration",
                                    {"networkMode": "PUBLIC"}),
        environmentVariables=env,
        protocolConfiguration={"serverProtocol": "HTTP"},
        # Without the allowlist the Runtime edge drops this header before the
        # container sees it, and does so silently — the request arrives looking
        # like one that simply chose not to forward a user token.
        requestHeaderConfiguration={
            "requestHeaderAllowlist": [
                "X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken"]},
        filesystemConfigurations=[{"sessionStorage": {"mountPath": "/mnt/workspace"}}],
    )

    restored = sorted(set(env) - before)
    print(f"runtime {runtime_id}")
    print(f"  env: {len(env)} vars ({len(restored)} restored: {restored})")
    print("  protocol=HTTP, header allowlist, /mnt/workspace: applied")
    print("  NOTE: the runtime goes UPDATING for ~1 minute before it is READY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
