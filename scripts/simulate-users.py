#!/usr/bin/env python3
"""Generate real agent traffic from simulated end users.

Creates a handful of test users, gives each a distinct profile (model, tenant
mode, scenario mix), then drives real multi-turn conversations through the same
SigV4 /invocations path the chatbot uses. The resulting spans, token counts,
sessions and evaluation scores are indistinguishable from real traffic, which
is what the Overview ops dashboard and AgentCore Evaluations consume.

Run this before a customer demo: the dashboard only reflects real traffic, so a
freshly deployed (or idle) environment shows an empty wall until someone talks
to the agent.

Usage (works from the repo root or from scripts/):
    export SIM_USER_PASSWORD='SomeStrong#Pass1'

    python3 scripts/simulate-users.py setup
    python3 scripts/simulate-users.py run
    python3 scripts/simulate-users.py run --heavy --personas dave,erin
    python3 scripts/simulate-users.py status
    python3 scripts/simulate-users.py teardown --yes

Docs:
    docs/admin_manual_管理员使用手册.md  §10.3 — pre-demo runbook (start here)
    scripts/sim/README.md                     — personas, coverage, gotchas
    docs/architecture-and-design.md           §9.16 — design rationale

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
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from sim import personas as P                      # noqa: E402
from sim.agent_client import AgentUser             # noqa: E402
from sim.provisioning import Provisioner, load_config, SIM_PREFIX  # noqa: E402

RESULTS_DIR = os.path.join(HERE, "sim-results")


def _password() -> str:
    """Read the shared password for the simulated users.

    It lives in an env var rather than the repo so a real credential is never
    committed. The error below is the first thing most people see, so it also
    points at the docs — otherwise the reader has no way to know a runbook
    exists two directories away.
    """
    pw = os.environ.get("SIM_USER_PASSWORD", "")
    if not pw:
        raise SystemExit(
            "SIM_USER_PASSWORD is not set — the simulated users all share this password.\n"
            "\n"
            "  export SIM_USER_PASSWORD='SomeStrong#Pass1'\n"
            "\n"
            "Any value works as long as it satisfies the Cognito password policy:\n"
            "at least 8 characters with an uppercase letter, a lowercase letter,\n"
            "a digit and a symbol. It is read from the environment (never the repo)\n"
            "so no real credential is committed.\n"
            "\n"
            "Then:\n"
            "  python3 simulate-users.py setup   # create + configure 5 personas\n"
            "  python3 simulate-users.py run     # generate conversations (~2 min)\n"
            "\n"
            "Full runbook:  docs/admin_manual_管理员使用手册.md  section 10.3\n"
            "Implementation: scripts/sim/README.md\n"
            "Run `python3 simulate-users.py --help` for all commands and flags."
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


# Reasons attached to a simulated 👎. Written as things a real user would say,
# because they are rendered verbatim in the console's "recent comments" list and
# a placeholder there would look like a bug in the feature.
SIM_DOWN_REASONS = [
    "Took too long to answer.",
    "It described what it would do but nothing actually changed.",
    "Not the room I meant.",
    "Answer was vaguer than I wanted.",
    "It asked me to clarify something I had already said.",
]


def _vote_on_turns(results: list, cfg: dict, days_back: int) -> tuple[int, int]:
    """File one 👍/👎 per successful turn, through the real feedback API.

    Runs after the conversations rather than during them: a vote references a
    turn, and the turn has to exist first. Failures are counted and reported but
    never fatal — a demo with traffic and no votes is still a usable demo,
    whereas aborting the run would throw away the conversations too.

    `days_back` spreads the votes over the past N days so the 60d/90d dashboard
    views are not one column on today. It moves only the rows we write.
    """
    prov = _provisioner(cfg)
    filed = failed = 0
    for r in results:
        persona = P.PERSONA_BY_KEY.get(r["persona"])
        if not persona:
            continue
        good = [t for t in r.get("turns", []) if t.status == 200 and not t.error]
        acc = 0.0  # carries the fractional negative across turns
        for i, t in enumerate(good):
            # Deterministic rather than random: a fixed sequence keeps two runs
            # comparable, and random noise in demo data makes a dashboard
            # screenshot impossible to reproduce.
            #
            # Negatives are spread EVENLY through the sequence (a Bresenham-style
            # accumulator), not grouped at the end of a fixed window. Two earlier
            # attempts both failed silently:
            #   - `i % 100 < rate*100`: a persona has 6-8 turns, so `i` never
            #     reached the threshold and EVERY vote was positive. The run
            #     reported "29 filed, 0 failed" and the card showed 5.0/5.
            #   - `i % 10`: puts all the negative slots at positions 7-9, which a
            #     persona with 6 turns never reaches.
            # `scripts/sim/tests/test_vote_distribution.py` asserts each persona's
            # own turn count yields a mixed result, so a third variant of this
            # mistake fails a test instead of a demo.
            acc += (1.0 - persona.feedback_up_rate)
            up = acc < 1.0
            if not up:
                acc -= 1.0
            vote = "up" if up else "down"
            reason = "" if up else SIM_DOWN_REASONS[i % len(SIM_DOWN_REASONS)]
            ts = ""
            if days_back > 0:
                offset = timedelta(days=(i % days_back),
                                   hours=(i * 7) % 24, minutes=(i * 13) % 60)
                ts = (datetime.now(timezone.utc) - offset).isoformat()
            status = prov.submit_feedback(
                persona.email, vote, turn_id=f"{r['persona']}-{i}-{t.scenario}",
                session_id=t.session_id, reason=reason,
                turn_prompt=t.prompt, ts=ts,
            )
            if status == 200:
                filed += 1
            else:
                failed += 1
                if failed <= 3:
                    print(f"  feedback failed for {persona.key}: HTTP {status}")
    return filed, failed


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
    if args.no_feedback:
        print("\nSkipping feedback votes (--no-feedback).")
    else:
        spread = (f", spread over {args.days_back} day(s)" if args.days_back
                  else " (all stamped now)")
        print(f"\nFiling satisfaction votes{spread}...")
        filed, failed = _vote_on_turns(results, cfg, args.days_back)
        print(f"  {filed} vote(s) filed, {failed} failed. "
              "They appear on Overview > User satisfaction, tagged as simulated.")

    print(f"\nElapsed {time.time() - t0:.0f}s. Results: {RESULTS_DIR}/*.jsonl")
    print("Telemetry takes a few minutes to surface in CloudWatch; the dashboard "
          "caches for 5 min (use Refresh to force).")
    if args.days_back:
        print("NOTE: --days-back backdates only the rows this script writes "
              "(feedback votes). Spans and evaluation scores are stamped by "
              "AgentCore and cannot be moved, so the long-range views stay "
              "sparse before today — that is real, not a bug.")
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
        description="Simulate real end users against the deployed agent, so the "
                    "ops dashboard and AgentCore Evaluations have real data.",
        epilog=(
            "requires:  export SIM_USER_PASSWORD='SomeStrong#Pass1'\n"
            "\n"
            "typical pre-demo run:\n"
            "  python3 simulate-users.py setup     # 5 personas, idempotent\n"
            "  python3 simulate-users.py run       # ~2 min, 23 conversations\n"
            "  # wait 2-3 min, then open Admin Console > Overview at 24h\n"
            "\n"
            "docs:\n"
            "  docs/admin_manual_管理员使用手册.md  §10.3  pre-demo runbook\n"
            "  scripts/sim/README.md                     personas and gotchas\n"
            "\n"
            "Scoped to the simuser+ email prefix; real users are never touched."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
    r.add_argument("--days-back", type=int, default=0, metavar="N",
                   help="spread the satisfaction votes over the past N days so "
                        "the 60d/90d dashboard views have data (default 0: today)")
    r.add_argument("--no-feedback", action="store_true",
                   help="skip filing satisfaction votes")
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
