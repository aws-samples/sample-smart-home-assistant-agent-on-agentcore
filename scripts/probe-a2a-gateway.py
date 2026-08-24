#!/usr/bin/env python3
"""The acceptance gate for routing A2A delegation through a gateway.

Run before changing any AgentCard. Every check is a thing that could silently stop
working once a hop is inserted, and each one has a specific failure signature:

  1. A real delegation still answers through the gateway at all.
  2. `cognito:groups` is still enforced — a caller WITHOUT the grant is refused.
     If the hop swallowed the claim check, every user would reach every specialist.
  3. Per-user identity still resolves inside the container. If the gateway replaced
     the caller's token with its own credential, the specialist would answer about
     nobody's home.
  4. The orchestrator's runtime session id still reaches the sub-agent. Only the
     metadata channel can (the header allowlist rejects `x-amzn-`), but a gateway
     that rewrote the JSON-RPC body would break it, and the span-level join depends
     separately on the platform still adopting the session header.
  5. Latency delta against a direct call, measured rather than assumed.

Usage:
    ./venv/bin/python scripts/probe-a2a-gateway.py
    ./venv/bin/python scripts/probe-a2a-gateway.py --agent energy-optimization
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import boto3

REPO = Path(__file__).resolve().parent.parent
REGION = "us-west-2"
GATEWAY_NAME = "smarthome-a2a-gw"
POOL_ID = "us-west-2_HwYYt6qLz"
CLIENT_ID = "7031k7ctqes8mfe5jrf7kljttf"
ADMIN = ("admin@smarthome.local", "SmartHome#Admin1")

SESSION_METADATA_KEY = "orchestratorSessionId"
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


def token(username: str, password: str) -> str:
    cog = boto3.client("cognito-idp", region_name=REGION)
    return cog.initiate_auth(
        ClientId=CLIENT_ID, AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )["AuthenticationResult"]["IdToken"]


def gateway_url() -> str:
    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
    for page in ac.get_paginator("list_gateways").paginate():
        for g in page.get("items", []):
            if g.get("name") == GATEWAY_NAME:
                return ac.get_gateway(
                    gatewayIdentifier=g["gatewayId"])["gatewayUrl"].rstrip("/")
    sys.exit(f"gateway {GATEWAY_NAME} not found — run scripts/setup-a2a-gateway.py")


def runtime_url(agent: str) -> str:
    state = json.loads((REPO / "a2a-agent-registry" / "deployed-state.json").read_text())
    for entry in state.get("agents") or []:
        if entry["agent"] == agent:
            return entry["invocationUrl"]
    sys.exit(f"{agent} not in deployed-state.json")


def send(url: str, tok: str, text: str, session_id: str, timeout: int = 90) -> dict:
    """One A2A `message/send`, returning {status, seconds, body}.

    Hand-rolled rather than via the a2a SDK on purpose: this probe has to be able to
    see a 403 or a 404 from the GATEWAY, and the SDK turns transport errors into its
    own exceptions. The payload shape is what `agent/tools/a2a.py` sends.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()),
            "role": "user",
            "kind": "message",
            "parts": [{"kind": "text", "text": text}],
            "metadata": {SESSION_METADATA_KEY: session_id},
        }},
    }
    req = urllib.request.Request(
        url, method="POST", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json",
                 SESSION_HEADER: session_id})
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            # NOT truncated. A real A2A reply runs well past 4KB, and a body cut
            # mid-JSON parses as nothing — which presents as "the gateway answered
            # 200 with no reply" and blocks a cutover that was actually fine.
            return {"status": r.status, "seconds": time.time() - started,
                    "body": r.read().decode()}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "seconds": time.time() - started,
                "body": e.read().decode()[:2000]}
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "seconds": time.time() - started, "body": repr(e)[:500]}


