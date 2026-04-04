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

    # Step 1: Delete the AgentCore CloudFormation stack (owns gateway, targets, runtime, memory)
    # The stack name follows the agentcore CLI convention: AgentCore-{project}-default
    cf = boto3.client("cloudformation", region_name=REGION)
    stack_name = None

    # Derive stack name from project dir (e.g. .agentcore-project/smarthome -> AgentCore-smarthome-default)
    if project_dir:
        project_name = os.path.basename(project_dir)
        stack_name = f"AgentCore-{project_name}-default"

    if stack_name:
        print(f"\n[1/3] Deleting CloudFormation stack: {stack_name}")
        try:
            cf.describe_stacks(StackName=stack_name)
            run(f"aws cloudformation delete-stack --stack-name {stack_name}")
            print("  Waiting for stack deletion...")
            run(f"aws cloudformation wait stack-delete-complete --stack-name {stack_name}")
            print("  Stack deleted.")
        except cf.exceptions.ClientError:
            print("  Stack not found, skipping.")

    # Step 2: Clean up specific resources by ID (safety net if stack delete missed them)
    print(f"\n[2/3] Cleaning up tracked resources...")
    client = boto3.client("bedrock-agentcore-control", region_name=REGION)

    if runtime_id:
        print(f"  Deleting runtime: {runtime_id}")
        try:
            eps = client.list_agent_runtime_endpoints(agentRuntimeId=runtime_id).get("agentRuntimeEndpoints", [])
            for ep in eps:
                client.delete_agent_runtime_endpoint(agentRuntimeId=runtime_id, agentRuntimeEndpointId=ep["agentRuntimeEndpointId"])
            client.delete_agent_runtime(agentRuntimeId=runtime_id)
            print("  Runtime deleted.")
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
