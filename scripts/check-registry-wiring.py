#!/usr/bin/env python3
"""Verify every REGISTRY_ID consumer points at a registry that actually has records.

Why this exists
---------------
Four things read a registry id — the admin Lambda, the Skill ERP Lambda, the
orchestrator runtime, and `agentcore-state.json` — and nothing compared them. When
they disagree, the symptom appears somewhere else entirely: Admin Console →
Integration Registry shows an empty list of grantable A2A agents, which reads as
"nobody has published anything yet" rather than "this Lambda is looking at the
wrong registry".

That ambiguity is not hypothetical. On 2026-08-10 an empty catalog was diagnosed as
two separate causes — a stale bundled botocore rejecting the GA `filters` shape, and
a missing `agent-registry:ListRegistryRecords` grant. Both were wrong. The admin
Lambda had been hand-patched to an id from the OLD `bedrock-agentcore` namespace,
which holds a DIFFERENT set of registries than the GA `agent-registry` namespace:

    aws bedrock-agentcore-control get-registry --registry-id Zuy3YNKrPQ5uwE9t
        -> ResourceNotFoundException          # the GA registry, queried as legacy
    aws agent-registry-control     get-registry --registry-id Zuy3YNKrPQ5uwE9t
        -> READY, 8 agent records

So a `GetRegistry` that 404s proves nothing on its own — it also 404s when the id is
right and the namespace is wrong. Both must be named together, which is what this
script does.

Usage
-----
    ./venv/bin/python scripts/check-registry-wiring.py

Exits non-zero when any consumer disagrees with `agentcore-state.json`, when the
registry is not READY, or when it holds no APPROVED AGENT records. Read-only: it
reports, it does not repair. The fix is to re-run `scripts/setup-agentcore.py`,
which patches both Lambdas from the state file.

Note REGISTRY_ID is read at MODULE IMPORT in the admin Lambda, so a corrected env
var does not take effect on a warm container. Force a cold start (any
configuration change does it, e.g. `--description`) or the old value survives.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
AGENTCORE_STATE = ROOT / "agentcore-state.json"

# The GA namespace. The old `bedrock-agentcore-control` client cannot see GA
# registries at all, which is the trap this script exists to make legible.
REGISTRY_CLIENT = "agent-registry-control"
LEGACY_CLIENT = "bedrock-agentcore-control"

LAMBDAS = ("smarthome-admin-api", "smarthome-skill-erp-api")


def main() -> int:
    if not AGENTCORE_STATE.exists():
        sys.exit(f"{AGENTCORE_STATE} not found — deploy first.")
    state = json.loads(AGENTCORE_STATE.read_text())
    region = state.get("region") or os.environ.get("AWS_REGION", "us-west-2")
    expected = state.get("registryId", "")
    if not expected:
        sys.exit("no registryId in agentcore-state.json")

    print(f"expected registryId (agentcore-state.json): {expected}")

    # -- who agrees? --------------------------------------------------------
    found: dict[str, str] = {}
    lam = boto3.client("lambda", region_name=region)
    for fn in LAMBDAS:
        try:
            cfg = lam.get_function_configuration(FunctionName=fn)
            found[fn] = (cfg.get("Environment", {}).get("Variables", {})
                         .get("REGISTRY_ID", ""))
        except Exception as e:  # noqa: BLE001
            found[fn] = f"<error: {e}>"

    runtime_id = state.get("runtimeId", "")
    if runtime_id:
        try:
            ac = boto3.client(LEGACY_CLIENT, region_name=region)
            rt = ac.get_agent_runtime(agentRuntimeId=runtime_id)
            found["orchestrator runtime"] = (
                (rt.get("environmentVariables") or {}).get("REGISTRY_ID", ""))
        except Exception as e:  # noqa: BLE001
            found["orchestrator runtime"] = f"<error: {e}>"

    problems: list[str] = []
    for who, got in found.items():
        if got == expected:
            print(f"  OK   {who}: {got}")
        elif not got:
            print(f"  MISS {who}: REGISTRY_ID is unset")
            problems.append(f"{who} has no REGISTRY_ID (a bare `cdk deploy` "
                            f"resets it; re-run scripts/setup-agentcore.py)")
        else:
            print(f"  DRIFT {who}: {got}")
            problems.append(f"{who} points at {got}, not {expected}")

    # -- is the registry real, and does it hold anything? -------------------
    reg = boto3.client(REGISTRY_CLIENT, region_name=region)
    try:
        info = reg.get_registry(registryId=expected)
        status = info.get("status", "")
        print(f"registry {expected}: status={status}")
        if status != "READY":
            problems.append(f"registry {expected} is {status}, not READY")
    except Exception as e:  # noqa: BLE001
        # Name the namespace explicitly. A 404 here is equally consistent with a
        # dead id and with a live id from the other namespace, and guessing
        # between them is what caused the original misdiagnosis.
        print(f"registry {expected}: NOT FOUND in the {REGISTRY_CLIENT} namespace "
              f"({e})")
        legacy_hit = False
        try:
            boto3.client(LEGACY_CLIENT, region_name=region).get_registry(
                registryId=expected)
            legacy_hit = True
        except Exception:  # noqa: BLE001
            pass
        if legacy_hit:
            problems.append(
                f"registry {expected} exists only in the legacy {LEGACY_CLIENT} "
                f"namespace, which stops serving Registry on 2026-09-17 — "
                f"re-provision it in the GA namespace")
        else:
            problems.append(f"registry {expected} does not exist in either namespace")
        _report(problems)
        return 1

    approved = 0
    total = 0
    try:
        token = None
        while True:
            kwargs = {"registryId": expected, "maxResults": 50}
            if token:
                kwargs["nextToken"] = token
            resp = reg.list_registry_records(**kwargs)
            for r in resp.get("registryRecords", []):
                total += 1
                if r.get("recordType") == "AGENT" and r.get("status") == "APPROVED":
                    approved += 1
            token = resp.get("nextToken")
            if not token:
                break
    except Exception as e:  # noqa: BLE001
        print(f"  ListRegistryRecords failed: {e}")
        problems.append(f"cannot list records in {expected}: {e}")
        _report(problems)
        return 1

    print(f"records: {total} total, {approved} APPROVED AGENT")
    if approved == 0:
        # Not fatal on its own — a fresh registry legitimately has none — but it is
        # the exact state that looks like a working system with nothing to show.
        problems.append(
            "no APPROVED AGENT records: Admin Console will show an empty A2A "
            "catalog. Run a2a-agent-registry/deploy.py, then approve the records.")

    _report(problems)
    return 1 if problems else 0


def _report(problems: list[str]) -> None:
    print()
    if not problems:
        print("Registry wiring OK.")
        return
    print(f"{len(problems)} problem(s):")
    for p in problems:
        print(f"  - {p}")
    print("\nUsual fix: ./venv/bin/python scripts/setup-agentcore.py "
          "(patches both Lambdas from agentcore-state.json).")
    print("A corrected env var needs a COLD START to be picked up: REGISTRY_ID is "
          "read at module import.")


if __name__ == "__main__":
    sys.exit(main())