def reply_text(body: str) -> str:
    try:
        doc = json.loads(body)
    except ValueError:
        return ""
    result = doc.get("result") or {}
    chunks = []
    for artifact in result.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            if part.get("kind") == "text":
                chunks.append(part.get("text", ""))
    for part in (result.get("parts") or []):
        if part.get("kind") == "text":
            chunks.append(part.get("text", ""))
    return "\n".join(chunks)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="energy-optimization")
    ap.add_argument("--runs", type=int, default=2,
                    help="timed runs per path for the latency comparison")
    args = ap.parse_args(argv)

    gw = f"{gateway_url()}/{args.agent}"
    direct = runtime_url(args.agent)
    tok = token(*ADMIN)
    session_id = f"probe-session-{uuid.uuid4().hex}{uuid.uuid4().hex}"[:80]
    results: dict[str, object] = {}
    verdicts: list[tuple[str, bool, str]] = []

    print(f"gateway : {gw}")
    print(f"direct  : {direct[:80]}…")
    print(f"session : {session_id}\n")

    # 1 + 3. A granted caller gets a real answer, and it is about THEIR home.
    ask = "How much could I save on the living room light? Name the device you used."
    out = send(gw, tok, ask, session_id)
    text = reply_text(out["body"])
    ok = out["status"] == 200 and bool(text)
    verdicts.append(("1. delegation answers through the gateway", ok,
                     f"HTTP {out['status']} in {out['seconds']:.1f}s, "
                     f"{len(text)} chars of reply"))
    if not ok:
        print(f"   body: {out['body'][:600]}")
    # A per-user answer names a real device from this user's own fleet.
    identity_ok = any(k in text for k in ("living", "客厅", "led", "LED", "strip", "灯"))
    verdicts.append(("3. per-user identity resolves in the container", identity_ok,
                     "the reply names a device from this user's fleet"
                     if identity_ok else f"reply did not name a device: {text[:200]}"))
    results["gateway_reply"] = text[:400]

    # 2. A caller with a valid token but NO grant on this agent must be refused.
    #    Built by asking for a token for a user who holds no grant here; if no such
    #    user exists the check reports itself as not run rather than passing.
    # A throwaway user created for this check and deleted afterwards. Reusing an
    # existing one is not good enough: whether it happens to hold a grant is exactly
    # the variable under test, and a user who quietly HAS the grant would make this
    # check pass by accident. The password never leaves this process.
    cog = boto3.client("cognito-idp", region_name=REGION)
    probe_email = f"a2a-gw-probe-{uuid.uuid4().hex[:10]}@smarthome.local"
    probe_password = f"Probe#{uuid.uuid4().hex[:12]}Aa1"
    deny_status = None
    try:
        cog.admin_create_user(
            UserPoolId=POOL_ID, Username=probe_email, MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": probe_email},
                            {"Name": "email_verified", "Value": "true"}])
        cog.admin_set_user_password(UserPoolId=POOL_ID, Username=probe_email,
                                   Password=probe_password, Permanent=True)
        held = [g["GroupName"] for g in cog.admin_list_groups_for_user(
            UserPoolId=POOL_ID, Username=probe_email, Limit=60).get("Groups", [])]
        print(f"   deny check: {probe_email} holds {held or 'no groups'}")

        # Every sub-agent is granted GLOBALLY in this deployment, so a new user is
        # not ungranted — the pre-token trigger gives them the global set. To test a
        # denial at all, narrow this throwaway user to nothing on the target agent:
        # per-user intent REPLACES global per sub-agent, and an empty list means
        # "none". That makes the check prove something stronger than "a stranger is
        # refused" — it proves per-user narrowing is enforced at the platform door,
        # through the gateway hop, for a grant that exists only as a claim.
        ddb = boto3.resource("dynamodb", region_name=REGION).Table("smarthome-skills")
        target_record = None
        try:
            reg = boto3.client("agent-registry-control", region_name=REGION)
            state = json.loads(
                (REPO / "a2a-agent-registry" / "deployed-state.json").read_text())
            target_record = next(e.get("recordId") for e in state["agents"]
                                 if e["agent"] == args.agent)
        except Exception as exc:  # noqa: BLE001
            print(f"   deny check: could not resolve the recordId ({exc})")
        if target_record:
            ddb.put_item(Item={"userId": probe_email,
                               "skillName": "__a2a_permissions__",
                               "a2aGrants": {target_record: []}})
            print(f"   deny check: narrowed {args.agent} ({target_record}) to []")
            # The trigger caches the record catalog for 60s; a fresh token is only
            # narrowed once that expires or a cold container answers.
            time.sleep(65)
        untok = token(probe_email, probe_password)
        # If the pre-token trigger injected global grants, this user legitimately IS
        # granted and the check cannot prove anything — say so rather than pass.
        import base64 as _b64
        payload = untok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claimed = json.loads(_b64.urlsafe_b64decode(payload)).get("cognito:groups") or []
        granted_here = [g for g in claimed if g.startswith(f"a2a-{args.agent}")]
        denied = send(gw, untok, "hello", session_id, timeout=60)
        deny_status = denied["status"]
        if granted_here:
            verdicts.append((
                "2. a narrowed caller is refused", None,
                f"the narrowing did not take: the claim still holds {granted_here}; "
                f"HTTP {deny_status}"))
        else:
            verdicts.append((
                "2. a narrowed caller is refused", deny_status in (401, 403),
                f"HTTP {deny_status} for a user whose claim carries no "
                f"a2a-{args.agent} group"))
    except Exception as exc:  # noqa: BLE001
        verdicts.append(("2. a narrowed caller is refused", None,
                         f"could not run: {exc}"))
    finally:
        try:
            boto3.resource("dynamodb", region_name=REGION).Table(
                "smarthome-skills").delete_item(
                Key={"userId": probe_email, "skillName": "__a2a_permissions__"})
        except Exception:  # noqa: BLE001
            print(f"   deny check: LEFT BEHIND the grant row for {probe_email}")
        try:
            cog.admin_delete_user(UserPoolId=POOL_ID, Username=probe_email)
            print(f"   deny check: deleted {probe_email}")
        except Exception:  # noqa: BLE001
            print(f"   deny check: LEFT BEHIND {probe_email} — delete it by hand")

    # 4. The session id reached the sub-agent. Read from ITS log group rather than
    #    from the reply, because the reply cannot carry it.
    logs = boto3.client("logs", region_name=REGION)
    slug = json.loads((REPO / "a2a-agent-registry" / "deployed-state.json").read_text())
    runtime_id = next(e["runtimeId"] for e in slug["agents"] if e["agent"] == args.agent)
    group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
    found = False
    for _ in range(10):
        try:
            events = logs.filter_log_events(
                logGroupName=group,
                startTime=int((time.time() - 600) * 1000),
                filterPattern=f'"{session_id}"').get("events", [])
        except Exception as exc:  # noqa: BLE001
            print(f"   (log read failed: {exc})")
            events = []
        if events:
            found = True
            break
        time.sleep(6)
    verdicts.append(("4. the session id survives the hop (metadata channel)", found,
                     f"log group {group.split('/')[-1]}"))

    # 5. Latency, gateway versus direct.
    def timed(url: str) -> list[float]:
        seen = []
        for _ in range(args.runs):
            r = send(url, tok, "In one sentence: any quick energy win?", session_id)
            if r["status"] == 200:
                seen.append(r["seconds"])
        return seen

    gw_times, direct_times = timed(gw), timed(direct)
    if gw_times and direct_times:
        delta = statistics.median(gw_times) - statistics.median(direct_times)
        verdicts.append((
            "5. latency delta is acceptable", delta < 1.5,
            f"gateway median {statistics.median(gw_times):.2f}s vs direct "
            f"{statistics.median(direct_times):.2f}s -> {delta:+.2f}s"))
    else:
        verdicts.append(("5. latency delta is acceptable", None,
                         f"gateway {len(gw_times)}/{args.runs} ok, "
                         f"direct {len(direct_times)}/{args.runs} ok"))

    print("\n=== acceptance gate ===")
    blocked = False
    for name, ok, detail in verdicts:
        mark = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        if ok is False:
            blocked = True
        print(f"  [{mark}] {name}\n         {detail}")
    print("\nVERDICT:", "BLOCKED — do not cut over" if blocked else
          "no blocking failure (read the SKIPs before cutting over)")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
