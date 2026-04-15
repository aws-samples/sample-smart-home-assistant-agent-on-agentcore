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
    discovery_lambda_arn = outputs["IoTDiscoveryLambdaArn"]
    kb_query_lambda_arn = outputs.get("KBQueryLambdaArn", "")
    aoss_endpoint = outputs.get("AOSSCollectionEndpoint", "")
    aoss_collection_arn = outputs.get("AOSSCollectionArn", "")
    kb_service_role_arn = outputs.get("KBServiceRoleArn", "")
    kb_docs_bucket = outputs.get("KBDocsBucketName", "")
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
    print("\n[4/8] Adding AgentCore Gateway (JWT auth for per-user tool control)...")
    r = run(
        f'agentcore add gateway --name SmartHomeGateway '
        f'--authorizer-type CUSTOM_JWT '
        f'--discovery-url {discovery_url} '
        f'--allowed-audience {client_id}',
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

    # Write discovery tool schema
    with open(os.path.join(project_dir, "discovery-tools.json"), "w") as f:
        json.dump([{
            "name": "discover_devices",
            "description": (
                "Discover all smart home devices available to the current user. "
                "Returns a list of devices with their type, display name, and supported actions."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        }], f, indent=2)

    r = run(
        f'agentcore add gateway-target --name SmartHomeDeviceDiscovery '
        f'--gateway SmartHomeGateway '
        f'--type lambda-function-arn '
        f'--lambda-arn {discovery_lambda_arn} '
        f'--tool-schema-file discovery-tools.json',
        cwd=project_dir,
    )
    if r.returncode != 0:
        raise Exception("Failed to add discovery gateway target")

    # Add KB query Lambda target to gateway (if KB infrastructure exists)
    if kb_query_lambda_arn:
        print("  Adding KB query Lambda target to gateway...")
        with open(os.path.join(project_dir, "kb-query-tools.json"), "w") as f:
            json.dump([{
                "name": "query_knowledge_base",
                "description": (
                    "Query the enterprise knowledge base to retrieve relevant documents. "
                    "Use this when users ask about company documents, product manuals, "
                    "troubleshooting guides, or internal knowledge."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query to find relevant documents",
                        },
                        "user_id": {
                            "type": "string",
                            "description": "User email for scoped retrieval. The agent MUST pass the current user's email so they can access their private documents.",
                        },
                    },
                    "required": ["query"],
                },
            }], f, indent=2)

        r = run(
            f'agentcore add gateway-target --name SmartHomeKnowledgeBase '
            f'--gateway SmartHomeGateway '
            f'--type lambda-function-arn '
            f'--lambda-arn {kb_query_lambda_arn} '
            f'--tool-schema-file kb-query-tools.json',
            cwd=project_dir,
        )
        if r.returncode != 0:
            print("  Warning: Failed to add KB query gateway target (non-fatal)")

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

    # --------------------------------------------------------
    # Post-deploy: Initialize Bedrock Knowledge Base
    # --------------------------------------------------------
    kb_id = ""
    kb_data_source_id = ""
    if aoss_endpoint and aoss_collection_arn and kb_service_role_arn and kb_docs_bucket:
        print("\nInitializing Bedrock Knowledge Base...")

        # Step A: Ensure caller's IAM identity is in AOSS data access policy
        print("  Ensuring AOSS data access for current IAM identity...")
        caller_arn = boto3.client("sts", region_name=REGION).get_caller_identity()["Arn"]
        aoss_client = boto3.client("opensearchserverless", region_name=REGION)
        try:
            policy_resp = aoss_client.get_access_policy(name="smarthome-kb-data", type="data")
            policy_detail = policy_resp["accessPolicyDetail"]
            policy_doc = policy_detail["policy"]
            # Add caller ARN if not already a principal
            principals = policy_doc[0].get("Principal", [])
            if caller_arn not in principals:
                principals.append(caller_arn)
                policy_doc[0]["Principal"] = principals
                aoss_client.update_access_policy(
                    name="smarthome-kb-data",
                    type="data",
                    policyVersion=policy_detail["policyVersion"],
                    policy=json.dumps(policy_doc),
                )
                print(f"  Added {caller_arn} to AOSS data access policy")
                print("  Waiting 30s for AOSS policy propagation...")
                time.sleep(30)
            else:
                print(f"  IAM identity already in AOSS data access policy")
        except Exception as e:
            print(f"  Warning: Could not update AOSS data access policy: {e}")
            print("  Will attempt index creation anyway...")

        # Step B: Create AOSS vector index using opensearch-py
        print("  Creating AOSS vector index...")
        endpoint = aoss_endpoint if aoss_endpoint.startswith("https://") else f"https://{aoss_endpoint}"
        host = endpoint.replace("https://", "")
        index_name = "smarthome-kb-index"

        # Install opensearch-py if needed
        try:
            from opensearchpy import OpenSearch, RequestsHttpConnection
            from requests_aws4auth import AWS4Auth
        except ImportError:
            import subprocess as _sp
            _sp.run([sys.executable, "-m", "pip", "install", "-q", "opensearch-py", "requests-aws4auth"], check=True)
            from opensearchpy import OpenSearch, RequestsHttpConnection
            from requests_aws4auth import AWS4Auth

        session = boto3.Session(region_name=REGION)
        credentials = session.get_credentials().get_frozen_credentials()
        awsauth = AWS4Auth(
            credentials.access_key, credentials.secret_key, REGION, "aoss",
            session_token=credentials.token,
        )
        os_client = OpenSearch(
            hosts=[{"host": host, "port": 443}],
            http_auth=awsauth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
            timeout=30,
        )

        index_body = {
            "settings": {"index": {"knn": True, "knn.algo_param.ef_search": 512}},
            "mappings": {
                "properties": {
                    "bedrock-knowledge-base-default-vector": {
                        "type": "knn_vector", "dimension": 1024,
                        "method": {"engine": "faiss", "space_type": "l2", "name": "hnsw",
                                   "parameters": {"ef_construction": 512, "m": 16}},
                    },
                    "AMAZON_BEDROCK_TEXT_CHUNK": {"type": "text"},
                    "AMAZON_BEDROCK_METADATA": {"type": "text"},
                }
            },
        }

        try:
            os_client.indices.create(index=index_name, body=index_body)
            print("  AOSS index created successfully")
        except Exception as e:
            if "resource_already_exists_exception" in str(e):
                print("  AOSS index already exists")
            else:
                raise Exception(f"Failed to create AOSS index: {e}")

        # Step B: Create Bedrock Knowledge Base
        print("  Creating Bedrock Knowledge Base...")
        bedrock_agent = boto3.client("bedrock-agent", region_name=REGION)
        embedding_model_arn = f"arn:aws:bedrock:{REGION}::foundation-model/cohere.embed-multilingual-v3"
        try:
            kb_resp = bedrock_agent.create_knowledge_base(
                name="SmartHomeEnterpriseKB",
                description="Enterprise knowledge base for smart home assistant",
                roleArn=kb_service_role_arn,
                knowledgeBaseConfiguration={
                    "type": "VECTOR",
                    "vectorKnowledgeBaseConfiguration": {
                        "embeddingModelArn": embedding_model_arn,
                    },
                },
                storageConfiguration={
                    "type": "OPENSEARCH_SERVERLESS",
                    "opensearchServerlessConfiguration": {
                        "collectionArn": aoss_collection_arn,
                        "vectorIndexName": index_name,
                        "fieldMapping": {
                            "vectorField": "bedrock-knowledge-base-default-vector",
                            "textField": "AMAZON_BEDROCK_TEXT_CHUNK",
                            "metadataField": "AMAZON_BEDROCK_METADATA",
                        },
                    },
                },
            )
            kb_id = kb_resp["knowledgeBase"]["knowledgeBaseId"]
            print(f"  Knowledge Base created: {kb_id}")

            # Wait for ACTIVE
            for _ in range(30):
                kb = bedrock_agent.get_knowledge_base(knowledgeBaseId=kb_id)
                if kb["knowledgeBase"]["status"] == "ACTIVE":
                    break
                time.sleep(2)

            # Step C: Create S3 data source
            print("  Creating S3 data source...")
            ds_resp = bedrock_agent.create_data_source(
                knowledgeBaseId=kb_id,
                name="SmartHomeKBDocuments",
                dataSourceConfiguration={
                    "type": "S3",
                    "s3Configuration": {
                        "bucketArn": f"arn:aws:s3:::{kb_docs_bucket}",
                    },
                },
            )
            kb_data_source_id = ds_resp["dataSource"]["dataSourceId"]
            print(f"  Data source created: {kb_data_source_id}")

            # Store KB config in DynamoDB
            skills_table = outputs.get("SkillsTableName", "smarthome-skills")
            ddb = boto3.resource("dynamodb", region_name=REGION)
            ddb_table = ddb.Table(skills_table)
            from datetime import datetime, timezone
            ddb_table.put_item(Item={
                "userId": "__kb_config__",
                "skillName": "__default__",
                "knowledgeBaseId": kb_id,
                "dataSourceId": kb_data_source_id,
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            })
            print("  KB config stored in DynamoDB")

        except bedrock_agent.exceptions.ConflictException:
            print("  Knowledge Base already exists, looking up existing...")
            list_resp = bedrock_agent.list_knowledge_bases(maxResults=100)
            for kb_summary in list_resp.get("knowledgeBaseSummaries", []):
                if kb_summary.get("name") == "SmartHomeEnterpriseKB":
                    kb_id = kb_summary["knowledgeBaseId"]
                    # Get data source
                    ds_list = bedrock_agent.list_data_sources(knowledgeBaseId=kb_id, maxResults=10)
                    for ds in ds_list.get("dataSourceSummaries", []):
                        kb_data_source_id = ds["dataSourceId"]
                        break
                    print(f"  Found existing KB: {kb_id}, DS: {kb_data_source_id}")
                    break
        except Exception as e:
            print(f"  Warning: Failed to create Knowledge Base: {e}")
        # Create default KB folders (__shared__ + admin user)
        print("  Creating default KB folders...")
        s3_setup = boto3.client("s3", region_name=REGION)
        for folder in ["__shared__/", "admin@smarthome.local/"]:
            try:
                s3_setup.put_object(Bucket=kb_docs_bucket, Key=folder, Body=b"", ContentType="application/x-directory")
            except Exception:
                pass
        print(f"  Created __shared__/ and admin@smarthome.local/ in s3://{kb_docs_bucket}")

    else:
        print("\nSkipping KB initialization (missing AOSS/KB infrastructure outputs)")

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
            # Propagate Authorization header to agent code so it can forward
            # the user's JWT to the CUSTOM_JWT gateway for per-user policy evaluation.
            requestHeaderConfiguration={
                "requestHeaderAllowlist": ["Authorization"],
            },
        )
        if rt_info.get("authorizerConfiguration"):
            update_kwargs["authorizerConfiguration"] = rt_info["authorizerConfiguration"]
        ac.update_agent_runtime(**update_kwargs)
        print(f"  Patched MODEL_ID, AWS_REGION, SKILLS_TABLE_NAME={skills_table}")
        print(f"  Enabled Authorization header propagation to agent code")

        # Grant runtime role DynamoDB read access for skills table
        role_arn = rt_info.get("roleArn", "")
        if role_arn:
            role_name = role_arn.split("/")[-1]
            iam_client = boto3.client("iam", region_name=REGION)
            policy_doc = json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan", "dynamodb:PutItem"],
                        "Resource": f"arn:aws:dynamodb:{REGION}:{account_id}:table/{skills_table}",
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"],
                        "Resource": "*",
                    },
                ],
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
            # Get memory ID from runtime env vars (auto-set by agentcore CLI)
            memory_id = ""
            for k, v in existing_env.items():
                if k.startswith("MEMORY_") and k.endswith("_ID"):
                    memory_id = v
                    break
            admin_env = {
                "SKILLS_TABLE_NAME": outputs.get("SkillsTableName", "smarthome-skills"),
                "AGENT_RUNTIME_ARN": runtime_arn,
                "AWS_REGION_OVERRIDE": REGION,
                "COGNITO_USER_POOL_ID": outputs.get("UserPoolId", ""),
                "GATEWAY_ID": gateway_id,
                "MEMORY_ID": memory_id,
            }
            # Preserve SKILL_FILES_BUCKET from CDK stack
            skill_files_bucket = outputs.get("SkillFilesBucketName", "")
            if skill_files_bucket:
                admin_env["SKILL_FILES_BUCKET"] = skill_files_bucket
            # KB-related env vars
            if kb_docs_bucket:
                admin_env["KB_DOCS_BUCKET"] = kb_docs_bucket
            if aoss_endpoint:
                admin_env["AOSS_ENDPOINT"] = aoss_endpoint
            if aoss_collection_arn:
                admin_env["AOSS_COLLECTION_ARN"] = aoss_collection_arn
            if kb_service_role_arn:
                admin_env["KB_SERVICE_ROLE_ARN"] = kb_service_role_arn
            if kb_id:
                admin_env["KB_ID"] = kb_id
            if kb_data_source_id:
                admin_env["KB_DATA_SOURCE_ID"] = kb_data_source_id
            lambda_client.update_function_configuration(
                FunctionName="smarthome-admin-api",
                Environment={"Variables": admin_env},
            )
            print(f"  Patched admin Lambda with AGENT_RUNTIME_ARN")
            print(f"  Patched admin Lambda with GATEWAY_ID={gateway_id}")
            if skill_files_bucket:
                print(f"  Patched admin Lambda with SKILL_FILES_BUCKET={skill_files_bucket}")
        except Exception as e:
            print(f"  Warning: Failed to patch admin Lambda: {e}")

        # Patch user-init Lambda with GATEWAY_ID
        try:
            user_init_resp = lambda_client.get_function_configuration(
                FunctionName="smarthome-user-init"
            )
            user_init_env = user_init_resp.get("Environment", {}).get("Variables", {})
            user_init_env["GATEWAY_ID"] = gateway_id
            if kb_docs_bucket:
                user_init_env["KB_DOCS_BUCKET"] = kb_docs_bucket
            lambda_client.update_function_configuration(
                FunctionName="smarthome-user-init",
                Environment={"Variables": user_init_env},
            )
            print(f"  Patched user-init Lambda with GATEWAY_ID={gateway_id}")
            if kb_docs_bucket:
                print(f"  Patched user-init Lambda with KB_DOCS_BUCKET={kb_docs_bucket}")
        except Exception as e:
            print(f"  Warning: Failed to patch user-init Lambda: {e}")

    # Patch KB Gateway target with inline tool schema.
    # agentcore deploy stores the schema in S3 which the Gateway may cache.
    # Updating to inline guarantees the agent sees the latest schema (including user_id).
    if gateway_id and kb_query_lambda_arn:
        print("Patching KB Gateway target with inline tool schema...")
        ac_control = boto3.client("bedrock-agentcore-control", region_name=REGION)
        try:
            targets = ac_control.list_gateway_targets(gatewayIdentifier=gateway_id)
            for t in targets.get("items", []):
                if "KnowledgeBase" in t.get("name", ""):
                    target = ac_control.get_gateway_target(
                        gatewayIdentifier=gateway_id, targetId=t["targetId"])
                    creds = target.get("credentialProviderConfigurations", [
                        {"credentialProviderType": "GATEWAY_IAM_ROLE"}])
                    ac_control.update_gateway_target(
                        gatewayIdentifier=gateway_id,
                        targetId=t["targetId"],
                        name=t["name"],
                        targetConfiguration={
                            "mcp": {
                                "lambda": {
                                    "lambdaArn": kb_query_lambda_arn,
                                    "toolSchema": {
                                        "inlinePayload": [{
                                            "name": "query_knowledge_base",
                                            "description": (
                                                "Query the enterprise knowledge base to retrieve "
                                                "relevant documents. Use this when users ask about "
                                                "company documents, product manuals, troubleshooting "
                                                "guides, or internal knowledge."
                                            ),
                                            "inputSchema": {
                                                "type": "object",
                                                "properties": {
                                                    "query": {
                                                        "type": "string",
                                                        "description": "The search query to find relevant documents",
                                                    },
                                                    "user_id": {
                                                        "type": "string",
                                                        "description": "User email for scoped retrieval. MUST pass the current user email.",
                                                    },
                                                },
                                                "required": ["query"],
                                            },
                                        }],
                                    },
                                },
                            },
                        },
                        credentialProviderConfigurations=creds,
                    )
                    print(f"  Updated target {t['name']} with inline schema (user_id included)")
                    break
        except Exception as e:
            print(f"  Warning: Failed to patch KB Gateway target: {e}")

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
            "knowledgeBaseId": kb_id, "dataSourceId": kb_data_source_id,
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
