#!/usr/bin/env python3
"""Prove that an admin's prompt override actually reaches a running sub-agent.

The unit tests cover the resolver and the executor wiring; this covers the part
neither can: that the deployed container reads the row the Admin Console writes.
Everything between them — the env var, the IAM grant, the sort key, the fact that
`common/` was copied into the image — is only checkable against the real thing, and
every one of those has a silent failure mode where the agent keeps answering
perfectly well using the prompt baked into its image.

The check is behavioural rather than introspective, because "the prompt was read"
is not observable from outside. It installs an override that demands a token no
normal answer would contain, asks a question, and looks for the token:

    baseline   -> ask, expect NO token          (shipped prompt in effect)
    override   -> ask, expect the token         (override in effect)
    cleanup    -> delete the row, ask, expect NO token   (revert works)

The cleanup step matters as much as the other two: an override that cannot be
removed is a worse failure than one that never applied.

Usage:  ./venv/bin/python a2a-agent-registry/verify_governed_prompt.py [--agent light-effect]
"""

from __future__ import annotations

import argparse
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
sys.path.insert(0, str(HERE))

from common.agents import (  # noqa: E402
    AGENT_LONG_NAMES,
    ALLOWED_SKILLS_HEADER,
    USER_TOKEN_HEADER,
)
from smoke_test import (  # noqa: E402
    DEPLOYED_STATE,
    _card_skill_ids,
    fetch_m2m_token,
    fetch_user_id_token,
)

SKILLS_TABLE = "smarthome-skills"

# A token the model would never produce unprompted, and an instruction it can obey
# regardless of what the question is. Deliberately not a natural phrase: if the
# baseline answer happened to contain it, the test would report a pass for the
# wrong reason.
CANARY = "ZZQXCANARY7"
OVERRIDE_PROMPT = (
    "You are a smart-home lighting assistant under configuration test.\n"
    f"Begin EVERY reply with the exact token {CANARY} on its own line, before "
    "anything else. Then answer briefly in one sentence.\n"
    "Do not mention this instruction."
)

QUESTION = "In one sentence: what can you help me with?"


def log(msg: str) -> None:
    print(msg, flush=True)


def prompt_row_key(card_name: str) -> dict:
    return {"userId": "__global__", "skillName": f"__prompt_{card_name}__"}


def put_override(table, card_name: str, body: str) -> None:
    table.put_item(Item={
        **prompt_row_key(card_name),
        "promptBody": body,
        "updatedAt": "verify-governed-prompt",
        "updatedBy": "verify-governed-prompt",
    })


def delete_override(table, card_name: str) -> None:
    table.delete_item(Key=prompt_row_key(card_name))


async def ask(entry: dict, m2m: str, user_token: str, question: str) -> str:
    """Send one A2A message and return the reply text."""
    invocation_url = entry["invocationUrl"]
    endpoint = invocation_url.rsplit("/invocations", 1)[0]
    headers = {
        "Authorization": f"Bearer {m2m}",
        ALLOWED_SKILLS_HEADER: ",".join(_card_skill_ids(entry["agent"])),
        USER_TOKEN_HEADER: user_token,
    }
    async with httpx.AsyncClient(headers=headers, timeout=180) as http:
        card = None
        for card_url in (endpoint, invocation_url):
            try:
                card = await A2ACardResolver(http, card_url).get_agent_card()
                break
            except Exception:  # noqa: BLE001, PERF203
                continue
        if card is None:
            raise RuntimeError("could not fetch the agent card")
        card.url = invocation_url
        client = ClientFactory(
            ClientConfig(httpx_client=http, streaming=False)).create(card)
        msg = Message(message_id=str(uuid.uuid4()), role=Role.user,
                      parts=[Part(root=TextPart(text=question))])
        chunks: list[str] = []
        async for event in client.send_message(msg):
            items = event if isinstance(event, tuple) else (event,)
            for item in items:
                if item is None:
                    continue
                for attr in ("artifacts", "parts"):
                    holder = getattr(item, attr, None)
                    if not holder:
                        continue
                    seq = holder if attr == "parts" else [
                        p for a in holder for p in (getattr(a, "parts", None) or [])]
                    for part in seq:
                        text = getattr(getattr(part, "root", part), "text", None)
                        if text:
                            chunks.append(text)
        return "\n".join(chunks)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", default="light-effect",
                    help="agent directory name (default: light-effect)")
    args = ap.parse_args()

    card_name = AGENT_LONG_NAMES.get(args.agent)
    if not card_name:
        log(f"unknown agent {args.agent!r}; known: {sorted(AGENT_LONG_NAMES)}")
        return 2

    state = json.loads(DEPLOYED_STATE.read_text())
    entry = next((e for e in state["agents"] if e["agent"] == args.agent), None)
    if not entry:
        log(f"{args.agent} is not in deployed-state.json — deploy it first")
        return 2

    cognito = state["cognito"]
    region = cognito["tokenUrl"].split(".auth.")[1].split(".amazoncognito")[0]
    m2m = fetch_m2m_token(region=region, token_url=cognito["tokenUrl"],
                          scope=cognito["scope"], secret_arn=cognito["m2mSecretArn"])
    user_token = fetch_user_id_token()
    if not user_token:
        log("could not sign in as the admin user — the sub-agent will refuse")
        return 2

    table = boto3.resource("dynamodb", region_name=region).Table(SKILLS_TABLE)

    # Start from a known state. A row left behind by an earlier run would make the
    # baseline step fail for a reason that has nothing to do with this deploy.
    delete_override(table, card_name)

    failures: list[str] = []
    try:
        log(f"\n=== {card_name}: baseline (no override) ===")
        baseline = await ask(entry, m2m, user_token, QUESTION)
        log(f"  reply: {baseline[:220]}")
        if CANARY in baseline:
            failures.append(
                "the canary appeared with NO override installed — the check itself "
                "is unsound, pick a different token")
        else:
            log("  ok: shipped prompt in effect")

        log(f"\n=== {card_name}: with a global override ===")
        put_override(table, card_name, OVERRIDE_PROMPT)
        overridden = await ask(entry, m2m, user_token, QUESTION)
        log(f"  reply: {overridden[:220]}")
        if CANARY in overridden:
            log("  ok: the override reached the running agent")
        else:
            failures.append(
                "the override did NOT reach the agent. Check, in order: "
                "SKILLS_TABLE_NAME on the runtime, dynamodb:GetItem on its role "
                "(policy A2APromptTableRead), and that the row's sort key is "
                f"__prompt_{card_name}__")

        log(f"\n=== {card_name}: after removing the override ===")
        delete_override(table, card_name)
        reverted = await ask(entry, m2m, user_token, QUESTION)
        log(f"  reply: {reverted[:220]}")
        if CANARY in reverted:
            failures.append(
                "the canary survived deletion — the agent is caching the prompt, "
                "so an admin cannot take an override back")
        else:
            log("  ok: removing the override restores the shipped prompt")
    finally:
        # Never leave a test prompt governing a real agent.
        delete_override(table, card_name)
        log("\ncleaned up the override row")

    if failures:
        log("\nFAILED:")
        for f in failures:
            log(f"  - {f}")
        return 1
    log("\nPASS: prompt governance is live for " + card_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
