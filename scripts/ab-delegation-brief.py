#!/usr/bin/env python3
"""A/B the delegation brief (spec 5 S2) against the deployed light-effect agent.

Same prompt, alternating: once with the brief the orchestrator would attach, once
without, four pairs. Alternating rather than blocked so a slow patch on the Bedrock
side lands on both arms instead of on whichever ran second.

Sent STRAIGHT to the sub-agent over A2A, deliberately. S2 changes what the
specialist does, and routing through the orchestrator would add its own two LLM
calls — whose run-to-run spread (13% on the delegated group, see
docs/measurements/README.md) is larger than the effect being measured. The
orchestrator's own share of the win is the same 1.6s, since it waits on this call.

Why `scripts/measure-baseline.py` cannot show this: its ten prompts are all
read-only, so none of them reaches a specialist that receives a device brief.
Making one of them a write would leave the user's lights changed three times per
baseline run. So the two instruments are complementary, not redundant.

Measured 2026-08-11: 16.83s without, 15.19s with, **-1.64s (-10%)**, and the brief
won every one of the four pairs.

Usage:  ./venv/bin/python scripts/ab-delegation-brief.py
"""
import asyncio, json, sys, time, uuid
from pathlib import Path
import boto3, httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, TextPart

REPO = Path(__file__).resolve().parent.parent
HERE = REPO / "a2a-agent-registry"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "shared"))
from common.agents import ALLOWED_SKILLS_HEADER, USER_TOKEN_HEADER
from device_brief import delegation_context

state = json.loads((HERE / "deployed-state.json").read_text())
entry = next(a for a in state["agents"] if a["agent"] == "light-effect")
cog = state["cognito"]
region = cog["tokenUrl"].split(".auth.")[1].split(".amazoncognito")[0]
sm = boto3.client("secretsmanager", region_name=region)
creds = json.loads(sm.get_secret_value(SecretId=cog["m2mSecretArn"])["SecretString"])
m2m = httpx.post(cog["tokenUrl"], data={"grant_type": "client_credentials",
                 "scope": cog["scope"]},
                 auth=(creds["client_id"], creds["client_secret"]), timeout=10
                 ).json()["access_token"]
out = json.loads((REPO / "cdk-outputs.json").read_text())
o = out[next(iter(out))]
idtok = boto3.client("cognito-idp", region_name=region).initiate_auth(
    ClientId=o["UserPoolClientId"], AuthFlow="USER_PASSWORD_AUTH",
    AuthParameters={"USERNAME": o["AdminUsername"], "PASSWORD": o["AdminPassword"]}
)["AuthenticationResult"]["IdToken"]

skills = [s["id"] for s in json.loads((HERE / "light-effect" / "card.json").read_text())["skills"]]
BASE = "Compose a calm twilight lighting effect for the living room light strip."

async def one(with_brief):
    msg = BASE + (delegation_context(BASE) if with_brief else "")
    headers = {"Authorization": f"Bearer {m2m}",
               ALLOWED_SKILLS_HEADER: ",".join(skills),
               USER_TOKEN_HEADER: idtok}
    async with httpx.AsyncClient(headers=headers, timeout=120) as http:
        card = await A2ACardResolver(http, entry["invocationUrl"]).get_agent_card()
        card.url = entry["invocationUrl"]
        client = ClientFactory(ClientConfig(httpx_client=http, streaming=False)).create(card)
        t0 = time.time()
        async for _ in client.send_message(Message(
                message_id=str(uuid.uuid4()), role=Role.user,
                parts=[Part(root=TextPart(text=msg))])):
            pass
        return time.time() - t0, len(msg)

async def main():
    res = {True: [], False: []}
    for i in range(4):
        for with_brief in (False, True):
            dt, n = await one(with_brief)
            res[with_brief].append(dt)
            print(f"  brief={str(with_brief):5} msg={n:5} chars  {dt:5.2f}s", flush=True)
            await asyncio.sleep(3)
    import statistics as st
    a, b = st.fmean(res[False]), st.fmean(res[True])
    print(f"\nwithout brief: {a:5.2f}s  (n={len(res[False])})")
    print(f"with brief   : {b:5.2f}s  (n={len(res[True])})")
    print(f"delta        : {b-a:+5.2f}s  ({(b-a)/a*100:+.0f}%)")

asyncio.run(main())
