#!/usr/bin/env python3
"""Provision the dedicated AgentCore Gateway that fronts the A2A sub-agents.

Idempotent. Safe to re-run; it converges the gateway, its eight passthrough
targets and its policy engine, and prints what an orchestrator would need to route
through it.

    ./venv/bin/python scripts/setup-a2a-gateway.py            # converge + report
    ./venv/bin/python scripts/setup-a2a-gateway.py --probe    # also run the gate checks
    ./venv/bin/python scripts/setup-a2a-gateway.py --teardown # remove it again

Why a SECOND gateway, and why it is not the tools gateway
--------------------------------------------------------
The tools gateway carries MCP targets that the orchestrator and every sub-agent
call OUTBOUND. This one sits INBOUND in front of the sub-agents themselves. Mixing
them would put a Cedar policy engine that must default-permit A2A delegation on the
same gateway as the per-user tool authorization, where the whole point is
default-deny.

What it buys, and what it does not
----------------------------------
Buys: one egress point for delegation, per-target audit and metrics, and a Cedar
`forbid` that takes effect immediately rather than at the next token refresh (the
one thing the `cognito:groups` model cannot do).

Does NOT buy guardrails. Measured 2026-08-13: `CreatePolicy` with a guardrails
block fails in us-west-2 with `AccessDeniedException: Guardrails policies are not
enabled for this account` — regional, not entitlement — and even where guardrails
ARE supported, Cedar dataPaths cannot traverse arrays, so they cannot read an A2A
message's text at `params.message.parts[i].text`. Content filtering for a
specialist has to be `ApplyGuardrail` inside its own container.

Target shape is `passthrough`, not `agentcoreRuntime`
----------------------------------------------------
Both work, at different paths, and each 404s at the other's. Measured on a
throwaway gateway 2026-08-13:

    passthrough      -> POST /{targetName}
    agentcoreRuntime -> POST /{targetName}/invocations

`passthrough` is chosen because per-target Cedar policies only bind on it: the
schema generated for an `agentcoreRuntime` target declares `{target}___POST:/`
while the live request path is `/invocations`, so only a blanket permit or deny
applies. Per-target control is the reason this gateway exists, so the shape that
supports it wins.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3

REPO = Path(__file__).resolve().parent.parent
REGION = "us-west-2"

GATEWAY_NAME = "smarthome-a2a-gw"

# The tools gateway, read for two things we deliberately do not re-derive: its
# service role (IAM is global, and a second role would be one more thing to keep in
# step) and its Cognito authorizer config, so the SAME end-user idToken authorizes
# both gateways. `_ensure_websearch_gateway` in setup-agentcore.py reuses them the
# same way and for the same reasons.
TOOLS_GATEWAY_ID = "smarthome-smarthomegateway-3nu9ktwpag"

DEPLOYED_STATE = REPO / "a2a-agent-registry" / "deployed-state.json"


def log(msg: str) -> None:
    print(f"  [a2a-gw] {msg}", flush=True)


def _agents() -> list[dict]:
    """The deployed sub-agents, from deploy.py's own state file."""
    if not DEPLOYED_STATE.exists():
        sys.exit(f"{DEPLOYED_STATE} not found — run a2a-agent-registry/deploy.py first")
    agents = json.loads(DEPLOYED_STATE.read_text()).get("agents") or []
    missing = [a["agent"] for a in agents if not a.get("invocationUrl")]
    if missing:
        sys.exit(f"agents with no invocationUrl in deployed-state.json: {missing}")
    return agents


def target_name(agent: str) -> str:
    """The gateway target name for a sub-agent.

    Hyphens are kept: the target name becomes the request PATH (`POST /{name}`), so
    it has to be URL-safe rather than merely valid as an identifier. The agent's own
    short name is used so a 403 or a metric names something a human recognises.
    """
    return agent


def ensure_gateway(ac) -> dict:
    tools = ac.get_gateway(gatewayIdentifier=TOOLS_GATEWAY_ID)
    role_arn = tools["roleArn"]
    authorizer = tools["authorizerConfiguration"]

    found = None
    for page in ac.get_paginator("list_gateways").paginate():
        for g in page.get("items", []):
            if g.get("name") == GATEWAY_NAME:
                found = g["gatewayId"]
                break
        if found:
            break

    if found is None:
        resp = ac.create_gateway(
            name=GATEWAY_NAME,
            roleArn=role_arn,
            authorizerType="CUSTOM_JWT",
            authorizerConfiguration=authorizer,
            description="Inbound gateway fronting the A2A sub-agents",
        )
        found = resp["gatewayId"]
        log(f"created gateway {GATEWAY_NAME} = {found}")
    else:
        log(f"gateway exists: {found}")

    for _ in range(60):
        full = ac.get_gateway(gatewayIdentifier=found)
        if full.get("status") not in ("CREATING", "UPDATING"):
            return full
        time.sleep(2)
    return ac.get_gateway(gatewayIdentifier=found)


