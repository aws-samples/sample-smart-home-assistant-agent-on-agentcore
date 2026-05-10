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
}

AGENT_SHORT_TO_LONG = {
    "energy-optimization": "energy-optimization-agent",
    "home-security": "home-security-agent",
    "appliance-maintenance": "appliance-maintenance-agent",
}


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


async def smoke_one(entry: dict, token: str) -> bool:
    agent_long = AGENT_SHORT_TO_LONG[entry["agent"]]
    prompt, marker = PROMPTS[agent_long]
    invocation_url = entry["invocationUrl"]
    # The invocation URL ends with /invocations; A2A expects the endpoint root.
    # AgentCore Runtime proxies POST /invocations to the container's POST /.
    # The a2a-sdk client resolver fetches /.well-known/agent-card.json which
    # the container exposes — AgentCore's edge passes that GET through.
    endpoint = invocation_url.rsplit("/invocations", 1)[0]

    headers = {"Authorization": f"Bearer {token}"}
    print(f"\n=== {agent_long} ===")
    print(f"  endpoint: {endpoint}")
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

    results = []
    for entry in state["agents"]:
        results.append(await smoke_one(entry, token))
    print()
    print("summary:", dict(zip([a["agent"] for a in state["agents"]], results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
