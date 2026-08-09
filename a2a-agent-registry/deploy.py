#!/usr/bin/env python3
"""Deploy the three sample A2A agents.

Driven by the ``agentcore`` CLI (no local Docker daemon — CodeBuild runs the
image build server-side). Idempotent; re-running only touches what changed.

Steps:
  1. LOAD — read cdk-outputs.json, agentcore-state.json, deployed-state.json
  2. COGNITO (global) — idempotent resource server + m2m app client + Secret
  3. RENDER (per-agent) — materialize ``.agentcore-project/<name>/``
  4. AGENTCORE DEPLOY (per-agent) — agentcore create + deploy -y
  5. WORKLOAD IDENTITY (per-agent) — aud claim for downstream JWT check
  6. REGISTRY (per-agent) — create/update + submit-for-approval
  7. PERSIST (global merge) — rewrite deployed-state.json keeping other agents
  8. PATCH TEXT AGENT (global) — add A2A_* envs + secret read permission

CLI:
  python deploy.py                           # all agents, all steps
  python deploy.py --agent energy-optimization
  python deploy.py --agent a,b
  python deploy.py --only cognito,registry   # step filter (in/out)
  python deploy.py --skip deploy             # skip a specific step
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import boto3

# ------------------------------------------------------------------
# Constants — keep in sync with README and setup-agentcore.py
# ------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
CDK_OUTPUTS = PROJECT_ROOT / "cdk-outputs.json"
AGENTCORE_STATE = PROJECT_ROOT / "agentcore-state.json"
DEPLOYED_STATE = HERE / "deployed-state.json"
AC_PROJECT_DIR = HERE / ".agentcore-project"

# The roster and the Cognito identifiers live in common/agents.py — one
# definition, imported by deploy / teardown / demo_reset / smoke_test. They used
# to be copy-pasted into all four, which meant a roster edit that missed a copy
# failed at a different stage depending on which script ran.
sys.path.insert(0, str(HERE))
from common.agents import (  # noqa: E402
    AGENT_LONG_NAMES,
    AGENT_NAMES,
    AGENT_SHORT_SLUG,
    ALLOWED_SKILLS_HEADER,
    M2M_CLIENT_NAME,
    RESOURCE_SERVER_ID,
    SCOPE_FULL,
    SCOPE_NAME,
    REGISTRY_CLIENT,
    SECRET_NAME,
    USER_TOKEN_HEADER,
)

ALL_STEPS = ("cognito", "render", "deploy", "workload", "registry", "persist", "patch-text-agent")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: str, cwd: str | Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    log(f"  $ {cmd}" + (f"   (cwd={cwd})" if cwd else ""))
    r = subprocess.run(cmd, shell=True, cwd=str(cwd) if cwd else None)
    if check and r.returncode != 0:
        raise RuntimeError(f"command failed: {cmd}")
    return r


def load_state() -> dict[str, Any]:
    if not CDK_OUTPUTS.exists():
        raise SystemExit(f"{CDK_OUTPUTS} not found — run ./deploy.sh first.")
    if not AGENTCORE_STATE.exists():
        raise SystemExit(f"{AGENTCORE_STATE} not found — run ./deploy.sh first.")

    cdk = json.loads(CDK_OUTPUTS.read_text())
    stack_name = next(iter(cdk))
    cdk_out = cdk[stack_name]
    agentcore_out = json.loads(AGENTCORE_STATE.read_text())

    region = agentcore_out.get("region") or os.environ.get("AWS_REGION") or "us-east-1"
    # Try to infer region from runtime ARN if not present
    arn = agentcore_out.get("runtimeArn", "")
    if arn:
        parts = arn.split(":")
        if len(parts) >= 4:
            region = parts[3]

    deployed = {"agents": [], "cognito": {}}
    if DEPLOYED_STATE.exists():
        try:
            deployed = json.loads(DEPLOYED_STATE.read_text())
        except Exception:
            log(f"  warn: could not parse {DEPLOYED_STATE}; starting fresh")

    return {
        "region": region,
        "user_pool_id": cdk_out["UserPoolId"],
        # The chatbot's app client — the `aud` of the user idTokens that get
        # forwarded on an A2A hop. Distinct from the m2m client id in
        # `deployed.cognito.clientId`, which is the `client_id` of the service
        # token in the Authorization header. Verifying a user token against the
        # m2m client would reject every real user.
        "user_pool_client_id": cdk_out.get("UserPoolClientId", ""),
        "cognito_domain": cdk_out["CognitoDomain"],
        "registry_id": agentcore_out.get("registryId", ""),
        "text_agent_runtime_id": agentcore_out.get("runtimeId", ""),
        "text_agent_runtime_arn": agentcore_out.get("runtimeArn", ""),
        "deployed": deployed,
    }


def save_deployed(deployed: dict[str, Any]) -> None:
    DEPLOYED_STATE.write_text(json.dumps(deployed, indent=2) + "\n")
    log(f"  wrote {DEPLOYED_STATE}")


def parse_agent_list(raw: list[str]) -> list[str]:
    """Flatten --agent a,b,c (possibly multiple times) into a clean list."""
    if not raw:
        return list(AGENT_NAMES)
    out: list[str] = []
    for item in raw:
        out.extend(x.strip() for x in item.split(",") if x.strip())
    unknown = [a for a in out if a not in AGENT_NAMES]
    if unknown:
        raise SystemExit(f"Unknown --agent value(s): {unknown}. Valid: {list(AGENT_NAMES)}")
    return out


def parse_steps(only: list[str] | None, skip: list[str] | None) -> set[str]:
    flat_only = [] if not only else [x.strip() for raw in only for x in raw.split(",") if x.strip()]
    flat_skip = [] if not skip else [x.strip() for raw in skip for x in raw.split(",") if x.strip()]
    if flat_only:
        steps = set(flat_only)
    else:
        steps = set(ALL_STEPS)
    for s in flat_skip:
        steps.discard(s)
    unknown = steps - set(ALL_STEPS)
    if unknown:
        raise SystemExit(f"Unknown step(s): {unknown}. Valid: {list(ALL_STEPS)}")
    return steps


# ------------------------------------------------------------------
# Step 2: Cognito OAuth2 (global, idempotent)
# ------------------------------------------------------------------

def ensure_cognito(state: dict[str, Any]) -> dict[str, Any]:
    region = state["region"]
    pool_id = state["user_pool_id"]

    cognito = boto3.client("cognito-idp", region_name=region)
    secrets = boto3.client("secretsmanager", region_name=region)

    # Resource server
    try:
        cognito.describe_resource_server(UserPoolId=pool_id, Identifier=RESOURCE_SERVER_ID)
        log(f"  resource server '{RESOURCE_SERVER_ID}' already exists")
    except cognito.exceptions.ResourceNotFoundException:
        cognito.create_resource_server(
            UserPoolId=pool_id,
            Identifier=RESOURCE_SERVER_ID,
            Name="A2A Server",
            Scopes=[{"ScopeName": SCOPE_NAME, "ScopeDescription": "Invoke A2A downstream agents"}],
        )
        log(f"  created resource server '{RESOURCE_SERVER_ID}'")

    # App client (look up by name — no native ByName API)
    client_id = None
    paginator = cognito.get_paginator("list_user_pool_clients")
    for page in paginator.paginate(UserPoolId=pool_id, MaxResults=60):
        for c in page["UserPoolClients"]:
            if c["ClientName"] == M2M_CLIENT_NAME:
                client_id = c["ClientId"]
                break
        if client_id:
            break

    if not client_id:
        resp = cognito.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName=M2M_CLIENT_NAME,
            GenerateSecret=True,
            AllowedOAuthFlows=["client_credentials"],
            AllowedOAuthScopes=[SCOPE_FULL],
            AllowedOAuthFlowsUserPoolClient=True,
            ExplicitAuthFlows=[],
            SupportedIdentityProviders=["COGNITO"],
            EnableTokenRevocation=True,
        )
        client_id = resp["UserPoolClient"]["ClientId"]
        log(f"  created m2m app client '{M2M_CLIENT_NAME}' ({client_id})")
    else:
        log(f"  m2m app client '{M2M_CLIENT_NAME}' already exists ({client_id})")

    desc = cognito.describe_user_pool_client(UserPoolId=pool_id, ClientId=client_id)["UserPoolClient"]
    client_secret = desc["ClientSecret"]

    # Secrets Manager
    secret_payload = json.dumps({"client_id": client_id, "client_secret": client_secret})
    try:
        sec = secrets.describe_secret(SecretId=SECRET_NAME)
        secrets.put_secret_value(SecretId=SECRET_NAME, SecretString=secret_payload)
        secret_arn = sec["ARN"]
        log(f"  updated Secret {SECRET_NAME}")
    except secrets.exceptions.ResourceNotFoundException:
        sec = secrets.create_secret(
            Name=SECRET_NAME,
            Description="Cognito m2m client_id+secret for A2A agents",
            SecretString=secret_payload,
        )
        secret_arn = sec["ARN"]
        log(f"  created Secret {SECRET_NAME}")

    token_url = f"https://{state['cognito_domain']}/oauth2/token"

    state["deployed"]["cognito"] = {
        "clientId": client_id,
        "resourceServer": RESOURCE_SERVER_ID,
        "scope": SCOPE_FULL,
        "tokenUrl": token_url,
        "m2mSecretArn": secret_arn,
    }
    return state


# ------------------------------------------------------------------
# Step 3: Render per-agent agentcore project
# ------------------------------------------------------------------

def _text_agent_gateway_env(state: dict[str, Any]) -> dict[str, str]:
    """Read the tools-Gateway env vars off the main text runtime.

    `agentcore deploy` names them AGENTCORE_GATEWAY_<GATEWAYNAME>_URL, so the key
    is not knowable without looking. Copying whatever the main runtime uses keeps
    the sub-agent pointed at the same Gateway by construction.
    """
    runtime_id = state.get("text_agent_runtime_id", "")
    if not runtime_id:
        return {}
    try:
        ac = boto3.client("bedrock-agentcore-control", region_name=state["region"])
        env = ac.get_agent_runtime(
            agentRuntimeId=runtime_id).get("environmentVariables") or {}
    except Exception as exc:  # noqa: BLE001
        log(f"  warn: could not read the main runtime's env: {exc}")
        return {}
    return {
        k: v for k, v in env.items()
        if k.startswith("AGENTCORE_GATEWAY_") and (
            k.endswith("_URL") or k.endswith("_AUTH_TYPE") or k == "AGENTCORE_GATEWAY_ARN")
    }


def _module_name(agent: str) -> str:
    """Importable package name for an agent directory.

    Directory names carry hyphens (`device-control`), which are not valid in an
    import path, so a tools-bearing agent's directory has to be copied under an
    underscored name for `from device_control.tools import build_tools` to work.
    """
    return agent.replace("-", "_")


def render_agent_project(agent: str, state: dict[str, Any]) -> Path:
    """Materialize one agentcore-CLI project for a sample agent.

    The CLI lays out ``<slug>/app/<slug>/`` as the code root. We wipe its
    stub, copy ``common/`` and ``<agent>/`` flat into that dir (so the generated
    ``main.py`` can ``from common.server import ...``), then seed
    ``aws-targets.json`` for non-interactive deploy.
    """
    slug = AGENT_SHORT_SLUG[agent]
    AC_PROJECT_DIR.mkdir(exist_ok=True)
    project_dir = AC_PROJECT_DIR / slug

    # Clean previous artifacts for a deterministic render.
    if project_dir.exists():
        shutil.rmtree(project_dir)

    # agentcore CLI 0.26.0 stopped accepting a bare `--defaults` for a
    # non-interactive create: it now wants the framework / model-provider /
    # memory choices spelled out ("Use --no-agent for project-only, or provide
    # all: --framework, --model-provider, --memory"). Pass them explicitly rather
    # than relying on a default set that has already changed once. The stub the
    # CLI generates is deleted below regardless — only the project scaffolding
    # (agentcore/ dir, CDK app) is kept.
    log(f"  [{agent}] agentcore create --name {slug} --protocol A2A ...")
    run(
        f"agentcore create --name {slug} --protocol A2A --defaults "
        f"--framework Strands --model-provider Bedrock --memory none "
        f"--build CodeZip --language Python",
        cwd=AC_PROJECT_DIR,
    )

    code_root = project_dir / "app" / slug
    # Clear stub sources (keep pyproject.toml / README — we rewrite them below)
    for child in code_root.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    # Copy common/ + <agent>/ into the code root.
    src_root = HERE
    shutil.copytree(src_root / "common", code_root / "common",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"))
    shutil.copytree(src_root / agent, code_root / agent,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # A tools-bearing agent also needs an importable copy: `device-control` is a
    # fine directory name but not a module name, and main.py has to be able to
    # `from device_control.tools import build_tools`. The hyphenated directory
    # stays because the card and prompt are loaded by path, not by import.
    if (src_root / agent / "tools.py").exists() and _module_name(agent) != agent:
        shutil.copytree(src_root / agent, code_root / _module_name(agent),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (code_root / _module_name(agent) / "__init__.py").touch()

    # main.py at code-root — agentcore CLI expects entrypoint at top level.
    #
    # An agent that needs tools ships a `tools.py` next to its card exporting
    # `build_tools(caller)`. It is passed as a FACTORY, not a list: the tools carry
    # the calling user's identity, so they have to be rebuilt per request (see
    # common/server.py). Agents without that file get the prompt-only path, byte
    # for byte what they had before tools existed.
    has_tools = (src_root / agent / "tools.py").exists()
    if has_tools:
        (code_root / "main.py").write_text(
            "# Auto-generated entrypoint — delegates to common.server.run_agent()\n"
            "from common.server import run_agent\n"
            f"from {_module_name(agent)}.tools import build_tools\n"
            f"run_agent(\n"
            f"    system_prompt_path=\"{agent}/system_prompt.md\",\n"
            f"    card_json_path=\"{agent}/card.json\",\n"
            f"    tools_factory=build_tools,\n"
            f")\n"
        )
    else:
        (code_root / "main.py").write_text(
            "# Auto-generated entrypoint — delegates to common.server.run_agent()\n"
            "from common.server import run_agent\n"
            f"run_agent(\n"
            f"    system_prompt_path=\"{agent}/system_prompt.md\",\n"
            f"    card_json_path=\"{agent}/card.json\",\n"
            f")\n"
        )
    log(f"  [{agent}] tools: {'per-request factory' if has_tools else 'none'}")

    # pyproject.toml — hatchling builds the wheel the CodeBuild stage runs.
    (code_root / "pyproject.toml").write_text(
        "[build-system]\n"
        "requires = [\"hatchling\"]\n"
        "build-backend = \"hatchling.build\"\n\n"
        "[project]\n"
        f"name = \"{slug}\"\n"
        "version = \"0.1.0\"\n"
        "description = \"SmartHome A2A sample agent\"\n"
        "readme = \"README.md\"\n"
        "requires-python = \">=3.10\"\n"
        "dependencies = [\n"
        "    \"aws-opentelemetry-distro\",\n"
        "    \"bedrock-agentcore >= 1.6.0\",\n"
        "    \"strands-agents[a2a] >= 1.13.0\",\n"
        "    \"fastapi >= 0.110\",\n"
        "    \"uvicorn[standard] >= 0.27\",\n"
        "    \"httpx >= 0.28\",\n"
        "    \"botocore[crt] >= 1.35.0\",\n"
        # Verifying the forwarded user idToken needs a JWT library, and calling
        # the Gateway as that user needs an MCP client. Both were missing, which
        # is why common/jwt_verify.py could never have run in the deployed
        # container: `from jose import jwt` would have ImportError'd at startup.
        # A dependency list that omits what the code imports is how that module
        # sat here looking functional without ever executing.
        "    \"python-jose[cryptography] >= 3.3.0\",\n"
        "    \"boto3 >= 1.42.93\",\n"
        "    \"mcp >= 1.9.0\",\n"
        "]\n\n"
        "[tool.hatch.build.targets.wheel]\n"
        "packages = [\".\"]\n"
    )
    (code_root / "README.md").write_text(f"# {slug}\nSmartHome A2A sample agent: {agent}\n")

    # aws-targets.json required for non-interactive agentcore deploy.
    account_id = boto3.client("sts").get_caller_identity()["Account"]
    targets_file = project_dir / "agentcore" / "aws-targets.json"
    targets_file.write_text(json.dumps(
        [{"name": "default", "region": state["region"], "account": account_id}],
        indent=2,
    ))

    log(f"  [{agent}] rendered → {code_root}")
    return project_dir


def patch_agentcore_json(agent: str, project_dir: Path, state: dict[str, Any]) -> None:
    cfg_file = project_dir / "agentcore" / "agentcore.json"
    cfg = json.loads(cfg_file.read_text())
    cognito = state["deployed"]["cognito"]

    if cfg.get("runtimes"):
        rt = cfg["runtimes"][0]
        rt["entrypoint"] = "main.py"
        rt["protocol"] = "A2A"
        rt.pop("authorizerType", None)
        rt.pop("authorizerConfiguration", None)
        rt["environmentVariables"] = {
            "AWS_REGION": state["region"],
            "COGNITO_REGION": state["region"],
            "COGNITO_USER_POOL_ID": state["user_pool_id"],
            "EXPECTED_SCOPE": SCOPE_FULL,
            "A2A_TOKEN_URL": cognito["tokenUrl"],
            "EXPECTED_CLIENT_ID": cognito["clientId"],
            # Audience for the forwarded USER idToken — the chatbot's app client,
            # not the m2m client above. Without it the audience check is skipped.
            "COGNITO_APP_CLIENT_ID": state.get("user_pool_client_id", ""),
        }
    cfg_file.write_text(json.dumps(cfg, indent=2))


# ------------------------------------------------------------------
# Step 4: agentcore deploy
# ------------------------------------------------------------------

def agentcore_deploy(agent: str, project_dir: Path, state: dict[str, Any]) -> dict[str, str]:
    """Run ``agentcore deploy -y``, then patch env + CUSTOM_JWT auth on the
    resulting Runtime (the agentcore CLI drops custom env vars and does not
    support CUSTOM_JWT for A2A use cases directly)."""
    log(f"  [{agent}] agentcore deploy -y ...")
    run("agentcore deploy -y --verbose", cwd=project_dir)

    cfn_stack = f"AgentCore-{AGENT_SHORT_SLUG[agent]}-default"
    cf = boto3.client("cloudformation", region_name=state["region"])
    resp = cf.describe_stacks(StackName=cfn_stack)
    outputs = {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}
    runtime_id = runtime_arn = ""
    for k, v in outputs.items():
        if "RuntimeIdOutput" in k:
            runtime_id = v
        elif "RuntimeArnOutput" in k:
            runtime_arn = v
    if not runtime_arn:
        raise RuntimeError(f"could not find RuntimeArnOutput in stack {cfn_stack}")
    region = runtime_arn.split(":")[3]
    invocation_url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        + runtime_arn.replace(":", "%3A").replace("/", "%2F")
        + "/invocations"
    )
    log(f"  [{agent}] runtime = {runtime_arn}")

    # Post-deploy patch: env vars + CUSTOM_JWT auth. The agentcore CLI drops
    # custom env values and does not expose JWT config knobs for A2A, so we
    # re-apply them directly against the control plane (same pattern as
    # scripts/setup-agentcore.py for the main smarthome runtime).
    cognito = state["deployed"]["cognito"]
    ac = boto3.client("bedrock-agentcore-control", region_name=region)
    rt_info = ac.get_agent_runtime(agentRuntimeId=runtime_id)
    discovery_url = (
        f"https://cognito-idp.{state['region']}.amazonaws.com/"
        f"{state['user_pool_id']}/.well-known/openid-configuration"
    )
    env = rt_info.get("environmentVariables") or {}
    # Per-agent default model from card.json (Claude Haiku for home-security, Nova Lite others)
    card_json_path = HERE / agent / "card.json"
    try:
        card_dict = json.loads(card_json_path.read_text())
        default_model = card_dict.get("defaultModelId")
    except Exception:
        default_model = None
    env.update({
        "AWS_REGION": state["region"],
        "COGNITO_REGION": state["region"],
        "COGNITO_USER_POOL_ID": state["user_pool_id"],
        "EXPECTED_SCOPE": SCOPE_FULL,
        "A2A_TOKEN_URL": cognito["tokenUrl"],
        "EXPECTED_CLIENT_ID": cognito["clientId"],
        # Audience for the forwarded USER idToken (chatbot app client). The
        # agentcore CLI drops custom env on deploy, so this has to be re-applied
        # here as well as in agentcore.json.
        "COGNITO_APP_CLIENT_ID": state.get("user_pool_client_id", ""),
        "BYPASS_TOOL_CONSENT": "true",
    })
    if not state.get("user_pool_client_id"):
        log(f"  [{agent}] WARNING: no UserPoolClientId in cdk-outputs.json — the "
            f"forwarded user token's audience will NOT be checked")

    # A tool-bearing agent calls the same tools Gateway the orchestrator does, so
    # it needs the URL. Read it off the main runtime rather than reconstructing it
    # — the env var's name embeds the gateway's name, and the URL format is the
    # platform's to choose.
    #
    # No IAM grant accompanies this: the Gateway is CUSTOM_JWT, so the credential
    # is the end user's forwarded token, not this runtime's role. That is the
    # point — the runtime holds no device permissions of its own, so every command
    # is evaluated against the real user by Cedar.
    if (HERE / agent / "tools.py").exists():
        gateway_env = _text_agent_gateway_env(state)
        if gateway_env:
            env.update(gateway_env)
            log(f"  [{agent}] gateway env: {sorted(gateway_env)}")
        else:
            log(f"  [{agent}] WARNING: could not find AGENTCORE_GATEWAY_*_URL on "
                f"the main runtime — this agent will have no device tools")
    if default_model:
        env["MODEL_ID"] = default_model
    update_kwargs = dict(
        agentRuntimeId=runtime_id,
        agentRuntimeArtifact=rt_info["agentRuntimeArtifact"],
        roleArn=rt_info["roleArn"],
        networkConfiguration=rt_info.get("networkConfiguration", {"networkMode": "PUBLIC"}),
        environmentVariables=env,
        protocolConfiguration={"serverProtocol": "A2A"},
        authorizerConfiguration={
            "customJWTAuthorizer": {
                "discoveryUrl": discovery_url,
                "allowedClients": [cognito["clientId"]],
            }
        },
        # Without this the Runtime edge DROPS both custom headers before the
        # container sees them, and it does it silently: the request arrives
        # looking like one that simply chose not to send them. Measured — with no
        # allowlist the server's skill check refused a request whose client had
        # definitely sent X-A2A-Allowed-Skills. The main smarthome runtime needs
        # the same treatment for its own auth-token header, so the mechanism is
        # not new, just never applied to the A2A agents (which had nothing to pass
        # through until now).
        requestHeaderConfiguration={
            "requestHeaderAllowlist": [ALLOWED_SKILLS_HEADER, USER_TOKEN_HEADER],
        },
    )
    ac.update_agent_runtime(**update_kwargs)
    log(f"  [{agent}] patched env + CUSTOM_JWT auth (discovery={discovery_url})")
    log(f"  [{agent}] header allowlist: {ALLOWED_SKILLS_HEADER}, {USER_TOKEN_HEADER}")

    return {
        "cfnStack": cfn_stack,
        "runtimeId": runtime_id,
        "runtimeArn": runtime_arn,
        "invocationUrl": invocation_url,
    }


# ------------------------------------------------------------------
# Step 5: Workload identity (optional — JWT middleware uses client_id check)
# ------------------------------------------------------------------

def ensure_workload_identity(agent: str, state: dict[str, Any]) -> str | None:
    """Create a workload identity named smarthome-a2a-<agent> if absent.

    Returns the workload identity ARN, or None if the AgentCore control-plane
    doesn't support it in this region (non-fatal).
    """
    client = boto3.client("bedrock-agentcore-control", region_name=state["region"])
    name = f"{AGENT_SHORT_SLUG[agent]}"
    try:
        client.get_workload_identity(name=name)
        log(f"  [{agent}] workload identity '{name}' exists")
    except client.exceptions.ResourceNotFoundException:
        resp = client.create_workload_identity(name=name)
        log(f"  [{agent}] created workload identity '{name}'")
        return resp.get("workloadIdentityArn") or resp.get("arn")
    except Exception as e:
        log(f"  [{agent}] workload identity skipped ({e})")
        return None

    # Lookup existing ARN
    try:
        resp = client.get_workload_identity(name=name)
        return resp.get("workloadIdentityArn") or resp.get("arn")
    except Exception:
        return None


# ------------------------------------------------------------------
# Step 6: Registry create / update
# ------------------------------------------------------------------

def ensure_registry_record(
    agent: str,
    state: dict[str, Any],
    invocation_url: str,
    existing_record_id: str | None,
) -> str:
    """Create or update the A2A record in AgentCore Registry and submit for approval."""
    from common.card import load_card_json, render_card_for_registry  # type: ignore

    card_json_path = HERE / agent / "card.json"
    card_dict = load_card_json(str(card_json_path))

    registry_id = state["registry_id"]
    if not registry_id:
        raise RuntimeError("registryId empty in agentcore-state.json; run setup-agentcore first")

    card_for_registry = render_card_for_registry(
        card_dict,
        runtime_url=invocation_url,
        token_url=state["deployed"]["cognito"]["tokenUrl"],
        scope=SCOPE_FULL,
    )
    # GA: descriptors.a2aAgentCard.data — one level shallower than preview's
    # descriptors.a2a.agentCard.inlineContent. The service validates the card
    # against the full A2A schema, so render_card_for_registry's complete output is
    # what makes this pass; a hand-trimmed card is rejected as "does not match any
    # supported version".
    descriptor_payload = {
        "a2aAgentCard": {"data": json.dumps(card_for_registry)},
    }

    # AWS Agent Registry (GA namespace) — Registry calls only.
    registry_control = boto3.client(REGISTRY_CLIENT, region_name=state["region"])

    record_id = existing_record_id
    if record_id:
        try:
            registry_control.get_registry_record(registryId=registry_id, recordId=record_id)
        except Exception:
            log(f"  [{agent}] prior recordId {record_id} missing — creating fresh")
            record_id = None

    if record_id:
        try:
            registry_control.update_registry_record(
                registryId=registry_id,
                recordId=record_id,
                descriptors=descriptor_payload,
            )
            log(f"  [{agent}] registry record updated ({record_id})")
        except Exception as e:
            log(f"  [{agent}] update failed, will recreate — {e}")
            try:
                registry_control.delete_registry_record(registryId=registry_id, recordId=record_id)
            except Exception:
                pass
            record_id = None

    # First-time deploy: setup-agentcore.py may have seeded a same-name
    # placeholder record pointing at example.com. Find it, delete it, so that
    # the Registry ends up with exactly one record per agent name (the real
    # one we're about to create).
    if not record_id:
        try:
            paginator = registry_control.get_paginator("list_registry_records")
            for page in paginator.paginate(
                registryId=registry_id,
                filters=[{"name": "recordType", "values": ["AGENT"]}],
                maxResults=50,
            ):
                for rec in page.get("registryRecords", []):
                    if rec.get("name") != AGENT_LONG_NAMES[agent]:
                        continue
                    stale_rid = rec.get("recordId", "")
                    if not stale_rid:
                        continue
                    try:
                        registry_control.delete_registry_record(
                            registryId=registry_id, recordId=stale_rid
                        )
                        log(f"  [{agent}] deleted stale placeholder {stale_rid}")
                    except Exception as e:
                        log(f"  [{agent}] could not delete stale {stale_rid}: {e}")
        except Exception as e:
            log(f"  [{agent}] placeholder scan failed (non-fatal): {e}")

    if not record_id:
        try:
            # GA: recordType replaces descriptorType; `name` is the dedup key and
            # `displayName` carries the label preview kept in `name`.
            resp = registry_control.create_registry_record(
                registryId=registry_id,
                name=AGENT_LONG_NAMES[agent],
                displayName=AGENT_LONG_NAMES[agent],
                description=card_dict.get("description", ""),
                recordType="AGENT",
                descriptors=descriptor_payload,
                recordVersion="0.1.0",
                clientToken=str(uuid.uuid4()),
            )
        except registry_control.exceptions.ConflictException:
            # Find existing by listing
            log(f"  [{agent}] name conflict; searching existing records")
            paginator = registry_control.get_paginator("list_registry_records")
            for page in paginator.paginate(registryId=registry_id):
                for rec in page.get("registryRecords", []):
                    name_matches = AGENT_LONG_NAMES[agent] in (
                        rec.get("name"), rec.get("displayName"))
                    if name_matches and rec.get("recordType") == "AGENT":
                        record_id = rec["recordId"]
                        registry_control.update_registry_record(
                            registryId=registry_id,
                            recordId=record_id,
                            descriptors=descriptor_payload,
                        )
                        log(f"  [{agent}] updated existing record ({record_id})")
                        break
                if record_id:
                    break
            if not record_id:
                raise
        else:
            # GA returns recordArn, NOT recordId. Reading `recordId` here would
            # store None and the record would look created but unfindable.
            arn = resp.get("recordArn", "")
            record_id = arn.split("/")[-1] if arn else ""
            log(f"  [{agent}] created record {record_id}")

    # Wait out CREATING state, then submit for approval
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            st = registry_control.get_registry_record(registryId=registry_id, recordId=record_id).get("status", "")
            if st != "CREATING":
                break
        except Exception:
            pass
        time.sleep(0.5)
    try:
        registry_control.submit_registry_record_for_approval(registryId=registry_id, recordId=record_id)
        log(f"  [{agent}] submitted for approval (PENDING_APPROVAL)")
    except Exception as e:
        log(f"  [{agent}] submit_for_approval: {e} (likely already submitted/approved)")

    return record_id


# ------------------------------------------------------------------
# Step 8: Patch text agent Runtime env + secret IAM
# ------------------------------------------------------------------

def patch_text_agent(state: dict[str, Any]) -> None:
    if not state["text_agent_runtime_id"]:
        log("  text agent runtime not found; skipping env patch")
        return
    cognito = state["deployed"]["cognito"]
    region = state["region"]
    ac = boto3.client("bedrock-agentcore-control", region_name=region)
    runtime_id = state["text_agent_runtime_id"]
    rt = ac.get_agent_runtime(agentRuntimeId=runtime_id)
    env = rt.get("environmentVariables", {}) or {}
    env.update({
        "A2A_M2M_SECRET_ARN": cognito["m2mSecretArn"],
        "A2A_COGNITO_TOKEN_URL": cognito["tokenUrl"],
        "A2A_COGNITO_SCOPE": cognito["scope"],
        "REGISTRY_ID": state["registry_id"],
    })
    # Preserve existing runtime config (requestHeaderAllowlist / filesystem /
    # protocol) — without these the chatbot's custom auth-forwarding header
    # stops reaching the container and MCP gateway calls start 401ing.
    update_kwargs = dict(
        agentRuntimeId=runtime_id,
        agentRuntimeArtifact=rt["agentRuntimeArtifact"],
        roleArn=rt["roleArn"],
        networkConfiguration=rt.get("networkConfiguration", {"networkMode": "PUBLIC"}),
        environmentVariables=env,
    )
    if rt.get("authorizerConfiguration"):
        update_kwargs["authorizerConfiguration"] = rt["authorizerConfiguration"]
    if rt.get("protocolConfiguration"):
        update_kwargs["protocolConfiguration"] = rt["protocolConfiguration"]
    if rt.get("requestHeaderConfiguration"):
        update_kwargs["requestHeaderConfiguration"] = rt["requestHeaderConfiguration"]
    if rt.get("filesystemConfigurations"):
        update_kwargs["filesystemConfigurations"] = rt["filesystemConfigurations"]
    ac.update_agent_runtime(**update_kwargs)
    log(f"  text agent runtime env patched with A2A_*")

    # Attach secrets:GetSecretValue to the role (inline)
    role_arn = rt["roleArn"]
    role_name = role_arn.split("/")[-1]
    iam = boto3.client("iam")
    inline_name = "A2AM2MSecretRead"
    policy_doc = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": [cognito["m2mSecretArn"]],
        }],
    }
    try:
        iam.put_role_policy(RoleName=role_name, PolicyName=inline_name,
                            PolicyDocument=json.dumps(policy_doc))
        log(f"  attached inline policy {inline_name} to {role_name}")
    except Exception as e:
        log(f"  warn: failed to attach inline policy — {e}")


def register_runtimes_for_dashboard(state: dict[str, Any]) -> None:
    """Add the A2A runtime ARNs to the admin Lambda's dashboard allowlist.

    dashboard.py builds an exact `service.name` allowlist from the runtime ARNs
    it is told about. Without this the A2A runtimes' spans and eval metrics are
    filtered out of Overview, so their token spend silently goes unreported.

    Merges into whatever is already there (text/voice/bundles are set by
    setup-agentcore.py) and is idempotent. Non-fatal: a failure here costs
    observability, not function.
    """
    arns = [
        e["runtimeArn"]
        for e in state.get("deployed", {}).get("agents", [])
        if e.get("runtimeArn")
    ]
    if not arns:
        return
    lam = boto3.client("lambda", region_name=state["region"])
    fn = "smarthome-admin-api"
    try:
        env = (lam.get_function_configuration(FunctionName=fn)
               .get("Environment", {}).get("Variables", {}))
        existing = [a.strip()
                    for a in env.get("DASHBOARD_EXTRA_RUNTIME_ARNS", "").split(",")
                    if a.strip()]
        merged = list(existing)
        for a in arns:
            if a not in merged:
                merged.append(a)
        if merged == existing:
            log("  dashboard allowlist already current")
            return
        env["DASHBOARD_EXTRA_RUNTIME_ARNS"] = ",".join(merged)
        lam.update_function_configuration(
            FunctionName=fn, Environment={"Variables": env})
        log(f"  registered {len(arns)} A2A runtime(s) in the dashboard allowlist")
    except Exception as e:
        log(f"  warn: dashboard allowlist update failed — {e}")


# ------------------------------------------------------------------
# Top-level orchestration
# ------------------------------------------------------------------

def find_deployed_agent(deployed: dict[str, Any], agent: str) -> dict[str, Any] | None:
    for a in deployed.get("agents", []):
        if a.get("agent") == agent:
            return a
    return None


def set_deployed_agent(deployed: dict[str, Any], agent: str, entry: dict[str, Any]) -> None:
    agents = deployed.setdefault("agents", [])
    for i, a in enumerate(agents):
        if a.get("agent") == agent:
            agents[i] = entry
            return
    agents.append(entry)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deploy A2A sample agents.")
    ap.add_argument("--agent", action="append", default=[],
                    help="Agent short name (repeatable, or comma-separated).")
    ap.add_argument("--only", action="append", default=None,
                    help="Only run these steps.")
    ap.add_argument("--skip", action="append", default=None,
                    help="Skip these steps.")
    args = ap.parse_args(argv)

    agents = parse_agent_list(args.agent)
    steps = parse_steps(args.only, args.skip)
    log(f"Agents: {agents}")
    log(f"Steps: {sorted(steps)}")

    state = load_state()
    log(f"Region: {state['region']}")
    log(f"Registry: {state['registry_id']}")

    if "cognito" in steps:
        log("\n[cognito] ensuring OAuth2 resources")
        state = ensure_cognito(state)

    for agent in agents:
        log(f"\n=== {agent} ===")
        entry = find_deployed_agent(state["deployed"], agent) or {"agent": agent}

        if "render" in steps or "deploy" in steps:
            project_dir = render_agent_project(agent, state)
            patch_agentcore_json(agent, project_dir, state)
            entry["projectDir"] = str(project_dir)
        else:
            project_dir = Path(entry.get("projectDir", AC_PROJECT_DIR / f"{AGENT_SHORT_SLUG[agent]}"))

        if "deploy" in steps:
            dep = agentcore_deploy(agent, project_dir, state)
            entry.update(dep)

        if "workload" in steps:
            wia = ensure_workload_identity(agent, state)
            if wia:
                entry["workloadIdentityArn"] = wia

        if "registry" in steps:
            rec_id = ensure_registry_record(
                agent, state,
                invocation_url=entry.get("invocationUrl", ""),
                existing_record_id=entry.get("recordId"),
            )
            entry["recordId"] = rec_id

        set_deployed_agent(state["deployed"], agent, entry)

    if "persist" in steps:
        save_deployed(state["deployed"])

    if "patch-text-agent" in steps:
        log("\n[patch-text-agent] wiring A2A envs into smarthome text agent runtime")
        patch_text_agent(state)
        register_runtimes_for_dashboard(state)

    log(
        "\nDone. A2A records are in PENDING_APPROVAL. Open the AgentCore "
        "Registry console and approve them, then grant skill access in the "
        "Admin Console → Users → Manage Permissions."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
