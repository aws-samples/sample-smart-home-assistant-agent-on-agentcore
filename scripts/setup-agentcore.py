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
import uuid
from datetime import datetime, timezone

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


def _seed_demo_a2a_records(ac_control, registry_id, admin_sub, admin_email, dynamo_table):
    """Idempotently seed 3 demo A2A records + ownership rows. Prints progress."""
    import json as _json
    import time as _time
    import uuid as _uuid

    if not registry_id:
        print("  [a2a-seed] registry_id empty; skipping")
        return

    demos = [
        {
            "name": "energy-optimization-agent",
            "description": "Recommends schedules and modes to reduce smart-home energy use.",
            "endpoint": "https://example.com/a2a/energy-optimization-agent",
            "version": "1.0.0",
            "provider": {"organization": "SmartHome Demo", "url": "https://example.com/smarthome-demo"},
            "capabilities": {"streaming": True, "pushNotifications": True, "stateTransitionHistory": False},
            "authentication": {"schemes": ["none"]},
            "tags": ["energy", "demo", "smarthome"],
            "skills": [
                {"id": "analyze-usage", "name": "Analyze Usage",
                 "description": "Summarises device runtime and energy draw over a time window.",
                 "examples": ["Summarise last week's fan usage",
                              "Which device consumed the most power yesterday?"]},
                {"id": "suggest-schedule", "name": "Suggest Schedule",
                 "description": "Proposes a daily schedule that meets comfort targets at lowest cost.",
                 "examples": ["Build me an energy-efficient schedule for weekdays"]},
            ],
        },
        {
            "name": "home-security-agent",
            "description": "Evaluates camera/door-sensor events and escalates alerts when needed.",
            "endpoint": "https://example.com/a2a/home-security-agent",
            "version": "1.0.0",
            "provider": {"organization": "SmartHome Demo", "url": "https://example.com/smarthome-demo"},
            "capabilities": {"streaming": False, "pushNotifications": True, "stateTransitionHistory": True},
            "authentication": {"schemes": ["none"]},
            "tags": ["security", "demo", "smarthome"],
            "skills": [
                {"id": "assess-event", "name": "Assess Event",
                 "description": "Scores an incoming sensor event for urgency.",
                 "examples": ["Rate this motion event at 02:14 from the front door"]},
                {"id": "draft-notification", "name": "Draft Notification",
                 "description": "Writes a human-readable alert for the homeowner.",
                 "examples": ["Draft an alert for the assessed event above"]},
            ],
        },
        {
            "name": "appliance-maintenance-agent",
            "description": "Predicts appliance maintenance windows from usage patterns.",
            "endpoint": "https://example.com/a2a/appliance-maintenance-agent",
            "version": "1.0.0",
            "provider": {"organization": "SmartHome Demo", "url": "https://example.com/smarthome-demo"},
            "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
            "authentication": {"schemes": ["none"]},
            "tags": ["maintenance", "demo", "smarthome"],
            "skills": [
                {"id": "predict-maintenance", "name": "Predict Maintenance",
                 "description": "Estimates the next maintenance date for an appliance.",
                 "examples": ["When should I service the oven?"]},
                {"id": "explain-reason", "name": "Explain Reason",
                 "description": "Explains the factors driving the prediction.",
                 "examples": ["Why does the oven need servicing?"]},
            ],
        },
    ]

    def _build_card_definition(form):
        # AgentCore Registry requires the card's protocolVersion + default IO modes.
        return _json.dumps({
            "protocolVersion": "0.3.0",
            "name": form["name"],
            "description": form["description"],
            "url": form["endpoint"],
            "version": form["version"],
            "provider": form["provider"],
            "capabilities": {k: bool(form["capabilities"].get(k))
                              for k in ("streaming", "pushNotifications", "stateTransitionHistory")},
            "authentication": form["authentication"],
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
            "skills": [
                {**s, "tags": s.get("tags", [])}
                for s in form["skills"]
            ],
            "tags": form["tags"],
        })

    # List once so we don't create duplicate placeholders on rerun. The
    # Registry API accepts same-name records (distinct recordIds), so the
    # earlier ConflictException path never fires — we have to filter by name
    # ourselves. Skip seeding whenever a real or placeholder record with that
    # name already exists (including APPROVED real ones from a2a-agent-registry/
    # deploy.py — we don't want to add a placeholder alongside them).
    existing_names: set[str] = set()
    try:
        paginator = ac_control.get_paginator("list_registry_records")
        for page in paginator.paginate(registryId=registry_id, descriptorType="A2A", maxResults=50):
            for rec in page.get("registryRecords", []):
                existing_names.add(rec.get("name", ""))
    except Exception as e:
        print(f"  [a2a-seed] list existing records failed: {e}")

    for demo in demos:
        if demo["name"] in existing_names:
            print(f"  [a2a-seed] {demo['name']} already exists — skip")
            continue
        try:
            resp = ac_control.create_registry_record(
                registryId=registry_id,
                name=demo["name"],
                description=demo["description"],
                descriptorType="A2A",
                descriptors={
                    "a2a": {
                        # Wrapper has no schemaVersion — the A2A protocolVersion
                        # lives inside the card JSON itself (build_card_definition).
                        "agentCard": {
                            "inlineContent": _build_card_definition(demo),
                        },
                    }
                },
                recordVersion="0.1.0",
                clientToken=str(_uuid.uuid4()),
            )
        except ac_control.exceptions.ConflictException:
            print(f"  [a2a-seed] {demo['name']} already exists — skip")
            continue
        except Exception as e:
            print(f"  [a2a-seed] {demo['name']}: create failed — {e}")
            continue

        record_arn = resp.get("recordArn", "")
        record_id = record_arn.split("/")[-1] if record_arn else ""
        if not record_id:
            print(f"  [a2a-seed] {demo['name']}: could not extract recordId from arn {record_arn!r}")
            continue

        # Ownership row under the deploy-time admin user's sub.
        try:
            dynamo_table.put_item(Item={
                "userId": "__erp_owner__",
                "skillName": f"a2a:{record_id}",
                "recordType": "a2a",
                "ownerSub": admin_sub or "deploy-time-admin",
                "ownerEmail": admin_email or "",
                "createdAt": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
                "updatedAt": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
            })
        except Exception as e:
            print(f"  [a2a-seed] {demo['name']}: ownership-row write failed — {e}")

        # Poll out of CREATING, then submit for approval.
        deadline = _time.time() + 10
        while _time.time() < deadline:
            try:
                s = ac_control.get_registry_record(
                    registryId=registry_id, recordId=record_id
                ).get("status")
                if s != "CREATING":
                    break
            except Exception:
                pass
            _time.sleep(0.5)

        try:
            ac_control.submit_registry_record_for_approval(
                registryId=registry_id, recordId=record_id
            )
            print(f"  [a2a-seed] {demo['name']}: created + submitted ({record_id})")
        except Exception as e:
            print(f"  [a2a-seed] {demo['name']}: submit-for-approval failed — {e}")


# ─── AgentCore Optimization (target-based A/B routing) ───────────────────────
# Provisioned at deploy time — see docs/superpowers/specs/
# 2026-05-17-agentcore-optimization-target-based-design.md.

def _runtime_short(runtime_id: str) -> str:
    """smarthome_smarthome-ee97ToCthI → smarthome_smarthome (drop the suffix)."""
    if "-" not in runtime_id:
        return runtime_id
    base, _, _ = runtime_id.rpartition("-")
    return base or runtime_id


def _ensure_runtime_endpoint(ac_control, runtime_id: str, name: str, version: str) -> str:
    """Create or look up a named runtime endpoint. Returns the endpoint ARN.
    On re-runs the existing endpoint's version pinning is preserved (admin
    may have repointed `treatment` via the CLI)."""
    try:
        resp = ac_control.create_agent_runtime_endpoint(
            agentRuntimeId=runtime_id,
            name=name,
            agentRuntimeVersion=str(version),
            description=f"AgentCore Optimization {name} variant",
        )
        return resp["agentRuntimeEndpointArn"]
    except ac_control.exceptions.ConflictException:
        existing = ac_control.get_agent_runtime_endpoint(
            agentRuntimeId=runtime_id, endpointName=name)
        return existing["agentRuntimeEndpointArn"]


def _ensure_role(iam, name: str, trust_service: str, inline_policy: dict, description: str) -> str:
    """Create-or-update an IAM role with the given trust policy and inline policy."""
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": trust_service},
            "Action": "sts:AssumeRole",
        }],
    }
    try:
        r = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                            Description=description)
        arn = r["Role"]["Arn"]
        print(f"  [opt-infra] Created IAM role {name}")
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    # Always overwrite the inline policy so drift gets corrected on every run.
    iam.put_role_policy(RoleName=name, PolicyName="Default",
                        PolicyDocument=json.dumps(inline_policy))
    return arn