def ensure_targets(ac, gateway_id: str, agents: list[dict]) -> dict[str, str]:
    """One passthrough target per sub-agent. Returns {agent: targetId}."""
    existing = {}
    for page in ac.get_paginator("list_gateway_targets").paginate(
            gatewayIdentifier=gateway_id):
        for t in page.get("items", []):
            existing[t.get("name")] = t["targetId"]

    out = {}
    for entry in agents:
        agent = entry["agent"]
        name = target_name(agent)
        if name in existing:
            out[agent] = existing[name]
            continue
        resp = ac.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=name,
            description=f"A2A passthrough to {agent}",
            targetConfiguration={"http": {"passthrough": {
                "endpoint": entry["invocationUrl"],
                "protocolType": "A2A",
            }}},
            # JWT_PASSTHROUGH, so the end user's own idToken reaches the sub-agent
            # unchanged. That token IS the authorization: the sub-agent's runtime
            # authorizer validates it and matches `cognito:groups` against its own
            # grant groups. A GATEWAY_IAM_ROLE credential here would replace the
            # caller's identity with the gateway's and break per-user authorization
            # entirely — the container would see no user.
            credentialProviderConfigurations=[
                {"credentialProviderType": "JWT_PASSTHROUGH"}],
        )
        out[agent] = resp["targetId"]
        log(f"created target {name} -> {entry['invocationUrl'][:60]}…")

    for _ in range(60):
        states = {}
        for page in ac.get_paginator("list_gateway_targets").paginate(
                gatewayIdentifier=gateway_id):
            for t in page.get("items", []):
                states[t["name"]] = t.get("status", "")
        if not any(s in ("CREATING", "UPDATING") for s in states.values()):
            log(f"target states: {sorted(set(states.values()))}")
            break
        time.sleep(2)
    return out


POLICY_ENGINE_NAME = "SmartHomeA2ADelegation"


