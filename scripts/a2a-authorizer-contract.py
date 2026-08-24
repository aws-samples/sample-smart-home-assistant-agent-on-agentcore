#!/usr/bin/env python3
"""Print the Runtime authorizer config an A2A agent must be deployed with here.

    ./venv/bin/python scripts/a2a-authorizer-contract.py --card path/to/card.json
    ./venv/bin/python scripts/a2a-authorizer-contract.py --record-id rXu3cIg291cn
    ./venv/bin/python scripts/a2a-authorizer-contract.py --card c.json --format cli

Registering an APPROVED AgentCard is all it takes to be DISCOVERED by this system:
the console lists the agent, the orchestrator registers `a2a_*` tools for it, and the
delegation prompt names it — no code change and no redeploy anywhere upstream.

It is not enough to be CALLABLE. That is decided by the agent's OWN Runtime
authorizer, and it has to be configured against this deployment's Cognito pool and
enumerate this card's grant groups. Nothing about that is guessable, and both ways of
getting it wrong are silent:

  - no `customClaims`  -> every authenticated user of the pool reaches every skill
  - wrong pool/audience -> granted users are refused with 401, and the orchestrator
                           still offers the tool, so the model apologises instead

So this prints the answer rather than leaving it to be inferred from a failure. The
group list comes from `shared/a2a_conformance.authorizer_for`, the same function the
console's conformance check compares against — so what we hand out and what we check
cannot drift.

**The authorizer list is ONE group and never changes** (`a2a-<cardName>`). Adding,
renaming or removing a skill does not require touching the runtime. It used to: the
list was the full per-skill enumeration, and `CONTAINS_ANY` has no wildcard, so a new
skill left its grantees refused at the door until someone redeployed. That
enumeration bought no authorization to pay for the coupling — the door passed on ANY
one of the groups, and the container derives the skill subset from the same signed
claim regardless. See `shared/a2a_groups.authorizer_groups`.

What DOES change the authorizer: renaming the card. The group name is keyed on the
card name, so a rename is a new agent as far as every enforcement point is concerned.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "shared"))

import a2a_conformance as conf  # noqa: E402
import agent_registry as registry_ns  # noqa: E402

REGION = "us-west-2"


def _deployment_identity() -> tuple[str, str]:
    """(discoveryUrl, appClientId) for this deployment, from cdk-outputs.json.

    Read rather than passed in: an author copying a wrong pool id produces exactly
    the silent 401 this script exists to prevent.
    """
    outputs = json.loads((REPO / "cdk-outputs.json").read_text())
    stack = next(iter(outputs.values()))
    pool = stack["UserPoolId"]
    client = stack["UserPoolClientId"]
    return (f"https://cognito-idp.{REGION}.amazonaws.com/{pool}"
            "/.well-known/openid-configuration"), client


def _card_from_record(record_id: str) -> dict:
    registry_id = json.loads((REPO / "agentcore-state.json").read_text())["registryId"]
    client = registry_ns.registry_client(REGION)
    detail = client.get_registry_record(registryId=registry_id, recordId=record_id)
    raw = registry_ns.read_agent_card(detail)
    if not raw:
        sys.exit(f"record {record_id} carries no AgentCard")
    return json.loads(raw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--card", help="path to the agent's card.json")
    src.add_argument("--record-id", help="an existing Registry record to read the card from")
    ap.add_argument("--format", choices=("json", "cli", "python"), default="json")
    args = ap.parse_args(argv)

    card = (json.loads(Path(args.card).read_text(encoding="utf-8"))
            if args.card else _card_from_record(args.record_id))
    discovery_url, app_client = _deployment_identity()

    door, findings = conf.expected_groups(card)
    grantable, _ = conf.grantable_groups(card)
    for f in findings:
        print(f"WARNING [{f['code']}] {f['detail']}", file=sys.stderr)
    if not door:
        return 1

    authorizer = conf.authorizer_for(card, discovery_url, app_client)
    payload = {"customJWTAuthorizer": authorizer}

    print(f"# agent      : {card.get('name')}")
    print(f"# skills     : {[s.get('id') for s in card.get('skills') or []]}")
    print("#")
    print(f"# DOOR — the only group your authorizer needs to match ({len(door)}):")
    for g in door:
        print(f"#   {g}")
    print("#   Stable for this agent's lifetime. Add or remove skills freely; this")
    print("#   list does not move, so a card edit is not also a runtime redeploy.")
    print("#")
    print(f"# GRANTS — what an admin hands out per skill ({len(grantable)}):")
    for g in grantable:
        print(f"#   {g}")
    print("#   You do NOT list these in the authorizer. Your container reads them")
    print("#   from the same signed claim to decide which skills a caller holds.")
    print("#")
    print("# Renaming the card DOES change the door group — that is a new agent to")
    print("# every enforcement point, and existing grants will not follow it.")
    print()

    if args.format == "json":
        print(json.dumps(payload, indent=2))
    elif args.format == "python":
        print("import boto3\n"
              "ac = boto3.client('bedrock-agentcore-control', "
              f"region_name='{REGION}')\n"
              "# update_agent_runtime is a full PUT: pass roleArn,\n"
              "# agentRuntimeArtifact and networkConfiguration back unchanged too.\n"
              "ac.update_agent_runtime(\n"
              "    agentRuntimeId='<YOUR_RUNTIME_ID>',\n"
              "    ...,\n"
              f"    authorizerConfiguration={payload!r},\n"
              ")")
    else:
        print("aws bedrock-agentcore-control update-agent-runtime \\")
        print(f"  --region {REGION} \\")
        print("  --agent-runtime-id <YOUR_RUNTIME_ID> \\")
        print("  # update-agent-runtime is a full PUT — pass --role-arn,")
        print("  # --agent-runtime-artifact and --network-configuration back too \\")
        print(f"  --authorizer-configuration '{json.dumps(payload)}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
