#!/usr/bin/env python3
"""Measure where the orchestrator actually routes each kind of request.

Retuning the delegation prompt needs evidence, and "the answer looked right" is
not evidence — an orchestrator answering a security question from its own
knowledge produces confident text that reads exactly like a delegated reply. This
reads the runtime's own `gen_ai.tool.name` spans, which record which tool actually
ran.

Three things this got wrong before it worked, all of which made correct behaviour
look broken:

  - `userId` in the payload becomes `actor_id`, and A2A grants are keyed on it.
    Omit it and it defaults to "default", which load_user_a2a_permissions skips —
    so no a2a_* tool is registered at all and every delegation "fails".
  - That key is the EMAIL, not the sub. The chatbot sends `email or username or
    sub` and the Admin Console resolves sub -> email when writing grants.
  - The ⟦A2A:…⟧ marker is on the SUB-AGENT's reply. The orchestrator summarises
    rather than pasting it through, so a correctly delegated turn usually shows no
    marker. Scanning the reply text reported six false misses.

Invoked over HTTPS with SigV4 rather than boto3, because the user's idToken
travels in X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken and
InvokeAgentRuntime models no parameter for it. Without that header the agent has
no identity to open the Gateway with, every tool call 401s, and the runtime
answers a bare 500.

Usage:  ./venv/bin/python scripts/probe-routing.py
"""
import json, re, sys, time, urllib.parse, uuid
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = "us-west-2"
st = json.load(open("agentcore-state.json"))
out = json.load(open("cdk-outputs.json")); o = out[next(iter(out))]

cog = boto3.client("cognito-idp", region_name=REGION)
tok = cog.initiate_auth(ClientId=o["UserPoolClientId"], AuthFlow="USER_PASSWORD_AUTH",
    AuthParameters={"USERNAME": o["AdminUsername"], "PASSWORD": o["AdminPassword"]}
)["AuthenticationResult"]["IdToken"]

import base64
claims = json.loads(base64.urlsafe_b64decode(
    tok.split(".")[1] + "=" * (-len(tok.split(".")[1]) % 4)))
# The chatbot sends `email or cognito:username or sub`, and the Admin Console
# resolves sub -> email when writing grants, so the row key is the EMAIL. Sending
# the sub here reads a row that does not exist and registers no a2a_* tool at all
# — the probe's first two runs measured that and looked like six routing bugs.
SUB = claims.get("email") or claims["sub"]

arn = st["runtimeArn"]
url = (f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/"
       f"{urllib.parse.quote(arn, safe='')}/invocations?qualifier=DEFAULT")

PROMPTS = [
    ("fast:power",     "Turn on the living room fan"),
    ("fast:state",     "Is the bedroom light on right now?"),
    ("fast:sensor",    "What is the temperature right now?"),
    ("fast:history",   "What was the average humidity over the last 6 hours?"),
    ("fast:nav",       "Open the automation page"),
    ("deleg:kb",       "What animation modes does the LED matrix support?"),
    ("deleg:light",    "Give the light strip a calm ocean feel"),
    ("deleg:scene",    "Every night at 22:30 turn the LED matrix off"),
    ("deleg:security", "What is my biggest smart-home security gap?"),
    ("deleg:energy",   "How much could I save by dimming the LEDs at night?"),
    ("deleg:maint",    "When should I replace the air purifier filter?"),
]

# Two signals, because the marker alone is not evidence. The sub-agent puts
# ⟦A2A:<domain>⟧ on ITS reply, but the orchestrator summarises rather than pasting
# that text through, so a delegated answer often carries no marker at all. The
# orchestrator is separately instructed to NAME the specialist it used, so an
# `a2a_..._agent` mention is the second signal. Measuring on the marker alone
# reported six false MISSes.
MARKER = re.compile(r"⟦A2A:([a-z-]+)⟧")
NAMED = re.compile(r"a2a[_ ]([a-z]+(?:[_ ][a-z]+)*?)[_ ]agent")
creds = boto3.Session().get_credentials().get_frozen_credentials()
import urllib.request

results = []
for label, prompt in PROMPTS:
    # A fresh session per prompt: a shared one lets an earlier answer bias the
    # next routing decision, which is the opposite of measuring each intent.
    sid = f"p5-{uuid.uuid4().hex}"[:40] + "0" * 24
    # `userId` is what becomes actor_id, and A2A grants are keyed on it. Omitting
    # it defaults to "default", which load_user_a2a_permissions skips outright — so
    # NO a2a_* tool is registered and every delegation "fails" for a reason that
    # has nothing to do with the prompt. The first run of this probe measured
    # exactly that and looked like six routing bugs.
    body = json.dumps({"prompt": prompt, "userId": SUB}).encode()
    req = AWSRequest(method="POST", url=url, data=body, headers={
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sid[:64],
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken": tok,
    })
    SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(req)
    t0 = time.time()
    try:
        r = urllib.request.Request(url, data=body, headers=dict(req.headers))
        with urllib.request.urlopen(r, timeout=180) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception as exc:
        detail = exc.read().decode()[:120] if hasattr(exc, "read") else str(exc)[:120]
        print(f"{label:16} ERROR {type(exc).__name__} {detail}", flush=True)
        continue
    dt = time.time() - t0
    m = MARKER.search(text)
    n = NAMED.search(text)
    who = (m.group(1) if m else
           n.group(1).replace("_", "-") + "*" if n else "(orchestrator)")
    flat = re.sub(r"\s+", " ", text)
    print(f"{label:16} {dt:5.1f}s {who:22} {flat[-150:]}", flush=True)
    results.append((label, who, dt))

# The reply text is NOT evidence of routing: the sub-agent's ⟦A2A:…⟧ marker is on
# ITS reply and the orchestrator summarises rather than pasting it through, so a
# correctly delegated turn often shows no marker and no tool name. Read the
# runtime's own gen_ai.tool.name spans instead — the only direct record of which
# tool actually ran. Measuring on text reported six false MISSes twice over.
print("\n--- tool calls recorded by the runtime (last 10 min) ---")
logs = boto3.client("logs", region_name=REGION)
import time as _t
resp = logs.filter_log_events(
    logGroupName=f"/aws/bedrock-agentcore/runtimes/{arn.split('/')[-1]}-DEFAULT",
    startTime=int((_t.time() - 600) * 1000),
    filterPattern='"gen_ai.tool.name"',
)
called = {}
for ev in resp.get("events", []):
    for name in re.findall(r'"gen_ai\.tool\.name":"([^"]+)"', ev["message"]):
        called[name] = called.get(name, 0) + 1
for name, n in sorted(called.items(), key=lambda kv: -kv[1]):
    kind = "A2A " if name.startswith("a2a_") else "tool"
    print(f"  {kind} {n:3}  {name}")

delegated = sorted(n for n in called if n.startswith("a2a_"))
print(f"\n  {len(delegated)} specialist skill(s) invoked: {delegated}")