def ensure_policy_engine(ac, gateway_arn: str, agents: list[dict],
                         mode: str) -> str:
    """Attach a policy engine carrying an explicit permit for every A2A target.

    The permit is not optional and not a formality. Measured 2026-08-13: Cedar in
    ENFORCE **default-denies** A2A and runtime targets — a gateway with a policy
    engine and zero policies answers
    `403 ... [No policy applies to the request (denied by default)]` for every
    delegation. The devguide line saying policy evaluation applies only to MCP tools
    is wrong here.

    `mode` is `LOG_ONLY` or `ENFORCE`. Attach LOG_ONLY first: Cedar evaluates and
    traces without denying, so a wrong action name shows up in the traces instead of
    taking every specialist down. Flip to ENFORCE once a probe confirms the permit
    matches.

    The action name is `{targetName}___POST:/`, which is what the generated schema
    declares for a passthrough target. `agentcoreRuntime` targets declare the same
    shape while the live path is `/invocations`, which is why per-target policies
    never bound on them — see the module docstring.
    """
    engine_id = None
    for e in ac.list_policy_engines().get("policyEngines", []):
        if e.get("name") == POLICY_ENGINE_NAME:
            engine_id = e["policyEngineId"]
    if engine_id is None:
        engine_id = ac.create_policy_engine(
            name=POLICY_ENGINE_NAME,
            description="Permits A2A delegation through smarthome-a2a-gw",
        )["policyEngineId"]
        log(f"created policy engine {engine_id}")
    for _ in range(60):
        eng = ac.get_policy_engine(policyEngineId=engine_id)
        if eng.get("status") not in ("CREATING", "UPDATING"):
            break
        time.sleep(2)
    engine_arn = eng.get("policyEngineArn") or eng.get("arn")

    existing = {p.get("name") for p in
                ac.list_policies(policyEngineId=engine_id).get("policies", [])}
    actions = " || ".join(
        f'action == AgentCore::Action::"{target_name(a["agent"])}___POST:/"'
        for a in agents)
    statement = (
        "permit(\n  principal,\n  action,\n"
        f'  resource == AgentCore::Gateway::"{gateway_arn}"\n'
        ") when {\n"
        "  ((principal is AgentCore::OAuthUser) || (principal is AgentCore::IamEntity))\n"
        f"  && ({actions})\n"
        "};"
    )
    name = "A2ADelegationPermit"
    if name in existing:
        log(f"policy {name} already present")
    else:
        ac.create_policy(
            policyEngineId=engine_id, name=name,
            description="Delegation to any A2A sub-agent target on this gateway",
            definition={"cedar": {"statement": statement}},
            # IGNORE_ALL_FINDINGS, and the finding it ignores is CORRECT: the
            # analyzer answers "Overly Permissive: will allow every request for the
            # specified principal, action and resource". That is exactly what this
            # policy is for.
            #
            # Cedar cannot do better HERE, measured 2026-08-15 by probing the
            # generated schema: `AgentCore::OAuthUser` exposes no `groups` and no
            # `claims` attribute, so a policy cannot read the caller's
            # `cognito:groups` and cannot express "permit if this user was granted
            # this agent". Its only per-user lever is an explicit `principal.id`
            # list — which is the per-user materialisation this deployment just
            # moved away from.
            #
            # So authorization stays one layer down, at each sub-agent runtime's
            # `customJWTAuthorizer.customClaims`, where it is enforced on a signed
            # claim before any container runs. Verified through this hop:
            # scripts/probe-a2a-gateway.py sees HTTP 401 for a user narrowed to no
            # grant. What the engine adds is the per-agent `forbid` below — an
            # immediate, all-users kill switch that the claim model cannot give,
            # because a claim only changes at the next token.
            validationMode="IGNORE_ALL_FINDINGS",
            enforcementMode="ACTIVE",
        )
        log(f"created policy {name} over {len(agents)} target action(s)")

    for _ in range(60):
        states = {p["name"]: ac.get_policy(
            policyEngineId=engine_id, policyId=p["policyId"]).get("status")
            for p in ac.list_policies(policyEngineId=engine_id).get("policies", [])}
        if not any(s in ("CREATING", "UPDATING") for s in states.values()):
            log(f"policy states: {states}")
            break
        time.sleep(3)

    # Attaching is an UpdateGateway, which is a full PUT of the fields it carries;
    # the authorizer config has to be passed back or the gateway loses it.
    gw = ac.get_gateway(gatewayIdentifier=gateway_arn.split("/")[-1])
    ac.update_gateway(
        gatewayIdentifier=gw["gatewayId"],
        name=gw["name"],
        roleArn=gw["roleArn"],
        authorizerType=gw["authorizerType"],
        authorizerConfiguration=gw["authorizerConfiguration"],
        policyEngineConfiguration={"arn": engine_arn, "mode": mode},
    )
    log(f"attached policy engine in {mode} mode")
    for _ in range(60):
        full = ac.get_gateway(gatewayIdentifier=gw["gatewayId"])
        if full.get("status") not in ("CREATING", "UPDATING"):
            break
        time.sleep(2)
    return engine_id


def set_agent_forbid(ac, gateway_arn: str, agent: str, on: bool) -> None:
    """Add or remove an immediate, all-users deny for ONE sub-agent.

    This is the lever the whole policy engine exists for. Revoking a grant changes a
    Cognito claim, which only takes effect at the caller's next token — up to the ID
    token lifetime, an hour here. A Cedar `forbid` denies at the gateway on the very
    next request, for everyone, without touching anyone's grants.

    Deliberately manual: a runbook action, not automation. Wiring it to the same
    signal the revocation sweep already acts on would put two mechanisms on one
    decision, and the sweep is the one with the tests.
    """
    engine_id = None
    for e in ac.list_policy_engines().get("policyEngines", []):
        if e.get("name") == POLICY_ENGINE_NAME:
            engine_id = e["policyEngineId"]
    if not engine_id:
        sys.exit("no policy engine — run with --policy first")

    name = f"Forbid_{agent.replace('-', '_')}"
    existing = {p["name"]: p["policyId"] for p in
                ac.list_policies(policyEngineId=engine_id).get("policies", [])}
    if not on:
        if name in existing:
            ac.delete_policy(policyEngineId=engine_id, policyId=existing[name])
            log(f"removed {name}")
        else:
            log(f"{name} was not present")
        return
    if name in existing:
        log(f"{name} already present")
        return
    statement = (
        "forbid(\n  principal,\n"
        f'  action == AgentCore::Action::"{target_name(agent)}___POST:/",\n'
        f'  resource == AgentCore::Gateway::"{gateway_arn}"\n'
        ") when {\n  (principal is AgentCore::OAuthUser) "
        "|| (principal is AgentCore::IamEntity)\n};"
    )
    ac.create_policy(
        policyEngineId=engine_id, name=name,
        description=f"Immediate all-users deny for {agent}",
        definition={"cedar": {"statement": statement}},
        # IGNORE_ALL_FINDINGS. Conditioning on the principal type does NOT satisfy
        # the analyzer — measured 2026-08-15, it still answers "Overly Restrictive:
        # will deny every request for the specified principal, action and resource",
        # once per principal type. Which is an accurate description of a kill
        # switch: denying every request for one agent is the entire feature. A
        # condition narrow enough to pass would be a condition that lets some
        # requests through.
        validationMode="IGNORE_ALL_FINDINGS",
        enforcementMode="ACTIVE",
    )
    log(f"created {name}")
    for _ in range(40):
        pol = {p["name"]: ac.get_policy(policyEngineId=engine_id,
                                        policyId=p["policyId"]).get("status")
               for p in ac.list_policies(policyEngineId=engine_id).get("policies", [])}
        if pol.get(name) not in ("CREATING", "UPDATING"):
            log(f"policy states: {pol}")
            return
        time.sleep(3)


