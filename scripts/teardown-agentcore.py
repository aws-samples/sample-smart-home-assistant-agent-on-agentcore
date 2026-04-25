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
