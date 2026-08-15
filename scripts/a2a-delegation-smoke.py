#!/usr/bin/env python3
"""Prove an A2A agent is callable — and refuses the ungranted — without our help.

    ./venv/bin/python scripts/a2a-delegation-smoke.py --record-id rXu3cIg291cn \
        --granted-user alice@example.com --granted-password '...'
    ./venv/bin/python scripts/a2a-delegation-smoke.py --card path/to/card.json \
        --url https://.../invocations --granted-user ... --ungranted-user ...
    ./venv/bin/python scripts/a2a-delegation-smoke.py --record-id r... --offline

The gap this closes
-------------------
An agent team could get everything right and still have no way to know it. The
authorizer contract generator says what to configure and the conformance check says
whether it matches — both static. Neither sends a request. So "is my agent actually
reachable by a granted user, and actually closed to everyone else" was answered by
asking the platform team to try it, which is the cross-team round trip this work exists
to remove.

It asserts BOTH directions, because only one of them is visible in normal use:

  1. A GRANTED user gets a 200 and a real A2A response.
  2. An UNGRANTED user gets 401/403 AT THE DOOR.

(2) is the one nobody notices is broken. A missing `customClaims` means every
authenticated user of the pool reaches every skill, with no error, no log line and a
console that reads `approved` throughout. A test that only checks (1) passes just as
happily against a wide-open agent.

What it does NOT do
-------------------
It does not grant anything. Granting is the platform's decision and stays there (see
docs/a2a-agent-onboarding.md §5). This needs two accounts that ALREADY differ: one
granted this agent, one not. It reports plainly when it cannot tell them apart rather
than guessing, because a "pass" from two identically-granted users is worse than no
result.

Credentials
-----------
Passwords are read from the environment (`A2A_SMOKE_GRANTED_PASSWORD` /
`A2A_SMOKE_UNGRANTED_PASSWORD`) or prompted for. They are never logged, and no token is
printed — only its claim summary.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "shared"))

import a2a_conformance as conf  # noqa: E402
import a2a_groups  # noqa: E402
import a2a_session  # noqa: E402
import agent_registry as registry_ns  # noqa: E402

REGION = "us-west-2"
TIMEOUT = 90


class Result:
    """Accumulates checks so every one runs and the exit code reflects all of them."""

    def __init__(self) -> None:
        self.rows: list[tuple[bool | None, str, str]] = []

    def add(self, ok: bool | None, name: str, detail: str = "") -> None:
        self.rows.append((ok, name, detail))
        mark = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)

    def report(self) -> int:
        failed = [r for r in self.rows if r[0] is False]
        skipped = [r for r in self.rows if r[0] is None]
        print()
        print(f"{len(self.rows) - len(failed) - len(skipped)} passed, "
              f"{len(failed)} failed, {len(skipped)} skipped")
        if skipped and not failed:
            # Loud, because a run that skipped the negative check has NOT shown the
            # agent is closed, and "0 failed" would otherwise read as if it had.
            print("\nNOTE: skipped checks are not passes. In particular, without an "
                  "ungranted user this run says nothing about whether callers who were "
                  "never granted this agent are refused.")
        return 1 if failed else 0


# ---------------------------------------------------------------------------
# Deployment identity and the target under test
# ---------------------------------------------------------------------------

def _deployment() -> dict:
    outputs = json.loads((REPO / "cdk-outputs.json").read_text())
    stack = next(iter(outputs.values()))
    state = json.loads((REPO / "agentcore-state.json").read_text())
    return {
        "region": REGION,
        "userPoolId": stack["UserPoolId"],
        "appClientId": stack["UserPoolClientId"],
        "registryId": state["registryId"],
        "discoveryUrl": (f"https://cognito-idp.{REGION}.amazonaws.com/"
                         f"{stack['UserPoolId']}/.well-known/openid-configuration"),
    }


def _card_and_url(args, dep: dict) -> tuple[dict, str]:
    if args.card:
        card = json.loads(Path(args.card).read_text(encoding="utf-8"))
        return card, (args.url or card.get("url") or "")
    import boto3  # noqa: F401  (registry_client needs it)

    client = registry_ns.registry_client(REGION)
    detail = client.get_registry_record(registryId=dep["registryId"],
                                        recordId=args.record_id)
    raw = registry_ns.read_agent_card(detail)
    if not raw:
        sys.exit(f"record {args.record_id} carries no AgentCard")
    card = json.loads(raw)
    status = detail.get("status", "")
    print(f"  record {args.record_id}: status={status}")
    if status != registry_ns.STATUS_APPROVED:
        print("  NOTE: not APPROVED, so the orchestrator will not offer tools for it. "
              "The door checks below still mean what they say.")
    return card, (args.url or card.get("url") or "")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def _id_token(dep: dict, username: str, password: str) -> str:
    import boto3

    resp = boto3.client("cognito-idp", region_name=REGION).initiate_auth(
        ClientId=dep["appClientId"], AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password})
    return resp["AuthenticationResult"]["IdToken"]


def _claim_groups(token: str) -> list[str]:
    """`cognito:groups` out of an idToken WITHOUT verifying it.

    Safe here: nothing is authorized from this. It is used to report what the token
    carries, and to refuse to run a meaningless test (both users granted the same).
    """
    import base64

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    return sorted(str(g) for g in (claims.get("cognito:groups") or []))


# ---------------------------------------------------------------------------
# The A2A call
# ---------------------------------------------------------------------------

def _send(url: str, token: str, text: str, session_id: str) -> tuple[int, str]:
    """One `message/send`. Returns `(status, body)`; never raises on an HTTP error.

    An HTTP error IS the result for the negative check, so urllib's habit of raising on
    4xx has to be caught rather than allowed to abort the run.
    """
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/send",
        "params": {"message": {
            "role": "user",
            "messageId": uuid.uuid4().hex,
            "parts": [{"kind": "text", "text": text}],
            # The only channel a container can read the parent session id on: the
            # platform session header exists but AgentCore's allowlist rejects
            # `x-amzn-*`. See shared/a2a_session.py.
            "metadata": {a2a_session.SESSION_ID_METADATA_KEY: session_id},
        }},
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        a2a_session.RUNTIME_SESSION_ID_HEADER: session_id,
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def _response_text(body: str) -> str:
    """The assistant text out of an A2A JSON-RPC result, or "" if it is not there."""
    try:
        doc = json.loads(body)
    except ValueError:
        return ""
    result = doc.get("result") or {}
    for container in (result, *(result.get("artifacts") or []),
                      *((result.get("status") or {}).get("message") or {},)):
        for part in (container.get("parts") or []):
            if part.get("text"):
                return part["text"]
    return ""


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def offline_checks(card: dict, url: str, dep: dict, res: Result) -> None:
    """Everything provable without a token. Always run — they explain later failures."""
    name = card.get("name") or ""
    res.add(bool(name), "card has a name", name or "missing")

    door, findings = conf.expected_groups(card)
    skills, _ = conf.grantable_groups(card)
    blocking = [f for f in findings if f["severity"] != conf.INFO]
    res.add(not blocking, "card names encode as groups",
            "; ".join(f["detail"] for f in blocking) if blocking
            else f"door={door[0] if door else '?'}, {len(skills)} skill group(s)")

    res.add(bool(url), "card has a url", url or "missing")
    res.add(url.endswith("/invocations") or ".gateway.bedrock-agentcore." in url,
            "url looks like a runtime or gateway endpoint", url[:90])


def granted_check(url: str, token: str, card: dict, prompt: str,
                  res: Result) -> None:
    session_id = f"smoke-{uuid.uuid4().hex}{uuid.uuid4().hex}"[:64]
    started = time.time()
    status, body = _send(url, token, prompt, session_id)
    elapsed = time.time() - started
    res.add(status == 200, "a GRANTED caller is admitted",
            f"HTTP {status} in {elapsed:.1f}s"
            + ("" if status == 200 else f" — {body[:200]}"))
    if status != 200:
        return
    text = _response_text(body)
    res.add(bool(text.strip()), "the response carries assistant text",
            f"{len(text)} chars: {text[:80]!r}" if text else "no text part found")
    print(f"      session id sent: {session_id}")
    print("      (search the agent's log group for 'orchestrator session' to confirm "
          "it read it)")


def ungranted_check(url: str, token: str, groups: list[str], card: dict,
                    res: Result) -> None:
    """The direction nobody notices is broken.

    A refusal must come from the DOOR (401/403). A 200 here means this agent's
    authorization is not happening at all: `customClaims` is missing or matches
    something every user of the pool holds.
    """
    session_id = f"smoke-{uuid.uuid4().hex}{uuid.uuid4().hex}"[:64]
    status, body = _send(url, token, "smoke test: you should not see this", session_id)
    if status in (401, 403):
        res.add(True, "an UNGRANTED caller is refused at the door", f"HTTP {status}")
        return
    if status == 200:
        res.add(False, "an UNGRANTED caller is refused at the door",
                "HTTP 200 — this agent admitted a caller holding "
                f"{groups or 'no a2a groups'}. Its authorizer is not enforcing "
                "grants; every authenticated user of the pool can reach it")
        return
    # A 5xx or a transport error is not evidence either way, and calling it a pass
    # would be the single most dangerous wrong answer this script could give.
    res.add(False, "an UNGRANTED caller is refused at the door",
            f"inconclusive: HTTP {status} — {body[:200]}. A non-401/403 does not show "
            "the door is closed")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--record-id", help="an APPROVED Registry record to test")
    src.add_argument("--card", help="path to card.json (use with --url)")
    ap.add_argument("--url", help="override the endpoint under test")
    ap.add_argument("--granted-user", help="a user who HAS been granted this agent")
    ap.add_argument("--ungranted-user", help="a user who has NOT been granted it")
    ap.add_argument("--prompt", default="Smoke test: reply with one short sentence "
                                        "describing what you do.")
    ap.add_argument("--offline", action="store_true",
                    help="card and naming checks only; send nothing")
    args = ap.parse_args(argv)

    dep = _deployment()
    card, url = _card_and_url(args, dep)
    print(f"\nagent: {card.get('name')}  ->  {url[:100]}")
    print(f"pool : {dep['userPoolId']}  client {dep['appClientId']}\n")

    res = Result()
    offline_checks(card, url, dep, res)

    if args.offline:
        res.add(None, "a GRANTED caller is admitted", "--offline")
        res.add(None, "an UNGRANTED caller is refused at the door", "--offline")
        return res.report()
    if not url:
        return res.report()

    agent_name = card.get("name") or ""
    granted_groups: list[str] = []

    if args.granted_user:
        pw = (os.environ.get("A2A_SMOKE_GRANTED_PASSWORD")
              or getpass.getpass(f"password for {args.granted_user}: "))
        try:
            token = _id_token(dep, args.granted_user, pw)
        except Exception as exc:  # noqa: BLE001
            res.add(False, "a GRANTED caller is admitted", f"sign-in failed: {exc}")
            token = ""
        if token:
            granted_groups = _claim_groups(token)
            held = a2a_groups.skills_for_agent(granted_groups, agent_name)
            door_held = a2a_groups.agent_group_name(agent_name) in granted_groups
            print(f"      {args.granted_user} holds: door={door_held}, "
                  f"skills={sorted(held) or 'none'}")
            if not door_held:
                # Worth stating before the call rather than after the 401: this user is
                # not actually granted, so a failure below would be correct behaviour
                # being reported as a broken agent.
                res.add(False, "the granted user actually holds a grant",
                        f"no {a2a_groups.agent_group_name(agent_name)} in their claim "
                        "— grant them in Admin Console -> Tool Policy and sign in "
                        "again (grants live in the token)")
            else:
                res.add(True, "the granted user actually holds a grant",
                        f"{len(held)} skill(s)")
            granted_check(url, token, card, args.prompt, res)
    else:
        res.add(None, "a GRANTED caller is admitted", "no --granted-user")

    if args.ungranted_user:
        pw = (os.environ.get("A2A_SMOKE_UNGRANTED_PASSWORD")
              or getpass.getpass(f"password for {args.ungranted_user}: "))
        try:
            token = _id_token(dep, args.ungranted_user, pw)
        except Exception as exc:  # noqa: BLE001
            res.add(False, "an UNGRANTED caller is refused at the door",
                    f"sign-in failed: {exc}")
            token = ""
        if token:
            groups = _claim_groups(token)
            if a2a_groups.agent_group_name(agent_name) in groups:
                # Refusing to report a pass here is the whole point: two granted users
                # would both get 200, and the run would look like a clean bill of health
                # for an agent whose door was never tested.
                res.add(False, "an UNGRANTED caller is refused at the door",
                        f"{args.ungranted_user} IS granted this agent, so this check "
                        "would prove nothing. Pick a user without "
                        f"{a2a_groups.agent_group_name(agent_name)}")
            else:
                ungranted_check(url, token, groups, card, res)
    else:
        res.add(None, "an UNGRANTED caller is refused at the door",
                "no --ungranted-user")

    return res.report()


if __name__ == "__main__":
    raise SystemExit(main())
