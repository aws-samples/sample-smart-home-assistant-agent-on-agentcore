#!/usr/bin/env python3
"""
Post-CDK script to deploy AgentCore resources (Gateway, Lambda Target, Agent Runtime).
Uses the agentcore CLI which handles CloudFormation deployment via its own CDK stack.

Prerequisites:
  - CDK stack (SmartHomeAssistantStack) already deployed
  - agentcore CLI installed (pip install strands-agents-builder)
  - boto3 installed
"""

import json
import subprocess
import sys
import time
import os
import shutil
import boto3

STACK_NAME = "SmartHomeAssistantStack"
REGION = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-west-2"))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
AGENTCORE_DIR = os.path.join(PROJECT_ROOT, ".agentcore-project")


def get_stack_outputs():
    cf = boto3.client("cloudformation", region_name=REGION)
    resp = cf.describe_stacks(StackName=STACK_NAME)
    return {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}


def get_account_id():
    return boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]


def run(cmd, cwd=None):
    """Run shell command, print output, return result."""
    print(f"  $ {cmd}")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
    for line in (r.stdout + r.stderr).strip().split("\n"):
        cleaned = line.strip()
        # Skip spinner-only lines
        if cleaned and not all(c in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ \x1b[K" for c in cleaned):
            print(f"    {cleaned}")
    return r


def main():
    print("=" * 60)
    print("  AgentCore Setup (Gateway + Lambda Target + Runtime + Observability + Eval)")
    print("=" * 60)

    # Read CDK stack outputs
    outputs = get_stack_outputs()
    account_id = get_account_id()
    user_pool_id = outputs["UserPoolId"]
    client_id = outputs["UserPoolClientId"]
    lambda_arn = outputs["IoTControlLambdaArn"]
    discovery_url = f"https://cognito-idp.{REGION}.amazonaws.com/{user_pool_id}/.well-known/openid-configuration"
    agent_code_src = os.path.join(PROJECT_ROOT, "agent")

    print(f"\n  Region:  {REGION}")
    print(f"  Account: {account_id}")

    # --------------------------------------------------------
    # Step 1: Create agentcore project (with default agent)
    # --------------------------------------------------------
    print("\n[1/8] Creating agentcore project...")
    if os.path.exists(AGENTCORE_DIR):
        shutil.rmtree(AGENTCORE_DIR)
    os.makedirs(AGENTCORE_DIR)

    r = run("agentcore create --name smarthome --defaults", cwd=AGENTCORE_DIR)
    if r.returncode != 0:
        raise Exception("agentcore create failed")
    project_dir = os.path.join(AGENTCORE_DIR, "smarthome")

    # --------------------------------------------------------
    # Step 2: Replace default agent code with our SmartHome agent
    # --------------------------------------------------------
    print("\n[2/8] Injecting SmartHome agent code...")
    default_app = os.path.join(project_dir, "app", "smarthome")
    if os.path.exists(default_app):
        shutil.rmtree(default_app)
    shutil.copytree(agent_code_src, default_app)

    # Patch agentcore.json: set entrypoint, JWT auth, env vars
    config_file = os.path.join(project_dir, "agentcore", "agentcore.json")
    with open(config_file) as f:
        config = json.load(f)

    if config.get("runtimes"):
        rt = config["runtimes"][0]
        rt["entrypoint"] = "agent.py"
        rt["authorizerType"] = "CUSTOM_JWT"
        rt["authorizerConfiguration"] = {
            "customJwtAuthorizer": {
                "discoveryUrl": discovery_url,
                "allowedAudience": [client_id],
            }
        }
        rt["environmentVariables"] = {
            "MODEL_ID": "moonshotai.kimi-k2.5",
            "AWS_REGION": REGION,
        }

    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)

    # Seed aws-targets.json (required for non-interactive deploy)
    targets_file = os.path.join(project_dir, "agentcore", "aws-targets.json")
    with open(targets_file, "w") as f:
        json.dump([{"name": "default", "region": REGION, "account": account_id}], f, indent=2)

    # --------------------------------------------------------
    # Step 3: Add AgentCore Memory (managed by agentcore CLI)
    # --------------------------------------------------------
    print("\n[3/8] Adding AgentCore Memory...")
    r = run(
        "agentcore add memory --name SmartHomeMemory "
        "--strategies SEMANTIC,SUMMARIZATION,USER_PREFERENCE",
        cwd=project_dir,
    )
    if r.returncode != 0:
        raise Exception("Failed to add memory")

    # --------------------------------------------------------
    # Step 4: Add AgentCore Gateway
    # --------------------------------------------------------
    print("\n[4/8] Adding AgentCore Gateway...")
    r = run(
        f'agentcore add gateway --name SmartHomeGateway '
        f'--authorizer-type NONE',
        cwd=project_dir,
    )
    if r.returncode != 0:
        raise Exception("Failed to add gateway")

    # --------------------------------------------------------
    # Step 5: Add Lambda target to gateway
    # --------------------------------------------------------
    print("\n[5/8] Adding Lambda target to gateway...")

    # Write tool schema file
    with open(os.path.join(project_dir, "tools.json"), "w") as f:
        json.dump([{
            "name": "control_device",
            "description": (
                "Send a control command to a smart home device. "
                "Devices: led_matrix (LED Matrix, modes: rainbow/breathing/chase/sparkle/fire/ocean/aurora), "
                "rice_cooker (modes: white_rice/brown_rice/porridge/steam), "
                "fan (speed 0-3, oscillation), "
                "oven (modes: bake/broil/convection, temp 200-500F)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "device_type": {"type": "string", "description": "Device: led_matrix, rice_cooker, fan, or oven"},
                    "command": {
                        "type": "object",
                        "description": "Command with action field and parameters.",
                        "properties": {"action": {"type": "string", "description": "Action to perform"}},
                        "required": ["action"],
                    },
                },
                "required": ["device_type", "command"],
            },
        }], f, indent=2)

    r = run(
        f'agentcore add gateway-target --name SmartHomeDeviceControl '
        f'--gateway SmartHomeGateway '
        f'--type lambda-function-arn '
        f'--lambda-arn {lambda_arn} '
        f'--tool-schema-file tools.json',
        cwd=project_dir,
    )
    if r.returncode != 0:
        raise Exception("Failed to add gateway target")

    # --------------------------------------------------------
    # Step 6: Add evaluator (LLM-as-a-Judge for response quality)
    # --------------------------------------------------------
    print("\n[6/8] Adding evaluator...")
    r = run(
        'agentcore add evaluator --name SmartHomeQuality '
        '--level SESSION '
        '--type llm-as-a-judge '
        '--model us.anthropic.claude-sonnet-4-20250514-v1:0 '
        '--rating-scale 1-5-quality '
        '--instructions "Evaluate the smart home assistant response. '
        'Consider: (1) Did the agent correctly understand the user intent? '
        '(2) Did the agent use the right device control tools? '
        '(3) Was the response helpful and concise? '
        'Context: {context}"',
        cwd=project_dir,
    )
    if r.returncode != 0:
        print("  Warning: Failed to add evaluator (non-fatal)")

    # --------------------------------------------------------
    # Step 7: Add online evaluation config
    # --------------------------------------------------------
    print("\n[7/8] Adding online evaluation config...")
    r = run(
        'agentcore add online-eval --name SmartHomeOnlineEval '
        '--runtime smarthome '
        '--evaluator SmartHomeQuality '
        '--sampling-rate 100 '
        '--enable-on-create',
        cwd=project_dir,
    )
    if r.returncode != 0:
        print("  Warning: Failed to add online eval config (non-fatal)")

    # --------------------------------------------------------
    # Step 8: Deploy all AgentCore resources
    # --------------------------------------------------------
    print("\n[8/8] Deploying AgentCore resources...")
    r = run("agentcore deploy -y --verbose", cwd=project_dir)
    if r.returncode != 0:
        raise Exception("agentcore deploy failed — check log above")

    # --------------------------------------------------------
    # Post-deploy: fetch IDs from AgentCore CFN stack outputs
    # --------------------------------------------------------
    print("\nFetching deployed resource info...")
    cf = boto3.client("cloudformation", region_name=REGION)
    ac_stack_name = "AgentCore-smarthome-default"
    ac_resp = cf.describe_stacks(StackName=ac_stack_name)
    ac_outputs = {o["OutputKey"]: o["OutputValue"] for o in ac_resp["Stacks"][0].get("Outputs", [])}

    gateway_id = gateway_url = runtime_id = runtime_arn = ""
    for key, val in ac_outputs.items():
        if "GatewayIdOutput" in key:
            gateway_id = val
        elif "GatewayUrlOutput" in key:
            gateway_url = val
        elif "RuntimeIdOutput" in key:
            runtime_id = val
        elif "RuntimeArnOutput" in key:
            runtime_arn = val

    # Patch runtime env vars (agentcore CLI drops custom env vars during deploy)
    if runtime_id:
        print("Patching runtime environment variables...")
        ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
        rt_info = ac.get_agent_runtime(agentRuntimeId=runtime_id)
        existing_env = rt_info.get("environmentVariables", {})
        existing_env["MODEL_ID"] = "moonshotai.kimi-k2.5"
        existing_env["AWS_REGION"] = REGION
        # Add skills table name for dynamic skill loading from DynamoDB
        skills_table = outputs.get("SkillsTableName", "smarthome-skills")
        existing_env["SKILLS_TABLE_NAME"] = skills_table
        update_kwargs = dict(
            agentRuntimeId=runtime_id,
            agentRuntimeArtifact=rt_info["agentRuntimeArtifact"],
            roleArn=rt_info["roleArn"],
            networkConfiguration=rt_info["networkConfiguration"],
            environmentVariables=existing_env,
        )
        if rt_info.get("authorizerConfiguration"):
            update_kwargs["authorizerConfiguration"] = rt_info["authorizerConfiguration"]
        ac.update_agent_runtime(**update_kwargs)
        print(f"  Patched MODEL_ID, AWS_REGION, SKILLS_TABLE_NAME={skills_table}")

        # Grant runtime role DynamoDB read access for skills table
        role_arn = rt_info.get("roleArn", "")
        if role_arn:
            role_name = role_arn.split("/")[-1]
            iam_client = boto3.client("iam", region_name=REGION)
            policy_doc = json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan", "dynamodb:PutItem"],
                    "Resource": f"arn:aws:dynamodb:{REGION}:{account_id}:table/{skills_table}",
                }],
            })
            try:
                iam_client.put_role_policy(
                    RoleName=role_name,
                    PolicyName="DynamoDBSkillsReadAccess",
                    PolicyDocument=policy_doc,
                )
                print(f"  Granted DynamoDB read access to role {role_name}")
            except Exception as e:
                print(f"  Warning: Failed to attach DynamoDB policy: {e}")

    # Patch admin Lambda with runtime ARN (needed for stop-runtime-session)
    if runtime_arn:
        try:
            lambda_client = boto3.client("lambda", region_name=REGION)
            lambda_client.update_function_configuration(
                FunctionName="smarthome-admin-api",
                Environment={
                    "Variables": {
                        "SKILLS_TABLE_NAME": outputs.get("SkillsTableName", "smarthome-skills"),
                        "AGENT_RUNTIME_ARN": runtime_arn,
                        "AWS_REGION_OVERRIDE": REGION,
                    }
                },
            )
            print(f"  Patched admin Lambda with AGENT_RUNTIME_ARN")
        except Exception as e:
            print(f"  Warning: Failed to patch admin Lambda: {e}")

    # Re-write config.js for ALL frontends.
    # CDK BucketDeployment syncs dist/ to S3 and removes files not in the source,
    # which wipes config.js that was written by CDK custom resources.
    # We re-write them here after everything is deployed.
    s3 = boto3.client("s3", region_name=REGION)
    cf_client = boto3.client("cloudfront", region_name=REGION)

    def _invalidate(dist_id):
        if dist_id:
            cf_client.create_invalidation(
                DistributionId=dist_id,
                InvalidationBatch={"Paths": {"Quantity": 1, "Items": ["/*"]},
                                   "CallerReference": str(time.time())})

    # Device simulator config.js
    ds_bucket = outputs.get("DeviceSimBucketName", "")
    if ds_bucket:
        ds_config = f"""window.__CONFIG__ = {{
  iotEndpoint: "{outputs['IoTEndpointOutput']}",
  region: "{REGION}",
  cognitoIdentityPoolId: "{outputs['IdentityPoolId']}"
}};"""
        print("Updating device simulator config.js...")
        s3.put_object(Bucket=ds_bucket, Key="config.js",
                      Body=ds_config, ContentType="application/javascript")
        _invalidate(outputs.get("DeviceSimDistributionId", ""))

    if runtime_arn:
        chatbot_config = f"""window.__CONFIG__ = {{
  cognitoUserPoolId: "{outputs['UserPoolId']}",
  cognitoClientId: "{outputs['UserPoolClientId']}",
  cognitoDomain: "{outputs['CognitoDomain']}",
  agentRuntimeArn: "{runtime_arn}",
  region: "{REGION}"
}};"""
        print("Updating chatbot config.js...")
        s3.put_object(Bucket=outputs["ChatbotBucketName"], Key="config.js",
                      Body=chatbot_config, ContentType="application/javascript")
        _invalidate(outputs.get("ChatbotDistributionId", ""))

    admin_bucket = outputs.get("AdminConsoleBucketName", "")
    admin_api_url = outputs.get("AdminApiUrl", "")
    if admin_bucket and admin_api_url:
        admin_config = f"""window.__CONFIG__ = {{
  cognitoUserPoolId: "{outputs['UserPoolId']}",
  cognitoClientId: "{outputs['UserPoolClientId']}",
  adminApiUrl: "{admin_api_url}",
  agentRuntimeArn: "{runtime_arn}",
  region: "{REGION}"
}};"""
        print("Updating admin console config.js...")
        s3.put_object(Bucket=admin_bucket, Key="config.js",
                      Body=admin_config, ContentType="application/javascript")
        _invalidate(outputs.get("AdminConsoleDistributionId", ""))

    # Save state for teardown
    state_file = os.path.join(PROJECT_ROOT, "agentcore-state.json")
    with open(state_file, "w") as f:
        json.dump({
            "gatewayId": gateway_id, "runtimeId": runtime_id,
            "runtimeArn": runtime_arn, "projectDir": project_dir,
        }, f, indent=2)

    print("\n" + "=" * 60)
    print("  AgentCore Setup Complete!")
    print("=" * 60)
    print(f"\n  Gateway ID:    {gateway_id}")
    print(f"  Gateway URL:   {gateway_url}")
    print(f"  Runtime ID:    {runtime_id}")
    print(f"  Runtime ARN:   {runtime_arn}")
    print(f"\n  Device Sim:    {outputs.get('DeviceSimulatorUrl', '')}")
    print(f"  Chatbot:       {outputs.get('ChatbotUrl', '')}")
    print(f"  Admin Console: {outputs.get('AdminConsoleUrl', '')}")
    print(f"  Admin API:     {outputs.get('AdminApiUrl', '')}")
    if outputs.get("AdminUsername"):
        print(f"\n  Admin Login:   {outputs['AdminUsername']} / {outputs.get('AdminPassword', '')}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
