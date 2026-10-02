#!/usr/bin/env python3
"""Move every runtime this project owns onto AgentCore Runtime platform V2 (or back).

    ./venv/bin/python scripts/set-platform-version.py                      # report only
    ./venv/bin/python scripts/set-platform-version.py --apply              # all -> V2
    ./venv/bin/python scripts/set-platform-version.py --apply --only sha2aqa_sha2aqa
    ./venv/bin/python scripts/set-platform-version.py --apply --to V1      # rollback

What V2 is
----------
`platformVersion` is a field on the runtime itself, separate from the runtime
versions that record config history. V2 prepares the container once, snapshots it on
the first healthy `/ping`, and restores every new session's microVM from that
snapshot instead of re-running the import. Cold start stops depending on how heavy
the import is. It also changes what import-time state means: whatever the module
computes before `app.run()` is shared by every session for the life of that runtime
version (see docs/architecture-and-design.md, "Platform V2").

Why a script and not `agentcore.json`
-------------------------------------
Neither CloudFormation nor CDK can set `platformVersion` yet, and `agentcore deploy`
goes through CloudFormation, so a runtime the CLI creates is always V1. An update that
omits the field keeps the runtime's current platform, so after this has run once,
`agentcore deploy`, `restore-text-runtime-config.py` and `a2a-agent-registry/deploy.py`
all leave V2 alone. A fresh provision does not, so run this again after one.

`update_agent_runtime` is a full PUT
------------------------------------
A field left out is CLEARED, not kept. That is how a runtime loses its env, its header
allowlist or its /mnt/workspace mount during a change that had nothing to do with
them. So this does not list fields by hand: it passes back every field
`GetAgentRuntime` returned that `UpdateAgentRuntime` accepts, reading that list from
the SDK's own service model, and then checks that each one reads back unchanged.

A V2 update takes minutes, not seconds, because the snapshot is prepared before the
runtime goes READY. The runtimes are moved one at a time and each is waited on, so a
failure stops on the runtime that caused it.
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
BUNDLES_RUNTIME_NAME = "smarthome_bundles"

# V2 caps the environment at 1.5 KB for CodeZip and 2.5 KB for containers (V1: 4 KB).
# Measured here as the JSON encoding, which over-counts slightly, so a runtime under
# this is under the real limit. The API rejects an oversize env with a
# ValidationException regardless; this is only to say so before the call.
V2_ENV_LIMIT_BYTES = {"codeConfiguration": 1536, "containerConfiguration": 2560}

# Get-only fields that UpdateAgentRuntime also accepts but must not be echoed.
NOT_PASSED_THROUGH = {"agentRuntimeId", "platformVersion", "clientToken"}

POLL_SECONDS = 10
READY_TIMEOUT_SECONDS = 20 * 60


def log(msg: str) -> None:
    print(f"  [platform] {msg}", flush=True)


def _roster(ac) -> list[tuple[str, str]]:
    """(label, runtimeId) for every runtime this deployment owns, from its state files."""
    state = json.loads((REPO / "agentcore-state.json").read_text())
    out = [("text", state["runtimeId"])]
    if state.get("voiceRuntimeId"):
        out.append(("voice", state["voiceRuntimeId"]))

    # The A/B bundles runtime is recorded nowhere but config.js, so find it by name.
    for page in ac.get_paginator("list_agent_runtimes").paginate():
        for rt in page.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == BUNDLES_RUNTIME_NAME:
                out.append(("bundles", rt["agentRuntimeId"]))

    a2a_state = REPO / "a2a-agent-registry" / "deployed-state.json"
    if a2a_state.exists():
        for agent in json.loads(a2a_state.read_text()).get("agents", []):
            if agent.get("runtimeId"):
                out.append((f"a2a:{agent['agent']}", agent["runtimeId"]))
    return out


def _wait_terminal(ac, runtime_id: str) -> dict:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while True:
        info = ac.get_agent_runtime(agentRuntimeId=runtime_id)
        status = info.get("status", "")
        if status == "READY" or status.endswith("FAILED"):
            return info
        if time.monotonic() > deadline:
            raise TimeoutError(f"{runtime_id} still {status} after "
                               f"{READY_TIMEOUT_SECONDS // 60} min")
        time.sleep(POLL_SECONDS)


def _pass_through_fields(ac) -> list[str]:
    members = ac.meta.service_model.operation_model("UpdateAgentRuntime").input_shape.members
    return [m for m in members if m not in NOT_PASSED_THROUGH]


def _env_bytes(info: dict) -> tuple[int, int | None]:
    env = info.get("environmentVariables") or {}
    kind = next(iter(info.get("agentRuntimeArtifact") or {}), "")
    return len(json.dumps(env, separators=(",", ":"))), V2_ENV_LIMIT_BYTES.get(kind)


def _endpoints(ac, runtime_id: str) -> str:
    eps = ac.list_agent_runtime_endpoints(agentRuntimeId=runtime_id).get("runtimeEndpoints", [])
    return ", ".join(f"{e['name']}=v{e.get('liveVersion')}" for e in eps)


def _move(ac, label: str, runtime_id: str, target: str, fields: list[str],
          apply: bool) -> str:
    """Returns 'already', 'would-change', 'changed' or raises."""
    info = _wait_terminal(ac, runtime_id)
    current = info.get("platformVersion") or "V1"
    size, limit = _env_bytes(info)
    head = f"{label:<24} {runtime_id:<44} {current} -> {target}"

    if current == target:
        log(f"{head}  already")
        return "already"
    if target == "V2" and limit and size > limit:
        raise ValueError(f"{label}: env is {size} B, over the V2 limit of {limit} B")

    log(f"{head}  env {size}/{limit} B  endpoints [{_endpoints(ac, runtime_id)}]")
    if not apply:
        return "would-change"

    kwargs = {f: info[f] for f in fields if f in info}
    ac.update_agent_runtime(agentRuntimeId=runtime_id, platformVersion=target, **kwargs)
    started = time.monotonic()
    after = _wait_terminal(ac, runtime_id)
    took = int(time.monotonic() - started)
    if after["status"] != "READY":
        raise RuntimeError(f"{label}: {after['status']} after {took}s — "
                           f"{after.get('failureReason', 'no failureReason')}")

    drift = [f for f in kwargs if after.get(f) != kwargs[f]]
    if (after.get("platformVersion") or "V1") != target:
        drift.append(f"platformVersion={after.get('platformVersion')}")
    if drift:
        raise RuntimeError(f"{label}: READY but did not read back unchanged: {drift}")

    log(f"{label:<24} READY on v{after['agentRuntimeVersion']} in {took}s, "
        f"{len(kwargs)} fields carried over  endpoints [{_endpoints(ac, runtime_id)}]")
    return "changed"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="make the change (default: report)")
    ap.add_argument("--to", default="V2", choices=["V1", "V2"])
    ap.add_argument("--only", action="append", default=[],
                    help="runtime id, runtime name or label (repeatable)")
    args = ap.parse_args()

    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
    fields = _pass_through_fields(ac)
    roster = _roster(ac)
    if args.only:
        roster = [(label, rid) for label, rid in roster
                  if any(o in (label, rid, rid.rsplit("-", 1)[0]) for o in args.only)]
        if not roster:
            sys.exit(f"--only matched nothing: {args.only}")

    counts: dict[str, int] = {}
    for label, runtime_id in roster:
        try:
            outcome = _move(ac, label, runtime_id, args.to, fields, args.apply)
        except Exception as exc:  # noqa: BLE001
            log(f"{label}: FAILED — {exc}")
            print("\nStopped. Runtimes after this one were not touched.")
            return 1
        counts[outcome] = counts.get(outcome, 0) + 1

    print("\n" + "   ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if not args.apply and counts.get("would-change"):
        print("Report only. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
