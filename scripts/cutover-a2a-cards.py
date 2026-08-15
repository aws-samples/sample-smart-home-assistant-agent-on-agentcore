#!/usr/bin/env python3
"""Point the A2A AgentCards at the gateway, or back at the runtimes.

    ./venv/bin/python scripts/cutover-a2a-cards.py --to gateway --dry-run
    ./venv/bin/python scripts/cutover-a2a-cards.py --to gateway
    ./venv/bin/python scripts/cutover-a2a-cards.py --to runtime    # rollback

The orchestrator routes on `card.url` and nothing else (`tools/a2a.py` uses the URL
it was GRANTED, never one a sub-agent reports about itself). So this one field is
the whole cutover, and the whole rollback.

Bidirectional on purpose, instead of an `A2A_ENDPOINT_OVERRIDE` in the orchestrator.
The design sketched that env var as the fast way back, but it would add a permanent
second routing path to the request-time code — maintained forever, for a rollback
this script already does in about a minute. Two ways to decide an endpoint is one
too many when one of them only exists for emergencies.

The outage window, stated plainly
---------------------------------
`UpdateRegistryRecord` on an APPROVED record resets it to DRAFT, and the
orchestrator registers `a2a_*` tools only for APPROVED records. So each agent stops
being delegable between its update and its re-approval. This script re-approves
immediately, making that seconds rather than however long a human takes — but it is
not zero, and a run that dies halfway leaves the records it already touched in
DRAFT. Re-running is safe and finishes the job.

Grants are NOT affected, because §2's revocation sweep gives an in-flight record a
re-approval window (`A2A_GRANT_GRACE_SECONDS`, default 1h) before it pulls groups.
Without that window this script would revoke every user's grants on every run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import boto3

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "shared"))

import agent_registry as registry_ns  # noqa: E402

REGION = "us-west-2"
GATEWAY_NAME = "smarthome-a2a-gw"
DEPLOYED_STATE = REPO / "a2a-agent-registry" / "deployed-state.json"


def log(msg: str) -> None:
    print(f"  [cutover] {msg}", flush=True)


def gateway_url() -> str:
    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
    for page in ac.get_paginator("list_gateways").paginate():
        for g in page.get("items", []):
            if g.get("name") == GATEWAY_NAME:
                return ac.get_gateway(
                    gatewayIdentifier=g["gatewayId"])["gatewayUrl"].rstrip("/")
    sys.exit(f"gateway {GATEWAY_NAME} not found — run scripts/setup-a2a-gateway.py")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", choices=("gateway", "runtime"), required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--agent", action="append", default=[],
                    help="limit to these agents (repeatable)")
    args = ap.parse_args(argv)

    state = json.loads(DEPLOYED_STATE.read_text())
    agents = state.get("agents") or []
    if args.agent:
        agents = [a for a in agents if a["agent"] in args.agent]
    if not agents:
        sys.exit("no matching agents in deployed-state.json")

    registry_id = json.loads((REPO / "agentcore-state.json").read_text())["registryId"]
    client = registry_ns.registry_client(REGION)
    gw = gateway_url() if args.to == "gateway" else ""

    # Records are looked up by CARD NAME, not by the recordId in deployed-state.json.
    # That file's ids go stale whenever a record is recreated — which is exactly what
    # happens when one gets deprecated, since DEPRECATED is terminal and the record
    # then disappears. The registry is the source of truth for which record is live.
    live = {}
    token = None
    while True:
        kwargs = {"registryId": registry_id, "maxResults": 50,
                  "filters": [{"name": "recordType",
                               "values": [registry_ns.RECORD_TYPE_AGENT]}]}
        if token:
            kwargs["nextToken"] = token
        resp = client.list_registry_records(**kwargs)
        for r in resp.get("registryRecords", []):
            live[r.get("name", "")] = r
        token = resp.get("nextToken")
        if not token:
            break

    changed, skipped, failed = [], [], []
    for entry in agents:
        agent = entry["agent"]
        target = f"{gw}/{agent}" if args.to == "gateway" else entry["invocationUrl"]

        record = None
        for name, r in live.items():
            detail = client.get_registry_record(
                registryId=registry_id, recordId=r["recordId"])
            raw = registry_ns.read_agent_card(detail)
            if not raw:
                continue
            card = json.loads(raw)
            # The card's own `name` is the agent's long name; match on the slug the
            # deployed state carries so this works regardless of naming style.
            if agent.replace("-", "") in card.get("name", "").replace("-", ""):
                record = (r, card, detail)
                break
        if record is None:
            log(f"{agent}: no live record found — skipped")
            skipped.append(agent)
            continue

        r, card, detail = record
        if card.get("url") == target:
            log(f"{agent}: already points at {args.to}")
            skipped.append(agent)
            continue

        log(f"{agent}: {card.get('url', '')[:48]}… -> {target[:60]}")
        if args.dry_run:
            changed.append(agent)
            continue

        card["url"] = target
        try:
            # `as_update_descriptors`, not a hand-rolled `{"optionalValue": ...}`.
            # Update wraps EVERY level — the union, each descriptor AND each field —
            # and wrapping only the outer one is a mistake that looks right and fails
            # client-side parameter validation.
            client.update_registry_record(
                registryId=registry_id, recordId=r["recordId"],
                descriptors=registry_ns.as_update_descriptors(
                    registry_ns.agent_record_descriptors(card)))
            status = registry_ns.approve_record(
                client, registry_id, r["recordId"],
                reason=f"A2A endpoint repointed to the {args.to}")
            log(f"{agent}: {status}")
            changed.append(agent)
        except Exception as exc:  # noqa: BLE001
            log(f"{agent}: FAILED — {exc}")
            failed.append(agent)

    print()
    print(f"changed: {changed}")
    print(f"skipped: {skipped}")
    if failed:
        print(f"FAILED : {failed}")
        print("Re-run to finish; anything left in DRAFT is not delegable until it is")
        print("APPROVED, and its grants survive for the re-approval window only.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
