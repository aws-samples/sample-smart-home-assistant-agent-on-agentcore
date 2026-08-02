#!/usr/bin/env python3
"""Generate real agent traffic from simulated end users.

Creates a handful of test users, gives each a distinct profile (model, tenant
mode, scenario mix), then drives real multi-turn conversations through the same
SigV4 /invocations path the chatbot uses. The resulting spans, token counts,
sessions and evaluation scores are indistinguishable from real traffic, which
is what the Overview ops dashboard and AgentCore Evaluations consume.

Usage:
    export SIM_USER_PASSWORD='SomeStrong#Pass1'

    python3 scripts/simulate-users.py setup
    python3 scripts/simulate-users.py run
    python3 scripts/simulate-users.py run --heavy --personas dave,erin
    python3 scripts/simulate-users.py status
    python3 scripts/simulate-users.py teardown --yes

Everything is scoped to the `simuser+` email prefix; the real users in the pool
are never touched.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from sim import personas as P                      # noqa: E402
from sim.agent_client import AgentUser             # noqa: E402
from sim.provisioning import Provisioner, load_config, SIM_PREFIX  # noqa: E402

RESULTS_DIR = os.path.join(HERE, "sim-results")


def _password() -> str:
    pw = os.environ.get("SIM_USER_PASSWORD", "")
    if not pw:
        raise SystemExit(
            "SIM_USER_PASSWORD is not set.\n"
            "  export SIM_USER_PASSWORD='SomeStrong#Pass1'\n"
            "(Must satisfy the Cognito password policy: upper, lower, digit, symbol.)"
        )
    return pw


def _provisioner(cfg: dict) -> Provisioner:
    return Provisioner(cfg, cfg["adminUsername"], cfg["adminPassword"], _password())


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------

def cmd_setup(args) -> int:
    cfg = load_config(REPO)
    prov = _provisioner(cfg)
    selected = P.select(args.personas)
    no_grant = {k.strip() for k in (args.no_grant or "").split(",") if k.strip()}

    print(f"Setting up {len(selected)} simulated user(s) in {cfg['userPoolId']}\n")
    granted_any = False
    for p in selected:
        sub, created = prov.ensure_user(p.email)
        state = "created" if created else "reused"
        print(f"  {p.email:34s} {state:8s} sub={sub[:18]}...")

        if p.grant_tools and p.key not in no_grant:
            st = prov.grant_tools(sub, p.tools)
            print(f"      tools   {p.tools} -> HTTP {st}")
            granted_any = True
        else:
            # Deliberately ungranted: produces real "tool unavailable" data.
            prov.grant_tools(sub, [])
            print("      tools   (none — deliberately ungranted)")

        if p.model_id:
            print(f"      model   {p.model_id} -> HTTP {prov.set_model(p.email, p.model_id)}")
        if p.tenant_env and p.tenant_env != "default":
            print(f"      tenant  {p.tenant_env} -> HTTP {prov.set_tenant_env(p.email, p.tenant_env)}")

    if granted_any:
        # The permissions API returns 200 even when the Cedar attach fails, and
        # a failed attach means the Gateway serves that user zero tools. Waiting
        # here is what keeps `run` from producing misleading refusal data.
        print("\nWaiting for Cedar tool policies to become ACTIVE "
              "(grants return 200 before they actually apply)...")
        ok, states = prov.wait_for_policies_active()
        for name, st in sorted(states.items()):
            print(f"  {name:36s} {st}")
        if not ok:
            print("\nPolicies did NOT all reach ACTIVE. Tool-dependent scenarios "
                  "will produce refusals rather than real tool calls.\n"
                  "If a policy shows UPDATE_FAILED with 'Insufficient permissions "
                  "to call gateway', the admin Lambda role is missing\n"
                  "bedrock-agentcore:InvokeGateway.")
            return 1
        print("\nAll tool policies ACTIVE.")

    print("\nSetup complete. Next: python3 scripts/simulate-users.py run")
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def _run_persona(p: P.Persona, cfg: dict, password: str, heavy: bool,
                 rounds: int) -> dict:
    """Drive one persona's scenarios serially (so context accumulates).

    Personas run concurrently with each other; turns within a persona must stay
    sequential because a conversation is inherently ordered.
    """
    user = AgentUser(p.email, password, cfg)
    scenarios = P.scenarios_for(p, heavy)
    turns: list = []
    try:
        user.login()
    except Exception as e:  # noqa: BLE001
        return {"persona": p.key, "error": f"login failed: {type(e).__name__}: {e}",
                "turns": []}

    for _ in range(rounds):
        for sc in scenarios:
            # One fresh session per scenario: distinct conversations, and it
            # keeps memory build-up scoped to the scenario that needs it.
            user.start_session()
            for prompt in sc.prompts:
                t = user.say(prompt, scenario=sc.name)
                turns.append(t)
                status = "ok" if t.status == 200 and not t.error else f"ERR {t.status}"
                flag = " [refused]" if t.looks_refused else ""
                print(f"  [{p.key}] {sc.name:22s} {t.elapsed_s:6.1f}s {status}{flag}")
                if t.status != 200:
                    # One retry — cold starts and transient 5xx are common.
                    time.sleep(3)
                    t2 = user.say(prompt, scenario=sc.name + "-retry")
                    turns.append(t2)
                    print(f"  [{p.key}] {sc.name:22s} {t2.elapsed_s:6.1f}s retry "
                          f"{'ok' if t2.status == 200 else 'ERR ' + str(t2.status)}")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"{p.key}.jsonl")
    stamp = datetime.now(timezone.utc).isoformat()
    with open(path, "a") as f:
        for t in turns:
            row = t.to_json()
            row["persona"] = p.key
            row["model"] = p.model_id or "(global default)"
            row["tenantEnv"] = p.tenant_env
            row["runAt"] = stamp
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return {"persona": p.key, "turns": turns, "path": path}


def cmd_run(args) -> int:
    cfg = load_config(REPO)
    password = _password()
    selected = P.select(args.personas)
    tier = "light + heavy" if args.heavy else "light only"

    print(f"Running {len(selected)} persona(s), {tier}, {args.rounds} round(s)")
    print(f"Runtime: {cfg['agentRuntimeArn'].split('/')[-1]}\n")

    t0 = time.time()
    results = []
    # Personas in parallel; each persona's own turns stay sequential.
    with ThreadPoolExecutor(max_workers=min(5, len(selected))) as ex:
        futs = {ex.submit(_run_persona, p, cfg, password, args.heavy, args.rounds): p
                for p in selected}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001 — one persona must not kill the run
                results.append({"persona": futs[fut].key,
                                "error": f"{type(e).__name__}: {e}", "turns": []})

    print(f"\n{'persona':10s} {'turns':>6s} {'ok':>4s} {'err':>4s} {'refused':>8s} "
          f"{'avg s':>7s} {'max s':>7s}")
    print("-" * 56)
    total = ok_total = err_total = 0
    for r in sorted(results, key=lambda x: x["persona"]):
        if r.get("error") and not r["turns"]:
            print(f"{r['persona']:10s} {'-':>6s}  {r['error'][:40]}")
            continue
        ts = r["turns"]
        ok = sum(1 for t in ts if t.status == 200 and not t.error)
        err = len(ts) - ok
        ref = sum(1 for t in ts if t.looks_refused)
        avg = sum(t.elapsed_s for t in ts) / len(ts) if ts else 0
        mx = max((t.elapsed_s for t in ts), default=0)
        print(f"{r['persona']:10s} {len(ts):6d} {ok:4d} {err:4d} {ref:8d} "
              f"{avg:7.1f} {mx:7.1f}")
        total += len(ts); ok_total += ok; err_total += err
    print("-" * 56)
    print(f"{'TOTAL':10s} {total:6d} {ok_total:4d} {err_total:4d}")
    print(f"\nElapsed {time.time() - t0:.0f}s. Results: {RESULTS_DIR}/*.jsonl")
    print("Telemetry takes a few minutes to surface in CloudWatch; the dashboard "
          "caches for 5 min (use Refresh to force).")
    return 0 if err_total == 0 else 1


# ---------------------------------------------------------------------------
# status / teardown
# ---------------------------------------------------------------------------

def cmd_status(args) -> int:
    cfg = load_config(REPO)
    prov = _provisioner(cfg)
    users = prov.list_sim_users()
    if not users:
        print(f"No simulated users (prefix {SIM_PREFIX}). Run `setup` first.")
        return 0

    print(f"{len(users)} simulated user(s):\n")
    print(f"{'email':34s} {'tenant':12s} {'tools':6s} {'model'}")
    print("-" * 96)
    for u in users:
        d = prov.describe(u["email"])
        print(f"{d['email']:34s} {d.get('tenantEnv','?'):12s} "
              f"{len(d.get('tools') or []):6d} {d.get('model','?')}")

    print("\nCedar tool policies:")
    for name, st in sorted(prov.policy_states().items()):
        mark = "" if st == "ACTIVE" else "   <-- not ACTIVE"
        print(f"  {name:36s} {st}{mark}")

    for p in P.PERSONAS:
        path = os.path.join(RESULTS_DIR, f"{p.key}.jsonl")
        if os.path.exists(path):
            with open(path) as f:
                n = sum(1 for _ in f)
            print(f"  results/{p.key}.jsonl  {n} logged turn(s)")
    return 0


def cmd_teardown(args) -> int:
    cfg = load_config(REPO)
    prov = _provisioner(cfg)
    users = prov.list_sim_users()
    if not users:
        print(f"No simulated users (prefix {SIM_PREFIX}) to remove.")
        return 0

    print(f"About to delete {len(users)} user(s) with prefix {SIM_PREFIX}:")
    for u in users:
        print(f"  {u['email']}")
    if not args.yes:
        if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    for u in users:
        print(f"  {u['email']:34s} {prov.remove_user(u['email'])}")
    print("\nDone. Real users are untouched.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Simulate real end users against the deployed agent.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="create + configure simulated users")
    s.add_argument("--personas", help="comma-separated subset (default: all)")
    s.add_argument("--no-grant", help="personas to leave without tool grants")
    s.set_defaults(func=cmd_setup)

    r = sub.add_parser("run", help="drive conversations")
    r.add_argument("--personas", help="comma-separated subset (default: all)")
    r.add_argument("--heavy", action="store_true",
                   help="include browser-use + code-interpreter (30-60s/turn)")
    r.add_argument("--rounds", type=int, default=1,
                   help="repeat the scenario set N times (default 1)")
    r.set_defaults(func=cmd_run)

    st = sub.add_parser("status", help="show simulated users and policy state")
    st.set_defaults(func=cmd_status)

    td = sub.add_parser("teardown", help="delete simulated users only")
    td.add_argument("--yes", action="store_true", help="skip confirmation")
    td.set_defaults(func=cmd_teardown)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
