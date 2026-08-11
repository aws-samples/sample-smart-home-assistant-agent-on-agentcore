#!/usr/bin/env python3
"""A/B parallel vs serial delegation across three specialists (spec 5 S4).

Serial arm mirrors the old transport: one delegation at a time. Parallel arm is what
the fixed transport now permits, which is also what Strands already attempts — it
issues independent tool calls concurrently, and before this change the third one
died with "This event loop is already running".

Measured 2026-08-11 over three alternating pairs: serial 51.1s, parallel 17.2s —
**-33.9s (-66%)**, and parallel won every pair. That is the shape you would expect
once the transport stops serialising: three ~17s specialists cost one specialist's
time instead of three.

Two things this does NOT measure, deliberately:
  - The orchestrator's own turn. Its two LLM calls are the same either way, so
    including them would only dilute the effect with their own variance.
  - Prewarming. It was measured separately and dropped: after 100+ minutes idle,
    a specialist's first call was ~0.3s slower than its warm calls (8.52s vs 8.39s
    for energy, 9.65s vs 9.28s for security). AgentCore keeps these runtimes hot,
    so there is no cold start left to hide.

Usage:  ./venv/bin/python scripts/ab-parallel-delegation.py
"""
import asyncio, json, sys, time, uuid
from pathlib import Path
import boto3, httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, TextPart

REPO = Path(__file__).resolve().parent.parent
HERE = REPO / "a2a-agent-registry"
sys.path.insert(0, str(HERE))
from common.agents import ALLOWED_SKILLS_HEADER

state = json.loads((HERE / "deployed-state.json").read_text())
cog = state["cognito"]
region = cog["tokenUrl"].split(".auth.")[1].split(".amazoncognito")[0]
creds = json.loads(boto3.client("secretsmanager", region_name=region)
                   .get_secret_value(SecretId=cog["m2mSecretArn"])["SecretString"])
m2m = httpx.post(cog["tokenUrl"],
                 data={"grant_type": "client_credentials", "scope": cog["scope"]},
                 auth=(creds["client_id"], creds["client_secret"]), timeout=10
                 ).json()["access_token"]

WORK = [
    ("home-security", "In one sentence: my biggest smart-home security gap?"),
    ("energy-optimization", "In one sentence: how much could dimming LEDs at night save?"),
    ("appliance-maintenance", "In one sentence: when should I replace an air purifier filter?"),
]

def _entry(agent):
    return next(a for a in state["agents"] if a["agent"] == agent)

def _skills(agent):
    return [s["id"] for s in json.loads((HERE / agent / "card.json").read_text())["skills"]]

async def call(agent, text):
    e = _entry(agent)
    headers = {"Authorization": f"Bearer {m2m}",
               ALLOWED_SKILLS_HEADER: ",".join(_skills(agent))}
    async with httpx.AsyncClient(headers=headers, timeout=180) as http:
        card = await A2ACardResolver(http, e["invocationUrl"]).get_agent_card()
        card.url = e["invocationUrl"]
        client = ClientFactory(ClientConfig(httpx_client=http, streaming=False)).create(card)
        async for _ in client.send_message(Message(
                message_id=str(uuid.uuid4()), role=Role.user,
                parts=[Part(root=TextPart(text=text))])):
            pass

async def serial():
    t0 = time.time()
    for a, q in WORK: await call(a, q)
    return time.time() - t0

async def parallel():
    t0 = time.time()
    await asyncio.gather(*(call(a, q) for a, q in WORK))
    return time.time() - t0

async def main():
    import statistics as st
    res = {"serial": [], "parallel": []}
    for i in range(3):
        res["serial"].append(await serial())
        print(f"  serial   run{i+1}: {res['serial'][-1]:5.1f}s", flush=True)
        await asyncio.sleep(2)
        res["parallel"].append(await parallel())
        print(f"  parallel run{i+1}: {res['parallel'][-1]:5.1f}s", flush=True)
        await asyncio.sleep(2)
    a, b = st.fmean(res["serial"]), st.fmean(res["parallel"])
    print(f"\nserial   {a:5.1f}s")
    print(f"parallel {b:5.1f}s")
    print(f"delta    {b-a:+5.1f}s  ({(b-a)/a*100:+.0f}%)")

asyncio.run(main())
