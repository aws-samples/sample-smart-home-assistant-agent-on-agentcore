#!/usr/bin/env python3
"""Smoke test the deployed A2A sample agents.

For each entry in ``deployed-state.json``:
  1. Fetch a Cognito m2m token (client_credentials grant).
  2. Open the Runtime invocation URL as the A2A endpoint.
  3. Fetch ``/.well-known/agent-card.json`` and print name + skills.
  4. Send one ``message/send`` with a known prompt; assert reply contains
     the agent's marker token.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import boto3
import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, TextPart


HERE = Path(__file__).resolve().parent
DEPLOYED_STATE = HERE / "deployed-state.json"

PROMPTS = {
    "energy-optimization-agent": (
        "How much can I save by dimming LEDs at night?",
        "⟦A2A:energy-optimization⟧",
    ),
    "home-security-agent": (
        "What's my biggest smart-home security gap?",
        "⟦A2A:home-security⟧",
    ),
    "appliance-maintenance-agent": (
        "When should I replace my AC filter?",
        "⟦A2A:appliance-maintenance⟧",
    ),
    # This one has real tools, so the prompt asks it to READ rather than control:
    # a smoke test should not leave the user's devices in a different state.
    "device-control-agent": (
        "What is the living room environment sensor reading right now?",
        "⟦A2A:device-control⟧",
    ),
}

# Roster: see common/agents.py. A stale copy here makes the smoke test skip an
# agent silently, which is the opposite of what a smoke test is for.
sys.path.insert(0, str(HERE))
from common.agents import AGENT_LONG_NAMES as AGENT_SHORT_TO_LONG  # noqa: E402
from common.agents import ALLOWED_SKILLS_HEADER, USER_TOKEN_HEADER  # noqa: E402


def fetch_m2m_token(region: str, token_url: str, scope: str, secret_arn: str) -> str:
    sm = boto3.client("secretsmanager", region_name=region)
    creds = json.loads(sm.get_secret_value(SecretId=secret_arn)["SecretString"])
    r = httpx.post(
        token_url,
        data={"grant_type": "client_credentials", "scope": scope},
        auth=(creds["client_id"], creds["client_secret"]),
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_user_id_token() -> str:
    """Sign in as the admin user and return an idToken.

    Stands in for the chatbot: an agent with tools needs a real end user to act
    as, and the m2m token cannot supply one. Returns "" if the credentials are not
    available, so the prompt-only agents still get tested.
    """
    outputs_path = HERE.parent / "cdk-outputs.json"
    if not outputs_path.exists():
        print(f"  note: {outputs_path} missing — skipping the user idToken")
        return ""
    outputs = json.loads(outputs_path.read_text())
    out = outputs[next(iter(outputs))]
    try:
        region = out["UserPoolId"].split("_")[0]
        cognito = boto3.client("cognito-idp", region_name=region)
        resp = cognito.initiate_auth(
            ClientId=out["UserPoolClientId"],
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": out["AdminUsername"],
                "PASSWORD": out["AdminPassword"],
            },
        )
        return resp["AuthenticationResult"]["IdToken"]
    except Exception as exc:  # noqa: BLE001
        print(f"  note: could not fetch a user idToken ({exc}) — tool-using "
              f"agents will refuse")
        return ""


def _card_skill_ids(agent: str) -> list[str]:
    """Read the skill ids this agent publishes straight from its card.json."""
    card_path = HERE / agent / "card.json"
    card = json.loads(card_path.read_text(encoding="utf-8"))
    return [s["id"] for s in (card.get("skills") or []) if s.get("id")]


async def smoke_one(entry: dict, token: str, user_token: str | None = None) -> bool:
    agent_long = AGENT_SHORT_TO_LONG[entry["agent"]]
    prompt, marker = PROMPTS[agent_long]
    invocation_url = entry["invocationUrl"]
    # The invocation URL ends with /invocations; A2A expects the endpoint root.
    # AgentCore Runtime proxies POST /invocations to the container's POST /.
    # The a2a-sdk client resolver fetches /.well-known/agent-card.json which
    # the container exposes — AgentCore's edge passes that GET through.
    endpoint = invocation_url.rsplit("/invocations", 1)[0]

    # The skills header is now ENFORCED server-side, so the smoke test has to send
    # a real grant like the orchestrator does. Sending every skill the agent
    # publishes is the right analogue of a fully-granted user.
    headers = {
        "Authorization": f"Bearer {token}",
        ALLOWED_SKILLS_HEADER: ",".join(_card_skill_ids(entry["agent"])),
    }
    if user_token:
        headers[USER_TOKEN_HEADER] = user_token
    print(f"\n=== {agent_long} ===")
    print(f"  endpoint: {endpoint}")
    print(f"  granted skills: {headers[ALLOWED_SKILLS_HEADER]}")
    async with httpx.AsyncClient(headers=headers, timeout=120) as http:
        try:
            # AgentCore Runtime likely serves the card under /invocations
            # rather than at the endpoint root. Try both.
            for card_url in (endpoint, invocation_url):
                try:
                    card = await A2ACardResolver(http, card_url).get_agent_card()
                    print(f"  card.url: {card.url}")
                    print(f"  name: {card.name}")
                    print(f"  skills: {[s.id for s in card.skills]}")
                    break
                except Exception as e:
                    print(f"  card fetch via {card_url} failed: {e}")
                    card = None
            if not card:
                return False

            # Pin the URL to the invocation URL since the agent card's own URL
            # was set at build time and may not match the runtime.
            card.url = invocation_url
            factory = ClientFactory(ClientConfig(httpx_client=http, streaming=False))
            client = factory.create(card)
            msg = Message(
                message_id=str(uuid.uuid4()),
                role=Role.user,
                parts=[Part(root=TextPart(text=prompt))],
            )
            reply_text = None
            async for event in client.send_message(msg):
                items = event if isinstance(event, tuple) else (event,)
                for item in items:
                    if item is None:
                        continue
                    # Task with artifacts (Strands A2AServer idiom)
                    for art in getattr(item, "artifacts", None) or []:
                        for p in getattr(art, "parts", None) or []:
                            r = getattr(p, "root", p)
                            if getattr(r, "kind", "") == "text":
                                reply_text = r.text
                    # Or a direct Message (some servers emit this)
                    for p in getattr(item, "parts", None) or []:
                        r = getattr(p, "root", p)
                        if getattr(r, "kind", "") == "text" and not reply_text:
                            reply_text = r.text
            if not reply_text:
                print("  FAIL: no text reply")
                return False
            ok = marker in reply_text
            print(f"  marker '{marker}' present: {ok}")
            print(f"  reply (truncated):\n    " + reply_text[:500].replace("\n", "\n    "))
            return ok
        except Exception as e:
            print(f"  ERROR: {e}")
            return False


async def expect_refusal(entry: dict, token: str, skills_header: str | None,
                         label: str) -> bool:
    """Send a request that SHOULD be refused and report whether it was.

    This is the half of the smoke test that proves the server-side check exists.
    The header used to be advisory, so anything holding the shared m2m token could
    call any skill on any agent; a passing positive test says nothing about that.

    A refusal arrives as an ordinary agent reply beginning "Request refused:" —
    the orchestrator shows tool output to its own model, so a readable refusal is
    more useful than an opaque 500.
    """
    invocation_url = entry["invocationUrl"]
    headers = {"Authorization": f"Bearer {token}"}
    if skills_header is not None:
        headers[ALLOWED_SKILLS_HEADER] = skills_header

    print(f"\n--- negative: {label} ({entry['agent']}) ---")
    async with httpx.AsyncClient(headers=headers, timeout=120) as http:
        try:
            card = await A2ACardResolver(http, invocation_url).get_agent_card()
            card.url = invocation_url
            factory = ClientFactory(ClientConfig(httpx_client=http, streaming=False))
            client = factory.create(card)
            msg = Message(
                message_id=str(uuid.uuid4()),
                role=Role.user,
                parts=[Part(root=TextPart(text="hello"))],
            )
            reply = None
            async for event in client.send_message(msg):
                items = event if isinstance(event, tuple) else (event,)
                for item in items:
                    if item is None:
                        continue
                    for art in getattr(item, "artifacts", None) or []:
                        for p in getattr(art, "parts", None) or []:
                            r = getattr(p, "root", p)
                            if getattr(r, "kind", "") == "text":
                                reply = r.text
                    for p in getattr(item, "parts", None) or []:
                        r = getattr(p, "root", p)
                        if getattr(r, "kind", "") == "text" and not reply:
                            reply = r.text
            refused = bool(reply) and "refused" in reply.lower()
            print(f"  refused: {refused}")
            print(f"  reply: {(reply or '(none)')[:220]}")
            return refused
        except Exception as e:
            # A transport-level rejection also counts as a refusal.
            print(f"  refused at transport: {e}")
            return True


async def main() -> int:
    state = json.loads(DEPLOYED_STATE.read_text())
    cognito = state["cognito"]
    region = cognito["tokenUrl"].split(".")[1]  # parse "...auth.us-west-2.amazoncognito.com"
    # More robust parse: token_url = https://<domain>.auth.<region>.amazoncognito.com/oauth2/token
    try:
        region = cognito["tokenUrl"].split(".auth.")[1].split(".amazoncognito")[0]
    except Exception:
        pass
    token = fetch_m2m_token(
        region=region,
        token_url=cognito["tokenUrl"],
        scope=cognito["scope"],
        secret_arn=cognito["m2mSecretArn"],
    )
    print(f"fetched m2m token ({len(token)} chars)")

    # An agent with tools acts on a real user's devices and refuses a request with
    # no verified user identity, so the smoke test has to present one the way the
    # orchestrator does. Sign in as the admin from cdk-outputs.json.
    user_token = fetch_user_id_token()
    print(f"fetched user idToken ({len(user_token) if user_token else 0} chars)")

    results = {}
    for entry in state["agents"]:
        results[entry["agent"]] = await smoke_one(entry, token, user_token)

    # Negative cases, run against one agent — the check is in shared code, so one
    # agent exercises the same path all of them use.
    if state["agents"]:
        probe = state["agents"][0]
        results["negative:no-skills-header"] = await expect_refusal(
            probe, token, None, "no skills header")
        results["negative:empty-skills-header"] = await expect_refusal(
            probe, token, "", "empty skills header")
        results["negative:other-agents-skill"] = await expect_refusal(
            probe, token, "some_skill_this_agent_does_not_publish",
            "a skill this agent does not publish")

    # A tool-using agent must refuse when there is no user identity to act as —
    # otherwise it would fall back to acting as its own service identity, which is
    # exactly the cross-user hole the forwarded token exists to close.
    tool_agents = [e for e in state["agents"]
                   if (HERE / e["agent"] / "tools.py").exists()]
    for entry in tool_agents:
        skills = ",".join(_card_skill_ids(entry["agent"]))
        results[f"negative:{entry['agent']}-no-user-token"] = await expect_refusal(
            entry, token, skills, "granted skills but NO user token")

    print()
    print("summary:", results)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