def _ensure_gateway(ac_control, name: str, role_arn: str, authorizer_type: str = "AWS_IAM"):
    """Create-or-look-up a gateway by name. Returns (gateway_id, gateway_arn).
    Blocks until status leaves CREATING so subsequent CreateGatewayTarget
    calls don't ValidationException."""
    paginator = ac_control.get_paginator("list_gateways")
    found_id = None
    for page in paginator.paginate():
        for g in page.get("items", []):
            if g.get("name") == name:
                found_id = g["gatewayId"]
                break
        if found_id:
            break
    if found_id is None:
        resp = ac_control.create_gateway(
            name=name, roleArn=role_arn, authorizerType=authorizer_type,
            description="AgentCore Optimization gateway (target-based A/B routing)",
        )
        found_id = resp["gatewayId"]
        print(f"  [opt-infra] Created gateway {name} = {found_id}")
    # Poll until READY (typically <30s for fresh creates).
    for _ in range(60):
        full = ac_control.get_gateway(gatewayIdentifier=found_id)
        status = full.get("status", "")
        if status not in ("CREATING", "UPDATING"):
            return full["gatewayId"], full["gatewayArn"]
        time.sleep(2)
    full = ac_control.get_gateway(gatewayIdentifier=found_id)
    return full["gatewayId"], full["gatewayArn"]


def _ensure_gateway_target(ac_control, gateway_id: str, name: str,
                           runtime_arn: str, qualifier: str):
    """Create-or-look-up an AgentCore-runtime gateway target."""
    paginator = ac_control.get_paginator("list_gateway_targets")
    for page in paginator.paginate(gatewayIdentifier=gateway_id):
        for t in page.get("items", []):
            if t.get("name") == name:
                return t["targetId"]
    resp = ac_control.create_gateway_target(
        gatewayIdentifier=gateway_id, name=name,
        targetConfiguration={"http": {"agentcoreRuntime": {
            "arn": runtime_arn, "qualifier": qualifier,
        }}},
        # Same pattern as the tools-gateway Lambda targets: gateway uses
        # its own IAM role to invoke the runtime endpoint.
        credentialProviderConfigurations=[
            {"credentialProviderType": "GATEWAY_IAM_ROLE"},
        ],
    )
    print(f"  [opt-infra] Created gateway target {name} → {qualifier}")
    return resp["targetId"]


def _ensure_online_eval_config(ac_control, name: str, log_group: str,
                               service_name: str, role_arn: str) -> str:
    """Create-or-look-up an online-eval config. Returns its ARN."""
    paginator = ac_control.get_paginator("list_online_evaluation_configs")
    for page in paginator.paginate():
        for c in page.get("onlineEvaluationConfigs", []):
            if c.get("onlineEvaluationConfigName") == name:
                return c["onlineEvaluationConfigArn"]
    resp = ac_control.create_online_evaluation_config(
        onlineEvaluationConfigName=name,
        description=f"Per-variant online eval for AgentCore Optimization ({name})",
        dataSourceConfig={"cloudWatchLogs": {
            "logGroupNames": [log_group],
            "serviceNames": [service_name],
        }},
        evaluators=[
            {"evaluatorId": "Builtin.GoalSuccessRate"},
            {"evaluatorId": "Builtin.Helpfulness"},
        ],
        rule={
            "samplingConfig": {"samplingPercentage": 100.0},
            "sessionConfig": {"sessionTimeoutMinutes": 5},
        },
        evaluationExecutionRoleArn=role_arn,
        enableOnCreate=True,
        clientToken=str(uuid.uuid4()),
    )
    print(f"  [opt-infra] Created online-eval config {name}")
    return resp["onlineEvaluationConfigArn"]


def _lookup_optimization_infra(runtime_id: str, account: str,
                               region: str) -> dict:
    """Read-only recovery path for the optimization env vars.

    `_ensure_optimization_infra` provisions AND returns the ARNs in one shot, so
    when it raises partway through (e.g. a transient throttle on one of the two
    online-eval creates) the caller gets NOTHING back and the admin Lambda ends
    up with no `OPTIMIZATION_GATEWAY_ARN` — the console then fails every
    `POST /optimization/ab-tests` with `ConfigurationError` even though the
    gateway, targets, endpoints and eval configs are all live in the account.
    This function looks up whatever already exists so the patch block can still
    populate the env. Returns only the keys it could resolve.
    """
    ac = boto3.client("bedrock-agentcore-control", region_name=region)
    found = {}
    try:
        paginator = ac.get_paginator("list_gateways")
        for page in paginator.paginate():
            for g in page.get("items", []):
                if g.get("name") == "smarthome-optimization-gateway":
                    gw_id = g["gatewayId"]
                    found["OPTIMIZATION_GATEWAY_ID"] = gw_id
                    found["OPTIMIZATION_GATEWAY_ARN"] = (
                        f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/{gw_id}"
                    )
                    break
    except Exception as e:
        print(f"  [opt-infra] lookup: list_gateways failed: {e}")
    for ep, key in (("control", "CONTROL_ENDPOINT_ARN"),
                    ("treatment", "TREATMENT_ENDPOINT_ARN")):
        found[key] = (f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/"
                      f"{runtime_id}/runtime-endpoint/{ep}")
    try:
        paginator = ac.get_paginator("list_online_evaluation_configs")
        names = {"smarthome_control_online_eval": "CONTROL_ONLINE_EVAL_ARN",
                 "smarthome_treatment_online_eval": "TREATMENT_ONLINE_EVAL_ARN"}
        for page in paginator.paginate():
            for c in page.get("onlineEvaluationConfigs", []):
                key = names.get(c.get("onlineEvaluationConfigName"))
                if key:
                    found[key] = c["onlineEvaluationConfigArn"]
    except Exception as e:
        print(f"  [opt-infra] lookup: list_online_evaluation_configs failed: {e}")
    try:
        boto3.client("iam", region_name=region).get_role(
            RoleName="smarthome-abtest-execution-role")
        found["AB_TEST_ROLE_ARN"] = (
            f"arn:aws:iam::{account}:role/smarthome-abtest-execution-role")
    except Exception:
        pass
    return found


