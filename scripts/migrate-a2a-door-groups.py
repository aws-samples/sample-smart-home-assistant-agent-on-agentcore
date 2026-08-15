#!/usr/bin/env python3
"""Flip each A2A sub-agent's Runtime authorizer to the stable door group.

    ./venv/bin/python scripts/migrate-a2a-door-groups.py            # report only
    ./venv/bin/python scripts/migrate-a2a-door-groups.py --apply
    ./venv/bin/python scripts/migrate-a2a-door-groups.py --apply --agent home-security-agent
    ./venv/bin/python scripts/migrate-a2a-door-groups.py --rollback --apply

What and why
------------
The authorizer's `customClaims` used to enumerate every `a2a-<agent>.<skill>` group.
`CONTAINS_ANY` has no wildcard, so adding a skill to a card left its grantees refused at
the door until someone redeployed that runtime — the single worst day-2 coupling between
an agent team and this platform. The enumeration bought no authorization to pay for it:
the door passed on ANY one of the groups, and the container derives the granted skill
subset from the same signed claim regardless (`common/server.enforce_allowed_skills`).

So the door becomes one group, `a2a-<cardName>`, constant for the agent's lifetime.

ORDER MATTERS, and reversing it locks every user out
----------------------------------------------------
    1. The GRANT side must emit `a2a-<agent>` first, and be backfilled.
         - per-user grants: `PUT /users/{id}/permissions?action=a2a-reconcile`
         - global grants: nothing to do; the pre-token trigger computes them per token
    2. Only then run this.

This script REFUSES to apply until step 1 is verifiably done: it checks that a
`a2a-<cardName>` group EXISTS in the pool for every agent it is about to flip. A group
with no holders is still a bug, but a group that does not exist at all means the
materialiser has not run and the flip would refuse everyone.

`--rollback` puts the per-skill enumeration back. Safe at any time, because granted users
hold both shapes — that is what makes this migration reversible rather than a one-way
door.

`update_agent_runtime` is a full PUT, so every field is read back and passed through
unchanged. Omitting one does not error; it CLEARS it. That is how a runtime loses its
env, its protocol or its header allowlist during an unrelated change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import boto3

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "shared"))

import a2a_conformance as conf  # noqa: E402
import a2a_groups  # noqa: E402
import agent_registry as registry_ns  # noqa: E402

REGION = "us-west-2"


def log(msg: str) -> None:
    print(f"  [door-migrate] {msg}", flush=True)


def _deployment() -> dict:
    outputs = json.loads((REPO / "cdk-outputs.json").read_text())
    stack = next(iter(outputs.values()))
    state = json.loads((REPO / "agentcore-state.json").read_text())
    return {
        "poolId": stack["UserPoolId"],
        "appClientId": stack["UserPoolClientId"],
        "registryId": state["registryId"],
        "discoveryUrl": (f"https://cognito-idp.{REGION}.amazonaws.com/"
                         f"{stack['UserPoolId']}/.well-known/openid-configuration"),
    }


def _records(dep: dict) -> list[dict]:
    """APPROVED AGENT records with their cards. The Registry is the roster."""
    client = registry_ns.registry_client(REGION)
    out, token = [], None
    while True:
        kwargs = {"registryId": dep["registryId"], "maxResults": 50,
                  "filters": [{"name": "recordType",
                               "values": [registry_ns.RECORD_TYPE_AGENT]}]}
        if token:
            kwargs["nextToken"] = token
        page = client.list_registry_records(**kwargs)
        for rec in page.get("registryRecords", []):
            if (rec.get("status") or "") != registry_ns.STATUS_APPROVED:
                continue
            detail = client.get_registry_record(
                registryId=dep["registryId"], recordId=rec["recordId"])
            raw = registry_ns.read_agent_card(detail)
            if not raw:
                continue
            out.append({"recordId": rec["recordId"], "card": json.loads(raw)})
        token = page.get("nextToken")
        if not token:
            return out


def _runtime_id(url: str, ac) -> tuple[str, str]:
    sys.path.insert(0, str(REPO / "cdk" / "lambda" / "admin-api"))
    import a2a_runtimes

    return a2a_runtimes.resolve(url, ac)


def _existing_door_groups(cognito, pool_id: str) -> set[str]:
    out, token = set(), None
    while True:
        kwargs = {"UserPoolId": pool_id, "Limit": 60}
        if token:
            kwargs["NextToken"] = token
        resp = cognito.list_groups(**kwargs)
        for g in resp.get("Groups", []):
            name = g.get("GroupName", "")
            if name.startswith(a2a_groups.GROUP_PREFIX) and \
                    a2a_groups.SEPARATOR not in name:
                out.add(name)
        token = resp.get("NextToken")
        if not token:
            return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write; default is report only")
    ap.add_argument("--rollback", action="store_true",
                    help="restore the per-skill enumeration")
    ap.add_argument("--agent", action="append", default=None,
                    help="limit to these card names (repeatable)")
    ap.add_argument("--force", action="store_true",
                    help="skip the 'door group exists' precondition. Only correct if "
                         "you have verified the claim another way")
    args = ap.parse_args(argv)

    dep = _deployment()
    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
    cognito = boto3.client("cognito-idp", region_name=REGION)

    records = _records(dep)
    if args.agent:
        wanted = set(args.agent)
        records = [r for r in records if (r["card"].get("name") or "") in wanted]
    if not records:
        sys.exit("no APPROVED AGENT records matched")

    present = _existing_door_groups(cognito, dep["poolId"])
    log(f"{len(records)} approved agent(s); {len(present)} door group(s) in the pool")

    # ---- precondition ---------------------------------------------------
    if not args.rollback:
        absent = []
        for rec in records:
            name = rec["card"].get("name") or ""
            try:
                door = a2a_groups.agent_group_name(name)
            except a2a_groups.GroupNameError as exc:
                sys.exit(f"{name!r}: {exc}")
            if door not in present:
                absent.append(door)
        if absent and not args.force:
            sys.exit(
                "REFUSING to flip: these door groups do not exist in the pool yet, so "
                "the flip would refuse every caller:\n  " + "\n  ".join(absent) +
                "\n\nRun the grant side first — for each user with per-user A2A intent:\n"
                "  PUT /users/{userId}/permissions?action=a2a-reconcile\n"
                "Global grants need nothing; the pre-token trigger computes them.\n"
                "Then re-run this. (--force overrides, only if you verified the claim "
                "another way.)")
        if absent:
            log(f"--force: proceeding with {len(absent)} door group(s) ABSENT")

    changed, already, failed = [], [], []
    for rec in records:
        card = rec["card"]
        name = card.get("name") or ""
        runtime_id, via = _runtime_id(card.get("url") or "", ac)
        if not runtime_id:
            log(f"{name}: SKIP — {via}")
            failed.append(f"{name}: {via}")
            continue

        skill_ids = [s.get("id") for s in (card.get("skills") or []) if s.get("id")]
        target = (a2a_groups.skill_groups_for_agent(name, skill_ids) if args.rollback
                  else a2a_groups.authorizer_groups(name))

        info = ac.get_agent_runtime(agentRuntimeId=runtime_id)
        jwt = ((info.get("authorizerConfiguration") or {})
               .get("customJWTAuthorizer") or {})
        claims = list(jwt.get("customClaims") or [])
        current = sorted(
            ((claims[0].get("authorizingClaimMatchValue") or {})
             .get("claimMatchValue") or {}).get("matchValueStringList") or []
        ) if claims else []

        if current == sorted(target):
            log(f"{name}: already correct ({current})")
            already.append(name)
            continue

        log(f"{name}: {current} -> {sorted(target)}   [{via}]")
        if not args.apply:
            changed.append(name)
            continue

        new_jwt = dict(jwt)
        new_jwt["discoveryUrl"] = dep["discoveryUrl"]
        new_jwt["allowedAudience"] = [dep["appClientId"]]
        new_jwt["customClaims"] = [{
            "inboundTokenClaimName": conf.CLAIM_NAME,
            "inboundTokenClaimValueType": conf.CLAIM_VALUE_TYPE,
            "authorizingClaimMatchValue": {
                "claimMatchValue": {"matchValueStringList": sorted(target)},
                "claimMatchOperator": conf.CLAIM_OPERATOR,
            },
        }]
        # A full PUT. Everything not named here is CLEARED, not left alone — which is
        # how a runtime silently loses its env vars, its A2A protocol setting or its
        # header allowlist during a change that had nothing to do with them.
        kwargs = dict(
            agentRuntimeId=runtime_id,
            agentRuntimeArtifact=info["agentRuntimeArtifact"],
            roleArn=info["roleArn"],
            networkConfiguration=info.get("networkConfiguration",
                                          {"networkMode": "PUBLIC"}),
            environmentVariables=dict(info.get("environmentVariables") or {}),
            protocolConfiguration=info.get("protocolConfiguration",
                                           {"serverProtocol": "A2A"}),
            authorizerConfiguration={"customJWTAuthorizer": new_jwt},
        )
        header_cfg = info.get("requestHeaderConfiguration")
        if header_cfg:
            kwargs["requestHeaderConfiguration"] = header_cfg
        else:
            # Absent means the edge is dropping Authorization before the container —
            # the request arrives looking like one that sent no bearer at all.
            log(f"{name}: WARNING no requestHeaderConfiguration on this runtime; "
                f"leaving it absent rather than inventing one, but the container "
                f"cannot see Authorization. Run a2a-agent-registry/deploy.py.")
        try:
            ac.update_agent_runtime(**kwargs)
            changed.append(name)
        except Exception as exc:  # noqa: BLE001
            log(f"{name}: FAILED — {exc}")
            failed.append(f"{name}: {exc}")

    print()
    verb = "would change" if not args.apply else "changed"
    print(f"{verb}: {len(changed)}   already correct: {len(already)}   "
          f"failed: {len(failed)}")
    if failed:
        print("\nfailures:")
        for f in failed:
            print(f"  {f}")
    if args.apply and changed:
        print("\nNext: confirm a delegated turn still works end to end, and that the "
              "conformance route now reports clean rather than "
              "`claim-skill-groups-only`:")
        print("  GET /registry/records?action=a2a-conformance")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