def detach_policy_engine(ac) -> None:
    """Take the policy engine back off the gateway. The rollback for §4."""
    for page in ac.get_paginator("list_gateways").paginate():
        for g in page.get("items", []):
            if g.get("name") != GATEWAY_NAME:
                continue
            gw = ac.get_gateway(gatewayIdentifier=g["gatewayId"])
            ac.update_gateway(
                gatewayIdentifier=gw["gatewayId"], name=gw["name"],
                roleArn=gw["roleArn"], authorizerType=gw["authorizerType"],
                authorizerConfiguration=gw["authorizerConfiguration"],
            )
            log("detached the policy engine")
            return
    log("gateway not found")


def teardown(ac) -> None:
    found = None
    for page in ac.get_paginator("list_gateways").paginate():
        for g in page.get("items", []):
            if g.get("name") == GATEWAY_NAME:
                found = g["gatewayId"]
    if not found:
        log("nothing to tear down")
        return
    for page in ac.get_paginator("list_gateway_targets").paginate(
            gatewayIdentifier=found):
        for t in page.get("items", []):
            ac.delete_gateway_target(gatewayIdentifier=found, targetId=t["targetId"])
            log(f"deleted target {t['name']}")
    time.sleep(5)
    ac.delete_gateway(gatewayIdentifier=found)
    log(f"deleted gateway {found}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--teardown", action="store_true")
    ap.add_argument("--policy", choices=("LOG_ONLY", "ENFORCE"),
                    help="also attach the policy engine in this mode")
    ap.add_argument("--detach-policy", action="store_true")
    ap.add_argument("--forbid", metavar="AGENT",
                    help="add an immediate all-users deny for one sub-agent")
    ap.add_argument("--allow", metavar="AGENT",
                    help="remove the deny added by --forbid")
    args = ap.parse_args(argv)

    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
    if args.teardown:
        teardown(ac)
        return 0
    if args.detach_policy:
        detach_policy_engine(ac)
        return 0
    if args.forbid or args.allow:
        gw = ensure_gateway(ac)
        set_agent_forbid(ac, gw["gatewayArn"], args.forbid or args.allow,
                         on=bool(args.forbid))
        return 0

    agents = _agents()
    gw = ensure_gateway(ac)
    log(f"status={gw.get('status')} url={gw.get('gatewayUrl')}")
    targets = ensure_targets(ac, gw["gatewayId"], agents)
    if args.policy:
        ensure_policy_engine(ac, gw["gatewayArn"], agents, args.policy)

    print()
    print(f"gatewayId  : {gw['gatewayId']}")
    print(f"gatewayUrl : {gw.get('gatewayUrl')}")
    print("targets    :")
    for entry in agents:
        print(f"  {entry['agent']:<24} POST {gw.get('gatewayUrl')}/{target_name(entry['agent'])}")
    print()
    if not args.policy:
        print("NO policy engine attached. Cedar ENFORCE default-DENIES A2A and")
        print("runtime targets, so pass --policy LOG_ONLY first, probe, then")
        print("--policy ENFORCE.")
    print("The AgentCards still point at the runtimes unless the cutover has been")
    print("run separately, so nothing routes through here until then.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