def _ensure_optimization_infra(runtime_id: str, runtime_arn: str,
                               account: str, region: str) -> dict:
    """Provision the dedicated optimization gateway + runtime endpoints +
    online-eval configs + supporting IAM roles. Idempotent."""
    print("Provisioning AgentCore Optimization infrastructure…")
    ac_control = boto3.client("bedrock-agentcore-control", region_name=region)
    iam = boto3.client("iam", region_name=region)

    # 1. Two named runtime endpoints, both initially pinned to the latest version.
    rt_info = ac_control.get_agent_runtime(agentRuntimeId=runtime_id)
    latest_version = rt_info.get("agentRuntimeVersion") or "1"
    control_ep_arn = _ensure_runtime_endpoint(ac_control, runtime_id, "control", latest_version)
    treatment_ep_arn = _ensure_runtime_endpoint(ac_control, runtime_id, "treatment", latest_version)

    # 2. A/B-test execution role (full agentcore + agentcore-control + logs +
    #    bedrock for evaluator LLM calls). Per the canonical sample notebook.
    ab_role_arn = _ensure_role(
        iam, name="smarthome-abtest-execution-role",
        trust_service="bedrock-agentcore.amazonaws.com",
        description="Execution role for AgentCore A/B tests",
        inline_policy={
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": [
                    "bedrock-agentcore:*", "bedrock-agentcore-control:*",
                ], "Resource": "*"},
                {"Effect": "Allow", "Action": [
                    "logs:DescribeLogGroups", "logs:DescribeIndexPolicies",
                    "logs:PutIndexPolicy", "logs:StartQuery", "logs:GetQueryResults",
                    "logs:StopQuery", "logs:FilterLogEvents", "logs:GetLogEvents",
                    "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                ], "Resource": "*"},
                {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:Retrieve"],
                 "Resource": "*"},
            ],
        },
    )

    # 3. Gateway role (forwards to runtime endpoints).
    gw_role_arn = _ensure_role(
        iam, name="smarthome-optimization-gateway-role",
        trust_service="bedrock-agentcore.amazonaws.com",
        description="IAM role for the AgentCore Optimization gateway",
        inline_policy={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                "Resource": [
                    f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}",
                    f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}/runtime-endpoint/*",
                ],
            }],
        },
    )

    # 4. Online-eval execution role.
    online_eval_role_arn = _ensure_role(
        iam, name="smarthome-optimization-online-eval-role",
        trust_service="bedrock-agentcore.amazonaws.com",
        description="Execution role for per-variant online evaluation configs",
        inline_policy={
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": [
                    "logs:DescribeLogGroups", "logs:DescribeIndexPolicies",
                    "logs:PutIndexPolicy", "logs:StartQuery", "logs:GetQueryResults",
                    "logs:StopQuery", "logs:FilterLogEvents", "logs:GetLogEvents",
                    "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                ], "Resource": "*"},
                {"Effect": "Allow", "Action": ["bedrock:InvokeModel"], "Resource": "*"},
            ],
        },
    )

    # 5. Optimization gateway.
    opt_gw_id, opt_gw_arn = _ensure_gateway(
        ac_control, name="smarthome-optimization-gateway",
        role_arn=gw_role_arn, authorizer_type="AWS_IAM",
    )

    # 6. Two AgentCore-runtime gateway targets.
    _ensure_gateway_target(ac_control, opt_gw_id, "smarthome-control",
                           runtime_arn=runtime_arn, qualifier="control")
    _ensure_gateway_target(ac_control, opt_gw_id, "smarthome-treatment",
                           runtime_arn=runtime_arn, qualifier="treatment")

    # 7. Per-endpoint online-eval configs. Each endpoint emits spans to its
    #    own log group (suffixed with the endpoint name).
    short = _runtime_short(runtime_id)
    # AgentCore name regex `[a-zA-Z][a-zA-Z0-9_]{0,47}` rejects hyphens, so
    # use underscores. Spec called these "smarthome-{control,treatment}-online-eval"
    # but the constraint forces an underscore form.
    control_eval_arn = _ensure_online_eval_config(
        ac_control, name="smarthome_control_online_eval",
        log_group=f"/aws/bedrock-agentcore/runtimes/{runtime_id}-control",
        service_name=f"{short}.control",
        role_arn=online_eval_role_arn,
    )
    treatment_eval_arn = _ensure_online_eval_config(
        ac_control, name="smarthome_treatment_online_eval",
        log_group=f"/aws/bedrock-agentcore/runtimes/{runtime_id}-treatment",
        service_name=f"{short}.treatment",
        role_arn=online_eval_role_arn,
    )

    # 8. Initialize the toggle DDB row to false (only if missing).
    ddb = boto3.resource("dynamodb", region_name=region).Table("smarthome-skills")
    toggle_sk = "__opt_routing_enabled__"
    existing = ddb.get_item(
        Key={"userId": "__global__", "skillName": toggle_sk}
    ).get("Item")
    if not existing:
        ddb.put_item(Item={
            "userId": "__global__",
            "skillName": toggle_sk,
            "value": False,
            "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "updatedBy": "setup-agentcore.py",
        })
        print(f"  [opt-infra] Initialized {toggle_sk} = false")

    return {
        "OPTIMIZATION_GATEWAY_ID": opt_gw_id,
        "OPTIMIZATION_GATEWAY_ARN": opt_gw_arn,
        "CONTROL_ENDPOINT_ARN": control_ep_arn,
        "TREATMENT_ENDPOINT_ARN": treatment_ep_arn,
        "CONTROL_ONLINE_EVAL_ARN": control_eval_arn,
        "TREATMENT_ONLINE_EVAL_ARN": treatment_eval_arn,
        "AB_TEST_ROLE_ARN": ab_role_arn,
    }


def _ensure_bundles_runtime(primary_runtime_id: str, primary_runtime_arn: str,
                            region: str) -> dict:
    """Provision the bundles runtime — same container image as the primary
    runtime, plus ENABLE_BUNDLE_HOOK=1 so the agent registers a Strands
    BeforeModelCallEvent hook that overrides system_prompt from W3C
    baggage. Idempotent: creates on first run, updates on subsequent runs.

    Returns: {"runtimeId": ..., "runtimeArn": ...}
    """
    ac = boto3.client("bedrock-agentcore-control", region_name=region)

    # Mirror everything from the primary runtime: same artifact (whether
    # container image or S3 code-zip), same role, same network/auth/
    # protocol/headers/filesystem config — so the two runtimes are
    # byte-identical at the agent business-code level. The only diverging
    # field is environmentVariables.ENABLE_BUNDLE_HOOK.
    primary = ac.get_agent_runtime(agentRuntimeId=primary_runtime_id)
    primary_artifact = primary["agentRuntimeArtifact"]
    role_arn = primary["roleArn"]
    network = primary["networkConfiguration"]
    auth = primary.get("authorizerConfiguration")
    proto = primary.get("protocolConfiguration")
    headers_cfg = primary.get("requestHeaderConfiguration")
    fs_cfg = primary.get("filesystemConfigurations")

    bundles_name = "smarthome_bundles"

    # Look up existing
    paginator = ac.get_paginator("list_agent_runtimes")
    found_id = None
    for page in paginator.paginate():
        for rt in page.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == bundles_name:
                found_id = rt["agentRuntimeId"]
                break
        if found_id:
            break

    bundles_env = dict(primary.get("environmentVariables", {}))
    bundles_env["ENABLE_BUNDLE_HOOK"] = "1"

    base_kwargs = dict(
        agentRuntimeArtifact=primary_artifact,
        networkConfiguration=network,
        roleArn=role_arn,
        environmentVariables=bundles_env,
    )
    if auth:
        base_kwargs["authorizerConfiguration"] = auth
    if proto:
        base_kwargs["protocolConfiguration"] = proto
    if headers_cfg:
        base_kwargs["requestHeaderConfiguration"] = headers_cfg
    if fs_cfg:
        base_kwargs["filesystemConfigurations"] = fs_cfg

    if found_id is None:
        resp = ac.create_agent_runtime(agentRuntimeName=bundles_name, **base_kwargs)
        rt_id = resp["agentRuntimeId"]
        rt_arn = resp["agentRuntimeArn"]
        print(f"  [bundles-runtime] Created {bundles_name} runtimeId={rt_id}")
    else:
        rt_id = found_id
        ac.update_agent_runtime(agentRuntimeId=rt_id, **base_kwargs)
        rt_arn = ac.get_agent_runtime(agentRuntimeId=rt_id)["agentRuntimeArn"]
        print(f"  [bundles-runtime] Updated {bundles_name} runtimeId={rt_id}")

    return {"runtimeId": rt_id, "runtimeArn": rt_arn}


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

    # Pre-render the voice-mode welcome clip with Polly and drop it into the
    # agent directory BEFORE we copy into the CodeZip source. Baking the MP3
    # into the container means the first WebSocket connection doesn't pay an
    # S3 GetObject round-trip — the agent can stream it out as soon as the
    # handshake completes.
    welcome_local_path = os.path.join(agent_code_src, "welcome-zh.mp3")
    try:
        print("  Rendering Polly welcome audio into agent/welcome-zh.mp3 ...")
        polly = boto3.client("polly", region_name=REGION)
        tts = polly.synthesize_speech(
            Text="欢迎使用智能家居设备助手",
            OutputFormat="mp3",
            VoiceId="Zhiyu",
            LanguageCode="cmn-CN",
            Engine="neural",
        )
        audio_bytes = tts["AudioStream"].read()
        with open(welcome_local_path, "wb") as f:
            f.write(audio_bytes)
        print(f"    Wrote {len(audio_bytes)} bytes to {welcome_local_path}")
    except Exception as e:
        print(f"    Warning: Polly render failed — {e}. Voice mode will skip the welcome clip.")

    default_app = os.path.join(project_dir, "app", "smarthome")
    if os.path.exists(default_app):
        shutil.rmtree(default_app)
    # Exclude tests/ and __pycache__/ from the CodeZip — keeps the container
    # small and avoids shipping test-only imports (pytest) to production.
    shutil.copytree(agent_code_src, default_app,
                    ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"))

    # Patch agentcore.json: set entrypoint, JWT auth, env vars
    config_file = os.path.join(project_dir, "agentcore", "agentcore.json")
    with open(config_file) as f:
        config = json.load(f)

    if config.get("runtimes"):
        rt = config["runtimes"][0]
        rt["entrypoint"] = "agent.py"
        # Runtime uses AWS_IAM (SigV4) — browser signs with Identity Pool
        # credentials. See plan.log for the CUSTOM_JWT-on-/ws regression that
        # forced this choice. The gateway still uses CUSTOM_JWT for per-user
        # Cedar policy; the chatbot passes its idToken in a custom header and
        # the agent forwards it to the gateway MCP client.
        rt.pop("authorizerType", None)
        rt.pop("authorizerConfiguration", None)
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

    # --------------------------------------------------------
    # Post-deploy: fetch IDs from AgentCore CFN stack outputs
    # --------------------------------------------------------
    # IMPORTANT: do NOT abort solely on a non-zero `agentcore deploy` return.
    # The CLI exits non-zero on a benign POST-success quirk — it validates its
    # local `agentcore/.cli/deployed-state.json` after the CloudFormation
    # deploy completes and rejects empty gatewayArn fields ("Too small:
    # expected string to have >=1 characters"). The AWS resources are fully
    # deployed and the stack is CREATE/UPDATE_COMPLETE in that case; only the
    # local state file failed schema validation. Aborting here used to skip ALL
    # the post-deploy patching below (admin Lambda env, runtime env vars, IAM
    # grants), leaving MEMORY_ID / REGISTRY_ID / AGENT_RUNTIME_ARN as
    # PLACEHOLDER_SET_BY_SETUP_SCRIPT and breaking the admin console.
    #
    # So: treat the CFN stack as the source of truth. If the stack reached a
    # *_COMPLETE state and exposes the expected outputs, continue regardless of
    # the CLI return code; only fail when the stack itself is missing/failed.
    print("\nFetching deployed resource info...")
    cf = boto3.client("cloudformation", region_name=REGION)
    ac_stack_name = "AgentCore-smarthome-default"
    try:
        ac_resp = cf.describe_stacks(StackName=ac_stack_name)
        stack_status = ac_resp["Stacks"][0].get("StackStatus", "")
    except Exception as e:
        raise Exception(
            f"agentcore deploy failed and stack {ac_stack_name} is not "
            f"queryable ({e}). Check the deploy log above."
        )
    if not stack_status.endswith("_COMPLETE"):
        raise Exception(
            f"agentcore deploy failed — stack {ac_stack_name} status="
            f"{stack_status}. Check the deploy log above."
        )
    if r.returncode != 0:
        print(
            f"  NOTE: `agentcore deploy` returned {r.returncode}, but stack "
            f"{ac_stack_name} is {stack_status}. This is the known benign "
            f"deployed-state.json validation quirk — continuing with "
            f"post-deploy patching (env vars, IAM grants)."
        )
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
    gateway_arn = (
        f"arn:aws:bedrock-agentcore:{REGION}:{account_id}:gateway/{gateway_id}"
        if gateway_id else ""
    )

    # --------------------------------------------------------
    # Post-deploy: Initialize Bedrock Knowledge Base
    # --------------------------------------------------------
    kb_id = ""
    kb_data_source_id = ""
    if kb_service_role_arn and kb_docs_bucket:
        print("\nInitializing Bedrock Knowledge Base (S3 Vectors)...")

        account_id = get_account_id()
        vector_bucket_name = f"smarthome-kb-vectors-{account_id}"
        index_name = "smarthome-kb-index"
        s3v = boto3.client("s3vectors", region_name=REGION)

        # Step A: Create the S3 Vector bucket (idempotent).
        try:
            s3v.create_vector_bucket(vectorBucketName=vector_bucket_name)
            print(f"  Created S3 vector bucket {vector_bucket_name}")
        except s3v.exceptions.ConflictException:
            print(f"  S3 vector bucket {vector_bucket_name} already exists")
        except Exception as e:
            print(f"  Warning: create_vector_bucket: {e}")

        vector_bucket_arn = f"arn:aws:s3vectors:{REGION}:{account_id}:bucket/{vector_bucket_name}"

        # Step B: Create the vector index (idempotent).
        # Cohere multilingual v3 = 1024 dims, cosine is the common choice for
        # semantic text retrieval.
        try:
            s3v.create_index(
                vectorBucketName=vector_bucket_name,
                indexName=index_name,
                dataType="float32",
                dimension=1024,
                distanceMetric="cosine",
            )
            print(f"  Created S3 vector index {index_name}")
        except s3v.exceptions.ConflictException:
            print(f"  S3 vector index {index_name} already exists")
        except Exception as e:
            print(f"  Warning: create_index: {e}")

        index_arn = f"{vector_bucket_arn}/index/{index_name}"

        # Step C: Create (or look up) the Bedrock Knowledge Base. We always
        # try to create first; if one already exists under this name we reuse
        # it, but if that KB points at AOSS (leftover from a prior deploy) we
        # delete it so the new S3 Vectors-backed KB can take its name.
        print("  Creating Bedrock Knowledge Base...")
        bedrock_agent = boto3.client("bedrock-agent", region_name=REGION)
        embedding_model_arn = f"arn:aws:bedrock:{REGION}::foundation-model/cohere.embed-multilingual-v3"

        def _create_kb():
            return bedrock_agent.create_knowledge_base(
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
                    "type": "S3_VECTORS",
                    "s3VectorsConfiguration": {
                        # Pass indexArn; indexName is mutually exclusive with it.
                        "indexArn": index_arn,
                    },
                },
            )

        def _wait_active_and_create_ds(new_kb_id):
            """Wait for KB ACTIVE, create the S3 data source, store DDB config."""
            for _ in range(30):
                kb = bedrock_agent.get_knowledge_base(knowledgeBaseId=new_kb_id)
                if kb["knowledgeBase"]["status"] == "ACTIVE":
                    break
                time.sleep(2)
            print("  Creating S3 data source...")
            ds_resp = bedrock_agent.create_data_source(
                knowledgeBaseId=new_kb_id,
                name="SmartHomeKBDocuments",
                dataSourceConfiguration={
                    "type": "S3",
                    "s3Configuration": {
                        "bucketArn": f"arn:aws:s3:::{kb_docs_bucket}",
                    },
                },
            )
            ds_id = ds_resp["dataSource"]["dataSourceId"]
            print(f"  Data source created: {ds_id}")
            skills_table = outputs.get("SkillsTableName", "smarthome-skills")
            ddb = boto3.resource("dynamodb", region_name=REGION)
            ddb_table = ddb.Table(skills_table)
            from datetime import datetime, timezone
            ddb_table.put_item(Item={
                "userId": "__kb_config__",
                "skillName": "__default__",
                "knowledgeBaseId": new_kb_id,
                "dataSourceId": ds_id,
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            })
            print("  KB config stored in DynamoDB")
            return ds_id

        try:
            kb_resp = _create_kb()
            kb_id = kb_resp["knowledgeBase"]["knowledgeBaseId"]
            print(f"  Knowledge Base created: {kb_id}")
            kb_data_source_id = _wait_active_and_create_ds(kb_id)

        except bedrock_agent.exceptions.ConflictException:
            print("  Knowledge Base already exists, looking up existing...")
            list_resp = bedrock_agent.list_knowledge_bases(maxResults=100)
            existing_id = ""
            for kb_summary in list_resp.get("knowledgeBaseSummaries", []):
                if kb_summary.get("name") == "SmartHomeEnterpriseKB":
                    existing_id = kb_summary["knowledgeBaseId"]
                    break
            if existing_id:
                detail = bedrock_agent.get_knowledge_base(knowledgeBaseId=existing_id)
                storage_type = (detail.get("knowledgeBase", {}).get("storageConfiguration") or {}).get("type", "")
                if storage_type == "S3_VECTORS":
                    kb_id = existing_id
                    ds_list = bedrock_agent.list_data_sources(knowledgeBaseId=kb_id, maxResults=10)
                    for ds in ds_list.get("dataSourceSummaries", []):
                        kb_data_source_id = ds["dataSourceId"]
                        break
                    print(f"  Reusing existing S3_VECTORS KB: {kb_id}, DS: {kb_data_source_id}")
                else:
                    # KB still points at old storage (e.g. AOSS). Delete and recreate.
                    #
                    # Gotcha: if the CDK stack already tore down the AOSS
                    # collection, the KB's delete will fail with "Unable to
                    # delete data from vector store" because Bedrock tries to
                    # purge vectors that no longer exist. The fix is to flip
                    # the data source's dataDeletionPolicy to RETAIN before
                    # deleting so Bedrock skips the vector purge.
                    print(f"  Existing KB {existing_id} uses {storage_type!r}; deleting so S3_VECTORS KB can take its name...")
                    try:
                        ds_list = bedrock_agent.list_data_sources(knowledgeBaseId=existing_id, maxResults=10)
                        for ds in ds_list.get("dataSourceSummaries", []):
                            ds_detail = bedrock_agent.get_data_source(
                                knowledgeBaseId=existing_id, dataSourceId=ds["dataSourceId"]
                            )
                            try:
                                bedrock_agent.update_data_source(
                                    knowledgeBaseId=existing_id,
                                    dataSourceId=ds["dataSourceId"],
                                    name=ds_detail["dataSource"]["name"],
                                    dataSourceConfiguration=ds_detail["dataSource"]["dataSourceConfiguration"],
                                    dataDeletionPolicy="RETAIN",
                                )
                            except Exception as e:
                                print(f"    update_data_source RETAIN failed (non-fatal): {e}")
                            bedrock_agent.delete_data_source(
                                knowledgeBaseId=existing_id, dataSourceId=ds["dataSourceId"]
                            )
                        bedrock_agent.delete_knowledge_base(knowledgeBaseId=existing_id)
                        # Wait for delete (up to 60s). Bedrock delete is async.
                        for _ in range(30):
                            try:
                                bedrock_agent.get_knowledge_base(knowledgeBaseId=existing_id)
                                time.sleep(2)
                            except bedrock_agent.exceptions.ResourceNotFoundException:
                                break
                        # Retry create — the name is freed once delete completes.
                        kb_resp = _create_kb()
                        kb_id = kb_resp["knowledgeBase"]["knowledgeBaseId"]
                        print(f"  Knowledge Base re-created on S3_VECTORS: {kb_id}")
                        kb_data_source_id = _wait_active_and_create_ds(kb_id)
                    except Exception as e:
                        print(f"  Warning: could not migrate existing KB: {e}")
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
        print("\nSkipping KB initialization (missing KB infrastructure outputs)")

    # Welcome audio is baked directly into the CodeZip (see step 2); no S3
    # upload is needed, and no runtime GetObject round-trip at connect time.

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
        # Dedicated table for per-login runtime sessions (text + voice). The
        # agent appends one row per login here so the admin Sessions tab can
        # render the full history instead of only the latest session.
        runtime_sessions_table = outputs.get(
            "RuntimeSessionsTableName", "smarthome-runtime-sessions"
        )
        existing_env["RUNTIME_SESSIONS_TABLE_NAME"] = runtime_sessions_table
        # Code Interpreter session table — execute_python writes per-run code +
        # streamed output + chart paths here for the chatbot's CodeInterpreter tab.
        existing_env["CODE_SESSIONS_TABLE_NAME"] = "smarthome-code-sessions"
        # Voice-mode env vars: Nova Sonic model + gateway ARN (welcome clip is
        # baked into the CodeZip, so no S3 path env var is needed).
        existing_env["NOVA_SONIC_MODEL_ID"] = "amazon.nova-2-sonic-v1:0"
        # Strands built-in tools default to requiring interactive consent
        # before file_write / http_request / shell side-effects. In a hosted
        # runtime there is no user at a prompt to confirm, so we bypass the
        # consent dialog. Risk is bounded because tool access is gated by the
        # agent's wrapper and the skill definitions in DynamoDB.
        existing_env["BYPASS_TOOL_CONSENT"] = "true"
        # NOTE: do not set OTEL_SEMCONV_STABILITY_OPT_IN here. Strands' legacy
        # gen_ai span-event convention is what AgentCore Online Evaluation's
        # StrandsEventParser expects; opting into `gen_ai_latest_experimental`
        # rewrites the event names and breaks evaluation.
        if gateway_arn:
            existing_env["AGENTCORE_GATEWAY_ARN"] = gateway_arn
        # Runtime auth mode: AWS_IAM (SigV4). The CUSTOM_JWT path on the
        # runtime's /ws endpoint is broken upstream — the edge rejects WebSocket
        # upgrades with HTTP 424. SigV4 works, so the browser signs with
        # temporary credentials from the Cognito Identity Pool (authenticated
        # role). Per-user Cedar gateway policies still work because the chatbot
        # passes the idToken in a custom allowlisted header
        # (X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken), which the
        # agent forwards as Bearer to the gateway MCP client.
        update_kwargs = dict(
            agentRuntimeId=runtime_id,
            agentRuntimeArtifact=rt_info["agentRuntimeArtifact"],
            roleArn=rt_info["roleArn"],
            networkConfiguration=rt_info["networkConfiguration"],
            environmentVariables=existing_env,
            # Explicit HTTP protocol — required for the runtime's edge to route
            # WebSocket upgrades on /ws through to the container. Without this,
            # upgrade requests are rejected at the runtime proxy with a 424.
            protocolConfiguration={"serverProtocol": "HTTP"},
            # Under AWS_IAM auth the `Authorization` header can't be allowlisted
            # (API validation rejects it). The chatbot instead passes the user's
            # Cognito idToken in a custom `X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken`
            # header/query-param; the agent forwards it as Bearer to the gateway MCP client.
            requestHeaderConfiguration={
                "requestHeaderAllowlist": [
                    "X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken",
                ],
            },
            # Per-session persistent filesystem at /mnt/workspace — the text
            # agent's vision path (agent/session_storage.py) saves raw
            # uploaded images there so follow-up turns and future tools can
            # recover originals without a second upload.
            filesystemConfigurations=[{
                "sessionStorage": {"mountPath": "/mnt/workspace"},
            }],
        )
        ac.update_agent_runtime(**update_kwargs)
        print(f"  Patched MODEL_ID, AWS_REGION, SKILLS_TABLE_NAME={skills_table}, RUNTIME_SESSIONS_TABLE_NAME={runtime_sessions_table}")
        print(f"  Runtime set to AWS_IAM auth (SigV4) for /invocations + /ws")
        print(f"  Session storage enabled at /mnt/workspace")

        # Stop all known user runtime sessions so the fresh CodeZip takes
        # effect immediately. Without this, existing sessions keep serving
        # stale agent.py / voice_session.py code until they idle-timeout
        # (observed up to several minutes of drift after a redeploy).
        # Runtime sessions live in a dedicated table (one row per login, not
        # overwritten) with kind=text|voice + sessionId. Agent writes via
        # agent.py:_record_session; voice writes via voice_session._record_voice_session.
        try:
            dataplane = boto3.client("bedrock-agentcore", region_name=REGION)
            ddb = boto3.resource("dynamodb", region_name=REGION)
            table = ddb.Table(runtime_sessions_table)
            # `kind` is a DynamoDB reserved word — alias it.
            session_items = table.scan(
                FilterExpression="#k = :k",
                ExpressionAttributeNames={"#k": "kind"},
                ExpressionAttributeValues={":k": "text"},
                ProjectionExpression="sessionId",
            ).get("Items", [])
            stopped = 0
            for item in session_items:
                sid = item.get("sessionId")
                if not sid:
                    continue
                try:
                    dataplane.stop_runtime_session(
                        agentRuntimeArn=runtime_arn,
                        runtimeSessionId=sid,
                    )
                    stopped += 1
                except dataplane.exceptions.ResourceNotFoundException:
                    pass  # already terminated / idle-expired
                except Exception as e:
                    print(f"    Warning: could not stop session {sid}: {e}")
            print(f"  Stopped {stopped} active runtime session(s) so the fresh CodeZip takes effect")
        except Exception as e:
            print(f"  Warning: session invalidation skipped: {e}")

        # Grant runtime role DynamoDB read access for skills table
        role_arn = rt_info.get("roleArn", "")
        if role_arn:
            role_name = role_arn.split("/")[-1]
            iam_client = boto3.client("iam", region_name=REGION)
            policy_statements = [
                {
                    "Effect": "Allow",
                    "Action": ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan", "dynamodb:PutItem", "dynamodb:UpdateItem"],
                    "Resource": [
                        f"arn:aws:dynamodb:{REGION}:{account_id}:table/{skills_table}",
                        f"arn:aws:dynamodb:{REGION}:{account_id}:table/smarthome-browser-sessions",
                        f"arn:aws:dynamodb:{REGION}:{account_id}:table/smarthome-code-sessions",
                        f"arn:aws:dynamodb:{REGION}:{account_id}:table/{runtime_sessions_table}",
                    ],
                },
                {
                    "Effect": "Allow",
                    "Action": ["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"],
                    "Resource": "*",
                },
                {
                    # Nova Sonic bi-directional streaming for the /ws voice session.
                    "Effect": "Allow",
                    "Action": [
                        "bedrock:InvokeModelWithBidirectionalStream",
                        "bedrock:InvokeModelWithResponseStream",
                        "bedrock:InvokeModel",
                    ],
                    "Resource": "*",
                },
                {
                    # AgentCore Browser Tool — browse_web starts, drives, and
                    # stops browser sessions. The APIs live on the data-plane
                    # (bedrock-agentcore), not the control-plane. We grant the
                    # full bedrock-agentcore:* because the browser-automation
                    # CDP WebSocket upgrade is authorised by AWS via IAM but
                    # does not surface as a single action name — restricting
                    # to explicit actions caused the upgrade to be rejected
                    # with HTTP 403. Scope is bounded to the service.
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:*",
                    ],
                    "Resource": "*",
                },
                {
                    # AgentCore Optimization (preview): the runtime reads bundle
                    # versions on-demand when an A/B test injects a baggage
                    # reference into the request. Read-only; scoped to bundles.
                    "Effect": "Allow",
                    "Action": ["bedrock-agentcore-control:GetConfigurationBundleVersion"],
                    "Resource": [
                        f"arn:aws:bedrock-agentcore:{REGION}:{account_id}:configuration-bundle/*"
                    ],
                },
            ]
            # (No S3 permission needed — welcome clip is bundled into CodeZip.)
            policy_doc = json.dumps({
                "Version": "2012-10-17",
                "Statement": policy_statements,
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

    # --------------------------------------------------------
    # Voice runtime: separate AgentCore Runtime for /ws (Nova Sonic BidiAgent)
    # --------------------------------------------------------
    # Rationale: keeping voice on its own runtime lets the login-time warmup
    # predictably heat the strands.bidi import chain and lets voice and text
    # scale/redeploy independently. See
    # docs/superpowers/specs/2026-04-23-voice-agent-split-design.md.
    voice_runtime_id = ""
    voice_runtime_arn = ""
    # Ensure shared state is defined even if text runtime_id was falsy.
    if "ac" not in dir():
        ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
    if "existing_env" not in dir():
        existing_env = {}
    if "skills_table" not in dir():
        skills_table = outputs.get("SkillsTableName", "smarthome-skills")
    if "runtime_sessions_table" not in dir():
        runtime_sessions_table = outputs.get(
            "RuntimeSessionsTableName", "smarthome-runtime-sessions"
        )
    try:
        print("\n" + "=" * 60)
        print("  Creating voice runtime (smarthomevoice)...")
        print("=" * 60)

        voice_project_parent = os.path.join(AGENTCORE_DIR, "voice-workdir")
        if os.path.exists(voice_project_parent):
            shutil.rmtree(voice_project_parent)
        os.makedirs(voice_project_parent)

        r = run("agentcore create --name smarthomevoice --defaults", cwd=voice_project_parent)
        if r.returncode != 0:
            raise Exception("agentcore create smarthomevoice failed")
        voice_project_dir = os.path.join(voice_project_parent, "smarthomevoice")

        # Replace default agent code with our agent/ directory — same source as
        # text runtime but with voice_agent.py as entrypoint (see single-package
        # two-entrypoint layout).
        voice_default_app = os.path.join(voice_project_dir, "app", "smarthomevoice")
        if os.path.exists(voice_default_app):
            shutil.rmtree(voice_default_app)
        shutil.copytree(agent_code_src, voice_default_app,
                        ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"))

        # Patch agentcore.json: entrypoint, env vars, AWS_IAM auth (/ws needs it)
        voice_config_file = os.path.join(voice_project_dir, "agentcore", "agentcore.json")
        with open(voice_config_file) as vf:
            voice_config = json.load(vf)
        if voice_config.get("runtimes"):
            vrt = voice_config["runtimes"][0]
            vrt["entrypoint"] = "voice_agent.py"
            vrt.pop("authorizerType", None)
            vrt.pop("authorizerConfiguration", None)
            vrt["environmentVariables"] = {
                "AWS_REGION": REGION,
                "DISABLE_ADOT": "1",
            }
        with open(voice_config_file, "w") as vf:
            json.dump(voice_config, vf, indent=2)

        voice_targets_file = os.path.join(voice_project_dir, "agentcore", "aws-targets.json")
        with open(voice_targets_file, "w") as vf:
            json.dump([{"name": "default", "region": REGION, "account": account_id}], vf, indent=2)

        print("\n  Deploying smarthomevoice runtime...")
        r = run("agentcore deploy -y --verbose", cwd=voice_project_dir)

        # Same benign-non-zero handling as the text runtime (see the long note
        # at the text `agentcore deploy` above): the CLI can exit non-zero on a
        # post-success deployed-state.json validation quirk while the CFN stack
        # is fully *_COMPLETE. Trust the stack, not the return code, so the
        # voice env/IAM patching below still runs.
        voice_ac_stack = "AgentCore-smarthomevoice-default"
        voice_ac_resp = cf.describe_stacks(StackName=voice_ac_stack)
        voice_stack_status = voice_ac_resp["Stacks"][0].get("StackStatus", "")
        if not voice_stack_status.endswith("_COMPLETE"):
            raise Exception(
                f"agentcore deploy smarthomevoice failed — stack "
                f"{voice_ac_stack} status={voice_stack_status}"
            )
        if r.returncode != 0:
            print(
                f"  NOTE: voice `agentcore deploy` returned {r.returncode}, but "
                f"stack {voice_ac_stack} is {voice_stack_status} — known benign "
                f"deployed-state.json quirk, continuing."
            )

        # Fetch voice runtime IDs from its CFN stack
        voice_ac_outputs = {o["OutputKey"]: o["OutputValue"]
                            for o in voice_ac_resp["Stacks"][0].get("Outputs", [])}
        for key, val in voice_ac_outputs.items():
            if "RuntimeIdOutput" in key:
                voice_runtime_id = val
            elif "RuntimeArnOutput" in key:
                voice_runtime_arn = val

        # Patch voice runtime env vars + auth + IAM, same pattern as text runtime
        if voice_runtime_id:
            print(f"  Patching voice runtime environment (id={voice_runtime_id})...")
            v_rt_info = ac.get_agent_runtime(agentRuntimeId=voice_runtime_id)
            voice_env = v_rt_info.get("environmentVariables", {})
            voice_env["AWS_REGION"] = REGION
            voice_env["DISABLE_ADOT"] = "1"
            voice_env["SKILLS_TABLE_NAME"] = skills_table
            voice_env["RUNTIME_SESSIONS_TABLE_NAME"] = runtime_sessions_table
            voice_env["NOVA_SONIC_MODEL_ID"] = "amazon.nova-2-sonic-v1:0"
            if gateway_arn:
                voice_env["AGENTCORE_GATEWAY_ARN"] = gateway_arn
            # Propagate AGENTCORE_GATEWAY_*_URL from text runtime env (added by
            # the agentcore CLI automatically during text deploy).
            for k, v in existing_env.items():
                if k.startswith("AGENTCORE_GATEWAY_") and k.endswith("_URL"):
                    voice_env[k] = v

            ac.update_agent_runtime(
                agentRuntimeId=voice_runtime_id,
                agentRuntimeArtifact=v_rt_info["agentRuntimeArtifact"],
                roleArn=v_rt_info["roleArn"],
                networkConfiguration=v_rt_info["networkConfiguration"],
                environmentVariables=voice_env,
                protocolConfiguration={"serverProtocol": "HTTP"},
                requestHeaderConfiguration={
                    "requestHeaderAllowlist": [
                        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken",
                    ],
                },
            )
            print("  Voice runtime set to AWS_IAM + HTTP protocol + custom auth header allowlist")

            # Stop any active voice sessions so the fresh CodeZip takes effect.
            # Voice sessions are tracked in the runtime-sessions table with kind='voice'.
            try:
                v_dataplane = boto3.client("bedrock-agentcore", region_name=REGION)
                v_table = boto3.resource("dynamodb", region_name=REGION).Table(runtime_sessions_table)
                v_sessions = v_table.scan(
                    FilterExpression="#k = :k",
                    ExpressionAttributeNames={"#k": "kind"},
                    ExpressionAttributeValues={":k": "voice"},
                    ProjectionExpression="sessionId",
                ).get("Items", [])
                v_stopped = 0
                for item in v_sessions:
                    sid = item.get("sessionId")
                    if not sid:
                        continue
                    try:
                        v_dataplane.stop_runtime_session(
                            agentRuntimeArn=voice_runtime_arn,
                            runtimeSessionId=sid,
                        )
                        v_stopped += 1
                    except v_dataplane.exceptions.ResourceNotFoundException:
                        pass
                    except Exception as e:
                        print(f"    Warning: could not stop voice session {sid}: {e}")
                print(f"  Stopped {v_stopped} active voice runtime session(s)")
            except Exception as e:
                print(f"  Warning: voice session invalidation skipped: {e}")

            # Voice runtime role: DynamoDB read + Nova Sonic bidi invoke
            v_role_arn = v_rt_info.get("roleArn", "")
            if v_role_arn:
                v_role_name = v_role_arn.split("/")[-1]
                v_policy_doc = json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan", "dynamodb:PutItem", "dynamodb:UpdateItem"],
                            "Resource": [
                                f"arn:aws:dynamodb:{REGION}:{account_id}:table/{skills_table}",
                                f"arn:aws:dynamodb:{REGION}:{account_id}:table/{runtime_sessions_table}",
                            ],
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "bedrock:InvokeModelWithBidirectionalStream",
                                "bedrock:InvokeModelWithResponseStream",
                                "bedrock:InvokeModel",
                            ],
                            "Resource": "*",
                        },
                        {
                            # ListFoundationModels is called by voice_agent.py's
                            # __warmup__ handler to preheat the Bedrock client
                            # (endpoint resolution + creds + TLS). Cheap and
                            # read-only; scoped to the control plane.
                            "Effect": "Allow",
                            "Action": ["bedrock:ListFoundationModels"],
                            "Resource": "*",
                        },
                        {
                            # AgentCore Optimization (preview): voice runtime
                            # also resolves bundle prompts when an A/B test
                            # injects a baggage reference. Same scope as text.
                            "Effect": "Allow",
                            "Action": ["bedrock-agentcore-control:GetConfigurationBundleVersion"],
                            "Resource": [
                                f"arn:aws:bedrock-agentcore:{REGION}:{account_id}:configuration-bundle/*"
                            ],
                        },
                    ],
                })
                try:
                    iam_client = boto3.client("iam", region_name=REGION)
                    iam_client.put_role_policy(
                        RoleName=v_role_name,
                        PolicyName="VoiceRuntimeAccess",
                        PolicyDocument=v_policy_doc,
                    )
                    print(f"  Granted DynamoDB + Bedrock bidi to voice role {v_role_name}")
                except Exception as e:
                    print(f"  Warning: Failed to attach voice runtime policy: {e}")

        print(f"  Voice runtime ready — ID={voice_runtime_id} ARN={voice_runtime_arn}")
    except Exception as e:
        print(f"  Warning: voice runtime setup failed: {e}")
        import traceback as _tb
        _tb.print_exc()

    # Grant the Cognito authenticated role permission to invoke both runtimes.
    # Browser calls /invocations (SigV4) on text ARN and /ws (SigV4 presigned URL)
    # on voice ARN using credentials from the Cognito Identity Pool authenticated role.
    cognito_auth_role_arn = outputs.get("CognitoAuthRoleArn", "")
    if runtime_arn and cognito_auth_role_arn:
        cognito_auth_role_name = cognito_auth_role_arn.split("/")[-1]
        invoke_resources = [runtime_arn, f"{runtime_arn}/*"]
        if voice_runtime_arn:
            invoke_resources.extend([voice_runtime_arn, f"{voice_runtime_arn}/*"])
        try:
            iam_client = boto3.client("iam", region_name=REGION)
            iam_client.put_role_policy(
                RoleName=cognito_auth_role_name,
                PolicyName="AgentCoreRuntimeInvoke",
                PolicyDocument=json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "bedrock-agentcore:InvokeAgentRuntime",
                                "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
                                "bedrock-agentcore:InvokeAgentRuntimeCommand",
                            ],
                            # Wildcard covers endpoint qualifiers (e.g. /DEFAULT).
                            "Resource": invoke_resources,
                        },
                        {
                            # UpdateBrowserStream is how the chatbot's "Take
                            # control / Release control" toggle flips the
                            # automation stream ENABLED↔DISABLED for a live
                            # browser session. Called directly from the
                            # browser, same pattern as InvokeAgentRuntimeCommand.
                            "Effect": "Allow",
                            "Action": [
                                "bedrock-agentcore:UpdateBrowserStream",
                            ],
                            "Resource": "*",
                        },
                    ],
                }),
            )
            print(f"  Granted runtime-invoke on {len(invoke_resources) // 2} runtime(s) to Cognito auth role {cognito_auth_role_name}")
        except Exception as e:
            print(f"  Warning: Failed to grant runtime-invoke to auth role: {e}")

    # AgentCore Optimization (target-based A/B routing): provision the
    # dedicated optimization gateway + 2 endpoints + 2 online-eval configs
    # + supporting IAM roles, before patching the admin Lambda's env so all
    # the new ARNs land in one update.
    opt_infra = {}
    bundles_info = {}
    if runtime_arn and runtime_id:
        try:
            opt_infra = _ensure_optimization_infra(
                runtime_id=runtime_id, runtime_arn=runtime_arn,
                account=account_id, region=REGION,
            )
            print(f"  [opt-infra] Optimization gateway: {opt_infra['OPTIMIZATION_GATEWAY_ID']}")

            # Extend Cognito auth-role IAM grant so chatbot can SigV4-invoke
            # the optimization gateway. AgentCore Gateway's runtime
            # invocation maps to the dedicated `InvokeGateway` action
            # (separate from `InvokeAgentRuntime`).
            if cognito_auth_role_arn:
                try:
                    iam_client = boto3.client("iam", region_name=REGION)
                    opt_gw_arn = opt_infra["OPTIMIZATION_GATEWAY_ARN"]
                    iam_client.put_role_policy(
                        RoleName=cognito_auth_role_arn.split("/")[-1],
                        PolicyName="AgentCoreOptimizationGatewayInvoke",
                        PolicyDocument=json.dumps({
                            "Version": "2012-10-17",
                            "Statement": [{
                                "Effect": "Allow",
                                "Action": [
                                    "bedrock-agentcore:InvokeGateway",
                                    "bedrock-agentcore:InvokeAgentRuntime",
                                ],
                                "Resource": [opt_gw_arn, f"{opt_gw_arn}/*"],
                            }],
                        }),
                    )
                    print(f"  [opt-infra] Granted optimization-gateway invoke to Cognito auth role")
                except Exception as e:
                    print(f"  [opt-infra] Warning: failed to grant gateway invoke to auth role: {e}")
        except Exception as e:
            print(f"  [opt-infra] Warning: provisioning failed: {e}")
            # Fall back to looking up whatever already exists. A partial
            # provisioning failure must not leave the admin Lambda with no
            # OPTIMIZATION_GATEWAY_ARN — that breaks "Start A/B test" with a
            # ConfigurationError even when every resource is live.
            opt_infra = _lookup_optimization_infra(runtime_id, account_id, REGION)
            if opt_infra.get("OPTIMIZATION_GATEWAY_ARN"):
                print("  [opt-infra] Recovered existing ARNs by lookup: "
                      f"{sorted(opt_infra)}")
            else:
                print("  [opt-infra] Admin Console Optimization tab will show "
                      "ConfigurationError until this resolves.")

        # Bundles runtime (§8.13). Same image as primary, ENABLE_BUNDLE_HOOK=1
        # so it registers a BeforeModelCallEvent hook overriding system_prompt
        # from W3C baggage. Used when a tenant is in 'ab-bundles' mode.
        try:
            bundles_info = _ensure_bundles_runtime(
                primary_runtime_id=runtime_id,
                primary_runtime_arn=runtime_arn,
                region=REGION,
            )
            print(f"  [bundles-runtime] ARN: {bundles_info['runtimeArn']}")
        except Exception as e:
            print(f"  [bundles-runtime] Warning: provisioning failed: {e}")
            bundles_info = {"runtimeArn": ""}

        # Extend Cognito auth-role IAM grant to cover the bundles runtime.
        if cognito_auth_role_arn and bundles_info.get("runtimeArn"):
            try:
                iam_client = boto3.client("iam", region_name=REGION)
                bundles_arn = bundles_info["runtimeArn"]
                iam_client.put_role_policy(
                    RoleName=cognito_auth_role_arn.split("/")[-1],
                    PolicyName="AgentCoreBundlesRuntimeInvoke",
                    PolicyDocument=json.dumps({
                        "Version": "2012-10-17",
                        "Statement": [{
                            "Effect": "Allow",
                            "Action": [
                                "bedrock-agentcore:InvokeAgentRuntime",
                                "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
                                "bedrock-agentcore:InvokeAgentRuntimeCommand",
                            ],
                            "Resource": [bundles_arn, f"{bundles_arn}/*"],
                        }],
                    }),
                )
                print(f"  [bundles-runtime] Granted invoke to Cognito auth role")
            except Exception as e:
                print(f"  [bundles-runtime] Warning: IAM grant failed: {e}")

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
            # Start from the Lambda's CURRENT env and merge our patched values
            # on top, rather than replacing wholesale. update_function_configuration
            # replaces the entire Variables map, so building a fresh dict would
            # silently drop any CDK-provided var this block doesn't enumerate
            # (e.g. CODE_SESSIONS_TABLE_NAME, future additions). Merging keeps
            # CDK's vars and only overrides the post-deploy-resolved ones.
            try:
                current_admin_env = lambda_client.get_function_configuration(
                    FunctionName="smarthome-admin-api"
                ).get("Environment", {}).get("Variables", {})
            except Exception:
                current_admin_env = {}
            admin_env = dict(current_admin_env)
            admin_env.update({
                "SKILLS_TABLE_NAME": outputs.get("SkillsTableName", "smarthome-skills"),
                "AGENT_RUNTIME_ARN": runtime_arn,
                "VOICE_AGENT_RUNTIME_ARN": voice_runtime_arn,
                "AWS_REGION_OVERRIDE": REGION,
                "COGNITO_USER_POOL_ID": outputs.get("UserPoolId", ""),
                "GATEWAY_ID": gateway_id,
                "MEMORY_ID": memory_id,
                "BROWSER_SESSIONS_TABLE_NAME": "smarthome-browser-sessions",
                "RUNTIME_SESSIONS_TABLE_NAME": outputs.get(
                    "RuntimeSessionsTableName", "smarthome-runtime-sessions"
                ),
            })
            # Merge optimization infra ARNs (empty dict on failure → admin
            # Lambda will return 500 ConfigurationError on /optimization/*).
            admin_env.update(opt_infra)
            # Don't let a silently-empty merge read as a successful deploy:
            # these four are exactly what optimization.py:start_ab_test needs.
            missing_opt = [k for k in ("OPTIMIZATION_GATEWAY_ARN",
                                       "CONTROL_ONLINE_EVAL_ARN",
                                       "TREATMENT_ONLINE_EVAL_ARN",
                                       "AB_TEST_ROLE_ARN")
                           if not admin_env.get(k)]
            if missing_opt:
                print(f"  WARNING: admin Lambda is missing {missing_opt} — the "
                      f"Admin Console's Evaluation > Optimization > Start A/B "
                      f"test will fail with ConfigurationError. Re-run this "
                      f"script once the optimization infra provisions.")
            # Preserve SKILL_FILES_BUCKET from CDK stack
            skill_files_bucket = outputs.get("SkillFilesBucketName", "")
            if skill_files_bucket:
                admin_env["SKILL_FILES_BUCKET"] = skill_files_bucket
            # KB-related env vars
            if kb_docs_bucket:
                admin_env["KB_DOCS_BUCKET"] = kb_docs_bucket
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

    # --------------------------------------------------------
    # Create AgentCore Registry for Skill ERP + admin Import feature
    # --------------------------------------------------------
    registry_id = ""
    try:
        print("\nCreating AgentCore Registry for skill records...")
        ac_control = boto3.client("bedrock-agentcore-control", region_name=REGION)
        # Fail loud if the local boto3 doesn't know about the Registry API —
        # otherwise registry creation would silently no-op and the Skill ERP
        # Lambda would keep REGISTRY_ID="PLACEHOLDER_SET_BY_SETUP_SCRIPT".
        if not hasattr(ac_control, "create_registry"):
            raise RuntimeError(
                "boto3 is too old — missing bedrock-agentcore-control.create_registry. "
                f"Current version: {boto3.__version__}. "
                "Run scripts/01-install-deps.sh (which upgrades boto3 in the venv) "
                "or `pip install --upgrade boto3` and retry."
            )
        registry_name = "SmartHomeSkillsRegistry"

        def _find_existing_registry():
            """Return (id, arn) of an existing registry with our name, or (None, None)."""
            token = None
            while True:
                kwargs = {}
                if token:
                    kwargs["nextToken"] = token
                lst = ac_control.list_registries(**kwargs)
                for reg in lst.get("registries", []):
                    if reg.get("name") == registry_name:
                        arn = reg.get("registryArn", "")
                        return arn.split("/")[-1] if arn else None, arn
                token = lst.get("nextToken")
                if not token:
                    return None, None

        try:
            reg_resp = ac_control.create_registry(
                name=registry_name,
                description="Registry for skills published from the Skill ERP site",
                authorizerType="AWS_IAM",
                approvalConfiguration={"autoApproval": False},
            )
            registry_arn = reg_resp.get("registryArn", "")
            registry_id = registry_arn.split("/")[-1] if registry_arn else ""
            print(f"  Created registry {registry_name} — id={registry_id}")
        except ac_control.exceptions.ConflictException:
            # Already exists — look it up
            registry_id, registry_arn = _find_existing_registry()
            if registry_id:
                print(f"  Found existing registry {registry_name} — id={registry_id}")
            else:
                print(f"  Warning: create_registry said Conflict but list_registries did not return {registry_name}")
        except Exception as e:
            # ServiceQuotaExceeded or any other create error — if one named
            # {registry_name} already exists (e.g. because the account hit its
            # per-account registry quota and a prior deploy created it), use
            # that instead of failing.
            existing_id, existing_arn = _find_existing_registry()
            if existing_id:
                registry_id = existing_id
                registry_arn = existing_arn
                print(f"  create_registry failed ({type(e).__name__}); reusing existing registry {registry_name} — id={registry_id}")
            else:
                print(f"  Warning: create_registry failed: {e}")

        # Wait for ACTIVE
        if registry_id:
            for _ in range(15):
                try:
                    reg_info = ac_control.get_registry(registryId=registry_id)
                    if reg_info.get("status") == "ACTIVE":
                        break
                except Exception:
                    pass
                time.sleep(2)

        # Patch admin + skill-erp Lambdas with REGISTRY_ID env
        if registry_id:
            lambda_client = boto3.client("lambda", region_name=REGION)
            for fn_name in ("smarthome-admin-api", "smarthome-skill-erp-api"):
                try:
                    resp_cfg = lambda_client.get_function_configuration(FunctionName=fn_name)
                    env = resp_cfg.get("Environment", {}).get("Variables", {})
                    env["REGISTRY_ID"] = registry_id
                    lambda_client.update_function_configuration(
                        FunctionName=fn_name,
                        Environment={"Variables": env},
                    )
                    print(f"  Patched {fn_name} with REGISTRY_ID={registry_id}")
                except Exception as e:
                    print(f"  Warning: could not patch {fn_name} with REGISTRY_ID: {e}")

        # Seed 3 demo A2A agent records so the Integration Registry tab has
        # content to show after admins approve them in the Registry console.
        try:
            admin_sub_for_seed = ""
            admin_email_for_seed = ""
            try:
                cog = boto3.client("cognito-idp", region_name=REGION)
                # Username can be the email (default in this stack) or "admin";
                # try the email form first, then fall back.
                u = None
                for candidate in ("admin@smarthome.local", "admin"):
                    try:
                        u = cog.admin_get_user(
                            UserPoolId=outputs["UserPoolId"], Username=candidate
                        )
                        break
                    except cog.exceptions.UserNotFoundException:
                        continue
                if u is not None:
                    admin_sub_for_seed = next(
                        (a["Value"] for a in u["UserAttributes"] if a["Name"] == "sub"),
                        "",
                    )
                    admin_email_for_seed = next(
                        (a["Value"] for a in u["UserAttributes"] if a["Name"] == "email"),
                        "admin@smarthome.local",
                    )
                else:
                    admin_email_for_seed = "admin@smarthome.local"
                    print("  [a2a-seed] admin Cognito user not found; ownership rows will use default email")
            except Exception as e:
                print(f"  [a2a-seed] could not read admin user attributes: {e}")

            dynamo_res = boto3.resource("dynamodb", region_name=REGION)
            skills_table = dynamo_res.Table("smarthome-skills")
            _seed_demo_a2a_records(
                ac_control, registry_id, admin_sub_for_seed, admin_email_for_seed, skills_table
            )
            print("  NOTE: approve the 3 A2A records in the AgentCore Registry console to see them in Admin → Integration Registry → A2A Agents.")
        except Exception as e:
            print(f"  [a2a-seed] unexpected failure (non-fatal): {e}")
    except Exception as e:
        print(f"  Warning: Registry setup skipped: {e}")

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
  cognitoIdentityPoolId: "{outputs['IdentityPoolId']}",
  cognitoUserPoolId: "{outputs['UserPoolId']}",
  cognitoClientId: "{outputs['UserPoolClientId']}"
}};"""
        print("Updating device simulator config.js...")
        s3.put_object(Bucket=ds_bucket, Key="config.js",
                      Body=ds_config, ContentType="application/javascript")
        _invalidate(outputs.get("DeviceSimDistributionId", ""))

    if runtime_arn:
        # Chatbot text path now goes through the optimization gateway
        # (target-based A/B routing). Voice path stays direct on the
        # voice runtime — the gateway doesn't proxy WebSocket.
        opt_gw_id = opt_infra.get("OPTIMIZATION_GATEWAY_ID", "")
        opt_gw_url = (
            f"https://{opt_gw_id}.gateway.bedrock-agentcore.{REGION}.amazonaws.com"
            if opt_gw_id else ""
        )
        chatbot_config = f"""window.__CONFIG__ = {{
  cognitoUserPoolId: "{outputs['UserPoolId']}",
  cognitoClientId: "{outputs['UserPoolClientId']}",
  cognitoDomain: "{outputs['CognitoDomain']}",
  cognitoIdentityPoolId: "{outputs['IdentityPoolId']}",
  agentRuntimeArn: "{runtime_arn}",
  bundlesRuntimeArn: "{bundles_info.get('runtimeArn', '')}",
  voiceAgentRuntimeArn: "{voice_runtime_arn}",
  optimizationGatewayUrl: "{opt_gw_url}",
  optimizationDefaultTarget: "smarthome-control",
  adminApiUrl: "{outputs.get('AdminApiUrl', '')}",
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
  cognitoIdentityPoolId: "{outputs['IdentityPoolId']}",
  adminApiUrl: "{admin_api_url}",
  agentRuntimeArn: "{runtime_arn}",
  voiceAgentRuntimeArn: "{voice_runtime_arn}",
  region: "{REGION}",
  chatbotUrl: "{outputs.get('ChatbotUrl', '')}",
  deviceSimulatorUrl: "{outputs.get('DeviceSimulatorUrl', '')}",
  skillErpUrl: "{outputs.get('SkillErpUrl', '')}"
}};"""
        print("Updating admin console config.js...")
        s3.put_object(Bucket=admin_bucket, Key="config.js",
                      Body=admin_config, ContentType="application/javascript")
        _invalidate(outputs.get("AdminConsoleDistributionId", ""))

    # Skill ERP config.js
    skill_erp_bucket = outputs.get("SkillErpBucketName", "")
    skill_erp_api_url = outputs.get("SkillErpApiUrl", "")
    if skill_erp_bucket and skill_erp_api_url:
        skill_erp_config = f"""window.__CONFIG__ = {{
  cognitoUserPoolId: "{outputs['UserPoolId']}",
  cognitoClientId: "{outputs['UserPoolClientId']}",
  erpApiUrl: "{skill_erp_api_url}",
  region: "{REGION}"
}};"""
        print("Updating skill-erp config.js...")
        s3.put_object(Bucket=skill_erp_bucket, Key="config.js",
                      Body=skill_erp_config, ContentType="application/javascript")
        _invalidate(outputs.get("SkillErpDistributionId", ""))

    # Save state for teardown
    state_file = os.path.join(PROJECT_ROOT, "agentcore-state.json")
    with open(state_file, "w") as f:
        json.dump({
            "gatewayId": gateway_id, "runtimeId": runtime_id,
            "runtimeArn": runtime_arn, "projectDir": project_dir,
            "voiceRuntimeId": voice_runtime_id,
            "voiceRuntimeArn": voice_runtime_arn,
            "knowledgeBaseId": kb_id, "dataSourceId": kb_data_source_id,
            "registryId": registry_id,
        }, f, indent=2)

    print("\n" + "=" * 60)
    print("  AgentCore Setup Complete!")
    print("=" * 60)
    print(f"\n  Gateway ID:    {gateway_id}")
    print(f"  Gateway URL:   {gateway_url}")
    print(f"  Runtime ID:    {runtime_id}")
    print(f"  Runtime ARN:   {runtime_arn}")
    if voice_runtime_id:
        print(f"  Voice Runtime ID:  {voice_runtime_id}")
        print(f"  Voice Runtime ARN: {voice_runtime_arn}")
    print(f"\n  Device Sim:    {outputs.get('DeviceSimulatorUrl', '')}")
    print(f"  Chatbot:       {outputs.get('ChatbotUrl', '')}")
    print(f"  Admin Console: {outputs.get('AdminConsoleUrl', '')}")
    print(f"  Skill ERP:     {outputs.get('SkillErpUrl', '')}")
    print(f"  Admin API:     {outputs.get('AdminApiUrl', '')}")
    print(f"  Skill ERP API: {outputs.get('SkillErpApiUrl', '')}")
    if registry_id:
        print(f"  Registry ID:   {registry_id}")
    if outputs.get("AdminUsername"):
        print(f"\n  Admin Login:   {outputs['AdminUsername']} / {outputs.get('AdminPassword', '')}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
