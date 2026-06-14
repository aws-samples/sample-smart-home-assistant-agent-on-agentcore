#!/usr/bin/env python3
"""
Teardown script for AgentCore resources created by this solution ONLY.
Reads resource IDs from agentcore-state.json — never touches unrelated resources.
Run BEFORE `cdk destroy`.
"""

import json
import subprocess
import os
import sys
import boto3

REGION = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-west-2"))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
STATE_FILE = os.path.join(PROJECT_ROOT, "agentcore-state.json")


def _delete_bundles_runtime_by_name(client) -> None:
    """Look up and delete the bundles runtime ('smarthome-bundles') if it
    exists. Idempotent — silent no-op when the runtime is absent (e.g.
    teardown after a deploy that never created the bundles runtime)."""
    target_name = "smarthome-bundles"
    paginator = client.get_paginator("list_agent_runtimes")
    rt_id = None
    for page in paginator.paginate():
        for rt in page.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == target_name:
                rt_id = rt["agentRuntimeId"]
                break
        if rt_id:
            break
    if not rt_id:
        print("  [bundles-runtime] Not present, skipping")
        return
    print(f"  [bundles-runtime] Deleting {target_name} runtimeId={rt_id}")
    try:
        eps = client.list_agent_runtime_endpoints(agentRuntimeId=rt_id).get("agentRuntimeEndpoints", [])
        for ep in eps:
            try:
                client.delete_agent_runtime_endpoint(
                    agentRuntimeId=rt_id,
                    agentRuntimeEndpointId=ep["agentRuntimeEndpointId"],
                )
            except Exception as e:
                print(f"  [bundles-runtime] endpoint delete warn: {e}")
        client.delete_agent_runtime(agentRuntimeId=rt_id)
        print("  [bundles-runtime] Deleted")
    except Exception as e:
        print(f"  [bundles-runtime] delete warn: {e}")


def run(cmd):
    print(f"  $ {cmd}")
    subprocess.run(cmd, shell=True, capture_output=True, text=True)


