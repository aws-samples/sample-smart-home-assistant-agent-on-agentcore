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
    SCENARIO_AGENT,
    SECRET_NAME,
    USER_TOKEN_HEADER,
)

# Set from --legacy-m2m-auth. Deploys the pre-migration auth model: the m2m client
# in `allowedClients` and no `cognito:groups` grant check. The two models cannot
# coexist on one runtime, because a client_credentials token carries no groups claim
# and would be refused by the check, so this is a switch rather than a flag that
# widens acceptance. It exists as the rollback for the claim migration.
LEGACY_M2M_AUTH = False

ALL_STEPS = ("cognito", "render", "deploy", "workload", "registry", "persist", "patch-text-agent")

# The single-table store the Admin Console writes prompt overrides into. Named by
# the CDK stack (`skillsTable`) and hardcoded there too; a sub-agent reads it to
# resolve its governed prompt. Same literal the setup script uses.
SKILLS_TABLE = "smarthome-skills"
# The task-management agent's own table. It is the ONLY table that agent may
# write, which is why scenes did not go into the skills table: writing there would
# let it edit the permission and prompt rows that govern it.
SCENARIOS_TABLE = "smarthome-scenarios"


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


def _websearch_gateway_env(state: dict[str, Any]) -> dict[str, str]:
    """The web-search gateway URL, read off the main text runtime.

    Deliberately NOT covered by the AGENTCORE_GATEWAY_* copy above. That prefix is
    what `common.gateway_tools.gateway_url()` scans to find the TOOLS gateway, and
    that scan returns whichever key it iterates over last — so naming this one to
    match would intermittently point every sub-agent's device tools at a gateway
    that serves only web search.
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
    url = env.get("WEBSEARCH_GATEWAY_URL", "")
    return {"WEBSEARCH_GATEWAY_URL": url} if url else {}


def _text_agent_memory_env(state: dict[str, Any]) -> dict[str, str]:
    """Read the shared Memory id off the main text runtime.

    Copied from the orchestrator rather than configured here, for the same reason
    the Gateway env is: it must be the SAME memory, and a second source of the id
    is a second chance for the two to disagree. A specialist pointed at its own
    memory would work — it would just retrieve nothing, forever, which reads as a
    retrieval bug rather than a configuration one.

    Note the var name embeds the memory's logical name
    (`MEMORY_SMARTHOMEMEMORY_ID`) because that is what the agentcore CLI sets on
    the orchestrator; `common/memory.py` reads the same key so the two match by
    construction.
    """
    runtime_id = state.get("text_agent_runtime_id", "")
    if not runtime_id:
        return {}
    try:
        ac = boto3.client("bedrock-agentcore-control", region_name=state["region"])
        env = ac.get_agent_runtime(
            agentRuntimeId=runtime_id).get("environmentVariables") or {}
    except Exception as exc:  # noqa: BLE001
        log(f"  warn: could not read the main runtime's memory env: {exc}")
        return {}
    return {k: v for k, v in env.items()
            if k.startswith("MEMORY_") and k.endswith("_ID") and v}


def _memory_arn_from_id(memory_id: str, account_id: str, region: str) -> str:
    return (f"arn:aws:bedrock-agentcore:{region}:{account_id}:memory/{memory_id}"
            if memory_id and account_id else "")


def _grant_memory_read(agent: str, role_arn: str, memory_env: dict[str, str],
                       state: dict[str, Any]) -> None:
    """Let this sub-agent RETRIEVE from the shared Memory, and nothing else.

    `RetrieveMemoryRecords` alone. Not CreateEvent, not ListEvents: writing is the
    orchestrator's job because only it holds the whole conversation (see
    common/memory.py), and the cheapest way to keep that true as the code changes
    is for the permission not to exist. A future edit that tried to write would
    fail loudly here rather than quietly poison the user's long-term memory with
    half-sentences.

    Its own inline policy name, like every other grant in this file:
    `put_role_policy` REPLACES a document, so sharing a name with
    `A2APromptTableRead` would silently delete whichever grant was applied first.
    """
    # Account id off the role ARN rather than an STS call, matching the other
    # grants in this file — the role is in the account we are deploying into by
    # definition.
    account_id = role_arn.split(":")[4]
    resources = [
        arn for arn in (
            _memory_arn_from_id(m, account_id, state["region"])
            for m in memory_env.values() if m
        ) if arn
    ]
    if not resources:
        log(f"  [{agent}] no shared memory id — skipping the Memory read grant")
        return
    doc = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:RetrieveMemoryRecords"],
            "Resource": resources,
        }],
    }
    role_name = role_arn.split("/")[-1]
    try:
        boto3.client("iam").put_role_policy(
            RoleName=role_name,
            PolicyName="A2ASharedMemoryRead",
            PolicyDocument=json.dumps(doc),
        )
        log(f"  [{agent}] granted RetrieveMemoryRecords on {len(resources)} memory(ies)")
    except Exception as exc:  # noqa: BLE001
        # Soft: the agent still answers, just without the user's remembered
        # context. Loud in the log because the symptom otherwise is "the
        # specialist never remembers anything", which looks like a code bug.
        log(f"  [{agent}] WARNING: could not grant Memory read: {exc}")


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

    # `shared/` — the device catalog and the scenario model, which the repo's
    # Lambdas already import. A sub-agent that validates a device command or a
    # scene has to agree with them exactly: a second copy of "what speeds does the
    # fan accept" is a copy that will disagree, and it would disagree by storing a
    # scene that the execution path then refuses. Copied rather than pip-installed
    # because these are repo modules, not a package.
    shared_src = PROJECT_ROOT / "shared"
    if shared_src.is_dir():
        shutil.copytree(shared_src, code_root / "shared",
                        ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"))

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
        # Prompt governance: common/governed_prompt reads the admin's override
        # from this table per request. Absent, the agent silently uses the prompt
        # baked into its image — which is why the env var and the IAM grant below
        # are applied together rather than in separate steps.
        "SKILLS_TABLE_NAME": SKILLS_TABLE,
    })
    # Only the agent that owns scenes learns the table's name. An agent with no
    # reason to touch it should not be able to name it.
    if (HERE / agent / "tools.py").exists() and agent == SCENARIO_AGENT:
        env["SCENARIOS_TABLE_NAME"] = SCENARIOS_TABLE
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
    tools_py = HERE / agent / "tools.py"
    if tools_py.exists():
        gateway_env = _text_agent_gateway_env(state)
        if gateway_env:
            env.update(gateway_env)
            log(f"  [{agent}] gateway env: {sorted(gateway_env)}")
        else:
            log(f"  [{agent}] WARNING: could not find AGENTCORE_GATEWAY_*_URL on "
                f"the main runtime — this agent will have no device tools")

        # Web search is a SECOND gateway in another region, and only the agents
        # that ask for it are told where it is — same reasoning as the scenarios
        # table above. Detected from the tools module declaring WEB_SEARCH rather
        # than from a hardcoded agent name, so the roster and the wiring cannot
        # drift apart.
        if "WEB_SEARCH" in tools_py.read_text():
            ws_env = _websearch_gateway_env(state)
            if ws_env:
                env.update(ws_env)
                log(f"  [{agent}] web-search gateway env applied")
            else:
                log(f"  [{agent}] note: tools.py wants WEB_SEARCH but the main "
                    f"runtime has no WEBSEARCH_GATEWAY_URL — this agent will "
                    f"assess without live-web advisories")

    # The shared Memory, for EVERY agent including the prompt-only advisors: a
    # security or energy recommendation is better for knowing the user prefers
    # warm light at night, and none of them needs a tool to use that. Read-only,
    # enforced by the grant below.
    memory_env = _text_agent_memory_env(state)
    if memory_env:
        env.update(memory_env)
        log(f"  [{agent}] shared memory env: {sorted(memory_env)}")
    else:
        log(f"  [{agent}] note: no MEMORY_*_ID on the main runtime — this agent "
            f"will answer without the user's remembered context")
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
            "customJWTAuthorizer": _authorizer_config(
                agent, discovery_url, cognito, state),
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
            # `Authorization` FIRST, and it is the one that matters now: the token in
            # it is both the credential the authorizer checks AND the source of the
            # `cognito:groups` claim the container derives the skill set from. The
            # Runtime edge consumes Authorization and does NOT pass it through unless
            # it is allowlisted here — measured: with it absent, a fully granted
            # user's request reached the container with no bearer at all, so the
            # container fell back to the legacy header path and refused. The platform
            # said yes and the container said no, which reads like a container bug.
            #
            # The two legacy headers stay only for the rollback path
            # (--legacy-m2m-auth); nothing sends them once the migration is done.
            "requestHeaderAllowlist": [
                "Authorization", ALLOWED_SKILLS_HEADER, USER_TOKEN_HEADER],
        },
    )
    ac.update_agent_runtime(**update_kwargs)
    log(f"  [{agent}] patched env + CUSTOM_JWT auth (discovery={discovery_url})")
    log(f"  [{agent}] header allowlist: Authorization, {ALLOWED_SKILLS_HEADER}, "
        f"{USER_TOKEN_HEADER}")

    _grant_prompt_table_read(agent, rt_info["roleArn"], state)
    _grant_memory_read(agent, rt_info["roleArn"], memory_env, state)
    if agent == SCENARIO_AGENT:
        _grant_scenarios_table_access(agent, rt_info["roleArn"], state)

    return {
        "cfnStack": cfn_stack,
        "runtimeId": runtime_id,
        "runtimeArn": runtime_arn,
        "invocationUrl": invocation_url,
    }


def _grant_scenarios_table_access(agent: str, role_arn: str,
                                 state: dict[str, Any]) -> None:
    """Let the task-management agent read and write its own scenarios table.

    The only sub-agent that gets write access to anything, and the grant is
    deliberately narrow in two ways:

      - one table. Not the skills table, which holds the permission and prompt
        rows that govern this very agent — an agent able to rewrite those governs
        itself. That is the reason scenes are a separate table rather than more
        `skillName` prefixes.
      - no IoT, no Gateway. The agent stores actions and hands them back; the
        orchestrator applies them under the user's identity so Cedar authorises
        each one. Write access here does not become device access.

    The index ARN is listed separately: a Query against TemplateIndex is denied by
    a policy that names only the table, and the resulting failure looks like an
    empty template library rather than a permissions error.
    """
    role_name = role_arn.split("/")[-1]
    account_id = role_arn.split(":")[4]
    table_arn = (f"arn:aws:dynamodb:{state['region']}:"
                 f"{account_id}:table/{SCENARIOS_TABLE}")
    doc = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query",
                       "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
            "Resource": [table_arn, f"{table_arn}/index/*"],
        }],
    }
    try:
        boto3.client("iam").put_role_policy(
            RoleName=role_name, PolicyName="A2AScenariosTableAccess",
            PolicyDocument=json.dumps(doc))
        log(f"  [{agent}] granted read/write on {SCENARIOS_TABLE} (+ its indexes)")
    except Exception as exc:  # noqa: BLE001
        log(f"  [{agent}] WARNING: could not grant {SCENARIOS_TABLE} access — "
            f"{exc}. Every scene tool will fail at runtime.")


def _authorizer_config(agent: str, discovery_url: str, cognito: dict[str, Any],
                       state: dict[str, Any]) -> dict[str, Any]:
    """The Runtime's inbound JWT authorizer, including the grant claim check.

    This is where sub-agent authorization actually happens. `customClaims` matches
    `cognito:groups` with `CONTAINS_ANY` over every group of this agent, so a caller
    with no grant is refused by AgentCore before the container is reached — on a
    claim signed by Cognito, which the caller cannot forge or widen. It replaces
    `X-A2A-Allowed-Skills`, which the client set itself.

    `CONTAINS_ANY` takes an exact list with no wildcard, so the agent's skills are
    enumerated here from card.json. **Adding a skill therefore needs a redeploy**:
    granting a group that this list does not name leaves the user refused at the
    door with nothing explaining why.

    **The claim check and the old m2m token cannot coexist.** A
    `client_credentials` token carries no `cognito:groups` at all, so once
    `customClaims` is set the authorizer refuses it — there is no both-ways window
    at this layer. The cutover is therefore coordinated: all eight agents get this
    config, then the orchestrator switches to sending the user token. Delegation
    fails in between, which is why `--legacy-m2m-auth` exists as the rollback and
    why the sequence is written down in README's demo notes.

    **`allowedAudience`, not `allowedClients`.** `allowedClients` validates the
    `client_id` claim, which only an *access* token carries; a Cognito **idToken**
    carries the app client id in `aud`. Measured against the live runtime: with
    `allowedClients` set, a fully granted user's idToken was rejected with
    "Claim 'client_id' value mismatch with configuration" — while an ungranted user
    got that message *plus* "Authorization denied", which is how we could tell the
    group check itself was passing.

    The idToken is the right token here regardless: the container needs `sub` and
    `email` (the knowledge base scopes by email) and requires `token_use == "id"`,
    and a Cognito access token has neither `aud` nor `email`.

    The m2m client is kept only under `--legacy-m2m-auth`, which also omits the claim
    check.
    """
    from common import a2a_groups  # type: ignore

    card_dict = json.loads((HERE / agent / "card.json").read_text(encoding="utf-8"))
    card_name = card_dict.get("name") or ""
    skill_ids = [s["id"] for s in (card_dict.get("skills") or []) if s.get("id")]
    if not card_name or not skill_ids:
        raise RuntimeError(
            f"[{agent}] card.json needs a name and at least one skill id to build "
            f"the grant claim check (got name={card_name!r}, skills={skill_ids})")

    app_client = state.get("user_pool_client_id", "")

    if LEGACY_M2M_AUTH:
        log(f"  [{agent}] LEGACY auth: m2m client, no grant claim check")
        return {
            "discoveryUrl": discovery_url,
            "allowedClients": [cognito["clientId"]],
        }

    if not app_client:
        raise RuntimeError(
            f"[{agent}] no UserPoolClientId in cdk-outputs.json. The grant claim "
            f"path authorizes the END USER's token, so without the app client id "
            f"every request would be refused. Re-run with --legacy-m2m-auth to "
            f"deploy the pre-migration auth model instead.")

    # ONE stable agent-level group, not the per-skill list this used to enumerate.
    # `CONTAINS_ANY` has no wildcard, so the old list made "add a skill to the card"
    # into "redeploy this runtime or its grantees are refused at the door with nothing
    # explaining why". It bought no authorization to pay for that: the door passed on
    # ANY one of the skill groups, and the container derives the skill subset from the
    # same signed claim anyway (`common/server.enforce_allowed_skills`). See
    # `shared/a2a_groups.authorizer_groups`.
    #
    # ORDER MATTERS on the way in: the admin API and the pre-token trigger have to be
    # emitting `a2a-<agent>` before this runs, or every already-granted user is refused
    # until the next materialisation reaches them.
    groups = a2a_groups.authorizer_groups(card_name, skill_ids)
    log(f"  [{agent}] door group: {groups} "
        f"(skills gated in-container: {sorted(skill_ids)})")
    return {
        "discoveryUrl": discovery_url,
        "allowedAudience": [app_client],
        "customClaims": [{
            "inboundTokenClaimName": "cognito:groups",
            "inboundTokenClaimValueType": "STRING_ARRAY",
            "authorizingClaimMatchValue": {
                "claimMatchValue": {"matchValueStringList": groups},
                "claimMatchOperator": "CONTAINS_ANY",
            },
        }],
    }


def _grant_prompt_table_read(agent: str, role_arn: str, state: dict[str, Any]) -> None:
    """Let this sub-agent's runtime role read its governed prompt.

    Read-only, and scoped to the one table. A sub-agent resolves its own prompt
    override; it has no reason to write governance state, and the whole point of
    the design is that the Admin Console is the only writer.

    Written as its own inline policy rather than merged into `A2AM2MSecretRead`:
    `put_role_policy` REPLACES a policy document, so sharing one name means
    whichever step runs last wins and the other grant vanishes. Same trap the
    tools-Gateway grant hit in setup-agentcore.py.

    Failure is logged, not raised — but loudly, because the symptom is subtle: the
    agent keeps working, using the prompt baked into its image, and an admin's
    saved override appears to be ignored for no visible reason.
    """
    # `arn:aws:iam::<account>:role/<name>` — the account is already here, so no
    # STS round-trip, and it is by construction the account the role lives in.
    role_name = role_arn.split("/")[-1]
    account_id = role_arn.split(":")[4]
    table_arn = (f"arn:aws:dynamodb:{state['region']}:"
                 f"{account_id}:table/{SKILLS_TABLE}")
    doc = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["dynamodb:GetItem"],
            "Resource": [table_arn],
        }],
    }
    try:
        boto3.client("iam").put_role_policy(
            RoleName=role_name, PolicyName="A2APromptTableRead",
            PolicyDocument=json.dumps(doc))
        log(f"  [{agent}] granted dynamodb:GetItem on {SKILLS_TABLE} "
            f"(prompt governance)")
    except Exception as exc:  # noqa: BLE001
        log(f"  [{agent}] WARNING: could not grant {SKILLS_TABLE} read — {exc}. "
            f"Admin prompt overrides will be SILENTLY IGNORED by this agent; it "
            f"will keep using the prompt from its image.")


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

def _as_update_descriptors(descriptors: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a Create-shaped descriptor tree into Update's shape.

    `CreateRegistryRecord` takes descriptors bare. `UpdateRegistryRecord` wraps
    EVERY level in `optionalValue` — the union, each descriptor, and each field:

        create  {"a2aAgentCard": {"data": "<json>"}}
        update  {"optionalValue": {"a2aAgentCard": {"optionalValue":
                    {"data": {"optionalValue": "<json>"}}}}}

    Measured from the botocore service model rather than guessed:

        c = boto3.client("agent-registry-control")
        c.meta.service_model.operation_model("UpdateRegistryRecord") \\
         .input_shape.members["descriptors"]                     # -> optionalValue
         ...members["optionalValue"].members["a2aAgentCard"]     # -> optionalValue
         ...members["optionalValue"].members["data"]             # -> optionalValue

    Why this is done by a function and not by hand: wrapping only the outer level
    is a fix that LOOKS right and is not, and the failure is silent. Any update
    error is treated as "delete the record and recreate it" by the caller; that
    path succeeds, so a redeploy reports success while minting a NEW recordId —
    and `a2aGrants` in every `__a2a_permissions__` row is keyed by recordId. The
    user-visible symptom is a specialist whose skills were granted yesterday
    having no `a2a_*` tools today, with nothing in any log. That happened twice:
    once before the outer wrap was added, and again on the next deploy because the
    inner two levels were still bare.
    """
    def wrap(value: Any, depth: int) -> Any:
        if isinstance(value, dict):
            return {"optionalValue": {k: wrap(v, depth + 1) for k, v in value.items()}}
        return {"optionalValue": value}

    return {"optionalValue": {
        name: wrap(fields, 0) for name, fields in descriptors.items()
    }}


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
    update_descriptor_payload = _as_update_descriptors(descriptor_payload)

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
                descriptors=update_descriptor_payload,
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
                            descriptors=update_descriptor_payload,
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
    ap.add_argument("--legacy-m2m-auth", action="store_true",
                    help="Deploy the pre-migration auth model: m2m client_credentials "
                         "in Authorization, no cognito:groups grant check. This is "
                         "the rollback for the claim-based migration; it must be "
                         "paired with an orchestrator that still sends an m2m token.")
    args = ap.parse_args(argv)

    global LEGACY_M2M_AUTH
    LEGACY_M2M_AUTH = bool(args.legacy_m2m_auth)
    if LEGACY_M2M_AUTH:
        log("LEGACY M2M AUTH: sub-agent authorization falls back to the m2m token "
            "and the cognito:groups grant check is NOT applied")

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