def main():
    print("=" * 50)
    print("  AgentCore Teardown")
    print("=" * 50)

    if not os.path.exists(STATE_FILE):
        print("\nNo agentcore-state.json found. Nothing to tear down.")
        return

    with open(STATE_FILE) as f:
        state = json.load(f)

    project_dir = state.get("projectDir", "")
    gateway_id = state.get("gatewayId", "")
    runtime_id = state.get("runtimeId", "")
    voice_runtime_id = state.get("voiceRuntimeId", "")
    registry_id = state.get("registryId", "")

    # Step 1: Delete the AgentCore CloudFormation stack (owns gateway, targets, runtime, memory)
    # The stack name follows the agentcore CLI convention: AgentCore-{project}-default
    cf = boto3.client("cloudformation", region_name=REGION)
    stack_name = None

    # Derive stack name from project dir (e.g. .agentcore-project/smarthome -> AgentCore-smarthome-default)
    if project_dir:
        project_name = os.path.basename(project_dir)
        stack_name = f"AgentCore-{project_name}-default"

    # Delete both text and voice stacks (voice is skipped if the state predates
    # the voice-runtime split).
    stack_names = []
    if stack_name:
        stack_names.append(stack_name)
    if voice_runtime_id:
        stack_names.append("AgentCore-smarthomevoice-default")

    if stack_names:
        print(f"\n[1/3] Deleting CloudFormation stack(s): {', '.join(stack_names)}")
        for sn in stack_names:
            try:
                cf.describe_stacks(StackName=sn)
                run(f"aws cloudformation delete-stack --stack-name {sn}")
                print(f"  Waiting for {sn} deletion...")
                run(f"aws cloudformation wait stack-delete-complete --stack-name {sn}")
                print(f"  {sn} deleted.")
            except cf.exceptions.ClientError:
                print(f"  {sn} not found, skipping.")

    # Step 2: Clean up specific resources by ID (safety net if stack delete missed them)
    print(f"\n[2/3] Cleaning up tracked resources...")
    client = boto3.client("bedrock-agentcore-control", region_name=REGION)
    data_client = boto3.client("bedrock-agentcore", region_name=REGION)

    # ── AgentCore Optimization (target-based A/B routing) cleanup ────────
    # Delete in order: A/B tests → per-variant online-eval configs →
    # optimization gateway (targets cascade) → optimization IAM roles →
    # toggle DDB row. Runtime endpoints (control/treatment) are removed by
    # the existing list_agent_runtime_endpoints loop below.
    print("  [opt-infra] Cleaning AgentCore Optimization resources…")
    try:
        for ab in data_client.list_ab_tests().get("abTests", []):
            tid = ab["abTestId"]
            try:
                data_client.delete_ab_test(abTestId=tid)
                print(f"  [opt-infra] Deleted A/B test {tid}")
            except Exception as e:
                print(f"  [opt-infra] Skipped A/B test {tid}: {e}")
    except Exception as e:
        print(f"  [opt-infra] list_ab_tests failed: {e}")

    for cfg_name in ("smarthome_control_online_eval", "smarthome_treatment_online_eval"):
        try:
            for c in client.list_online_evaluation_configs().get("onlineEvaluationConfigs", []):
                if c.get("onlineEvaluationConfigName") == cfg_name:
                    client.delete_online_evaluation_config(
                        onlineEvaluationConfigId=c["onlineEvaluationConfigId"]
                    )
                    print(f"  [opt-infra] Deleted online-eval {cfg_name}")
                    break
        except Exception as e:
            print(f"  [opt-infra] Skipped online-eval {cfg_name}: {e}")

    try:
        for g in client.list_gateways().get("items", []):
            if g.get("name") == "smarthome-optimization-gateway":
                gw_id = g["gatewayId"]
                for t in client.list_gateway_targets(
                        gatewayIdentifier=gw_id).get("items", []):
                    try:
                        client.delete_gateway_target(
                            gatewayIdentifier=gw_id, targetId=t["targetId"])
                    except Exception:
                        pass
                client.delete_gateway(gatewayIdentifier=gw_id)
                print(f"  [opt-infra] Deleted optimization gateway {gw_id}")
                break
    except Exception as e:
        print(f"  [opt-infra] Skipped optimization gateway: {e}")

    iam_client = boto3.client("iam")
    for role_name in ("smarthome-abtest-execution-role",
                      "smarthome-optimization-gateway-role",
                      "smarthome-optimization-online-eval-role"):
        try:
            for p in iam_client.list_role_policies(RoleName=role_name).get("PolicyNames", []):
                iam_client.delete_role_policy(RoleName=role_name, PolicyName=p)
            iam_client.delete_role(RoleName=role_name)
            print(f"  [opt-infra] Deleted IAM role {role_name}")
        except iam_client.exceptions.NoSuchEntityException:
            pass
        except Exception as e:
            print(f"  [opt-infra] Skipped IAM role {role_name}: {e}")

    try:
        ddb = boto3.resource("dynamodb", region_name=REGION).Table("smarthome-skills")
        for sk in ("__opt_routing_enabled__", "__opt_abtest_enabled__"):  # last is legacy
            ddb.delete_item(Key={"userId": "__global__", "skillName": sk})
        # Per-test/bundle/rec rows keyed under various userIds — leave them
        # (admins may want to inspect history). Stale rows are filtered
        # server-side by the redesigned list handlers.
        print("  [opt-infra] Cleared optimization toggle DDB row(s)")
    except Exception as e:
        print(f"  [opt-infra] Skipped DDB cleanup: {e}")

    # Bundles runtime (§8.13) — looked up by name, no ID in saved state.
    _delete_bundles_runtime_by_name(client)

    for rt_id, label in ((runtime_id, "text"), (voice_runtime_id, "voice")):
        if not rt_id:
            continue
        print(f"  Deleting {label} runtime: {rt_id}")
        try:
            eps = client.list_agent_runtime_endpoints(agentRuntimeId=rt_id).get("agentRuntimeEndpoints", [])
            for ep in eps:
                client.delete_agent_runtime_endpoint(agentRuntimeId=rt_id, agentRuntimeEndpointId=ep["agentRuntimeEndpointId"])
            client.delete_agent_runtime(agentRuntimeId=rt_id)
            print(f"  {label.capitalize()} runtime deleted.")
        except Exception as e:
            print(f"  Skipped (already deleted or not found): {e}")

    if gateway_id:
        print(f"  Deleting gateway: {gateway_id}")
        try:
            targets = client.list_gateway_targets(gatewayIdentifier=gateway_id).get("targets", [])
            for t in targets:
                client.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=t["targetId"])
            client.delete_gateway(gatewayIdentifier=gateway_id)
            print("  Gateway deleted.")
        except Exception as e:
            print(f"  Skipped (already deleted or not found): {e}")

    if registry_id:
        print(f"  Deleting registry: {registry_id}")
        try:
            # Delete all records first — DeleteRegistry requires an empty registry
            token = None
            while True:
                kwargs = {"registryId": registry_id, "maxResults": 50}
                if token:
                    kwargs["nextToken"] = token
                resp = client.list_registry_records(**kwargs)
                for r in resp.get("registryRecords", []):
                    try:
                        client.delete_registry_record(
                            registryId=registry_id, recordId=r["recordId"]
                        )
                    except Exception as e:
                        print(f"    Skipped record {r.get('recordId', '')}: {e}")
                token = resp.get("nextToken")
                if not token:
                    break
            client.delete_registry(registryId=registry_id)
            print("  Registry deleted.")
        except Exception as e:
            print(f"  Skipped (already deleted or not found): {e}")

    # Step 3: Clean up local state
    print(f"\n[3/3] Cleaning up local files...")
    os.remove(STATE_FILE)
    print(f"  Removed {STATE_FILE}")

    agentcore_dir = os.path.join(PROJECT_ROOT, ".agentcore-project")
    if os.path.exists(agentcore_dir):
        import shutil
        shutil.rmtree(agentcore_dir)
        print(f"  Removed {agentcore_dir}")

    print("\nTeardown complete.")


if __name__ == "__main__":
    main()
