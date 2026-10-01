#!/usr/bin/env python3
"""Smoke test the deployed A2A sample agents.

For each entry in ``deployed-state.json``:
  1. Sign in as the admin user and take its idToken — the only credential an A2A
     hop carries (the grant is its ``cognito:groups`` claim).
  2. Open the Runtime invocation URL as the A2A endpoint.
  3. Fetch ``/.well-known/agent-card.json`` and print name + skills.
  4. Send one ``message/send`` with a known prompt; assert reply contains
     the agent's marker token.

Then a set of negative cases, each a way authorization could fail open.
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
    # These have real tools, so the prompts ask them to READ rather than write
    # where possible: a smoke test should not leave the user's devices in a
    # different state than it found them.
    "device-control-agent": (
        "What is the living room environment sensor reading right now?",
        "⟦A2A:device-control⟧",
    ),
    # Lighting is the exception — this agent exists to change the lights, and a
    # read-only prompt would not exercise the path that matters. It picks a mild
    # effect on the TV backlight, which also covers segment truncation (4 segments).
    "light-effect-agent": (
        "Give the TV backlight a calm ocean feel.",
        "⟦A2A:light-effect⟧",
    ),
    "knowledge-qa-agent": (
        "What animation modes does the LED matrix support?",
        "⟦A2A:knowledge-qa⟧",
    ),
    # Reads rather than writes: the smoke test should not leave a scene behind
    # that a scheduler would then start firing.
    "task-management-agent": (
        "What automations do I have saved?",
        "⟦A2A:task-management⟧",
    ),
    # Video rather than music, deliberately: the music path waits on the Bluetooth
    # link, which nothing is driving during a smoke test, so it would spend several
    # seconds polling and then correctly report that no speaker is paired. Video
    # sync has no such dependency — the backlight reads the picture itself — so this
    # exercises discover -> setSyncMode without a timing-dependent outcome.
    #
    # It DOES change device state, unlike the reads above. That is the point for
    # this agent (it has no read-only skill to test), and it is recoverable: the
    # user's own panel or a later command sets sync_mode back to off.
    "scene-sync-agent": (
        "I'm watching a film in the living room — make the lights follow the TV.",
        "⟦A2A:scene-sync⟧",
    ),
}

# Roster: see common/agents.py. A stale copy here makes the smoke test skip an
# agent silently, which is the opposite of what a smoke test is for.
sys.path.insert(0, str(HERE))
from common.agents import AGENT_LONG_NAMES as AGENT_SHORT_TO_LONG  # noqa: E402


def fetch_admin_tokens() -> tuple[str, str]:
    """Sign in as the admin user and return ``(idToken, accessToken)``.

    The idToken stands in for the chatbot's: it is what the orchestrator forwards.
    The access token is kept for a negative case — same pool, valid signature, but
    not a user identity, so both the authorizer and the container must refuse it.
    Returns ``("", "")`` if the credentials are not available.
    """
    outputs_path = HERE.parent / "cdk-outputs.json"
    if not outputs_path.exists():
        print(f"  note: {outputs_path} missing — skipping the user idToken")
        return "", ""
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
        result = resp["AuthenticationResult"]
        return result["IdToken"], result.get("AccessToken", "")
    except Exception as exc:  # noqa: BLE001
        print(f"  note: could not fetch a user idToken ({exc}) — every agent "
              f"will refuse")
        return "", ""


def fetch_user_id_token() -> str:
    """The admin user's idToken, or "" — see `fetch_admin_tokens`."""
    return fetch_admin_tokens()[0]


def _token_groups(token: str) -> list:
    """The `cognito:groups` claim of a JWT, without verifying it.

    Report-and-test-selection only; the authoritative check is the Runtime
    authorizer.
    """
    import base64

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return []
    groups = claims.get("cognito:groups") or []
    return [groups] if isinstance(groups, str) else list(groups)


def fetch_ungranted_user_token() -> str:
    """A valid idToken for a user holding NO A2A grant.

    The most important negative case: a legitimate signed-in user must not reach a
    specialist they were not granted. Uses a dedicated account so the test never
    depends on the demo users' grant state, and so running it cannot change anyone
    else's access.

    Returns "" when the account cannot be provisioned, and the caller skips rather
    than silently passing — a skipped negative test is honest, a missing one is not.
    """
    outputs_path = HERE.parent / "cdk-outputs.json"
    if not outputs_path.exists():
        return ""
    out = json.loads(outputs_path.read_text())
    out = out[next(iter(out))]
    email = "smoke-ungranted@smarthome.local"
    password = "SmokeTest#Ungranted1"
    region = out["UserPoolId"].split("_")[0]
    idp = boto3.client("cognito-idp", region_name=region)
    try:
        try:
            idp.admin_create_user(
                UserPoolId=out["UserPoolId"], Username=email,
                UserAttributes=[{"Name": "email", "Value": email},
                                {"Name": "email_verified", "Value": "true"}],
                MessageAction="SUPPRESS")
        except idp.exceptions.UsernameExistsException:
            pass
        idp.admin_set_user_password(
            UserPoolId=out["UserPoolId"], Username=email,
            Password=password, Permanent=True)
        # Strip any a2a group a previous global grant may have added, or this user
        # would not be grantless and the test would silently become a positive one.
        for group in idp.admin_list_groups_for_user(
                UserPoolId=out["UserPoolId"], Username=email).get("Groups", []):
            name = group.get("GroupName", "")
            if name.startswith("a2a-"):
                idp.admin_remove_user_from_group(
                    UserPoolId=out["UserPoolId"], Username=email, GroupName=name)
        resp = idp.initiate_auth(
            ClientId=out["UserPoolClientId"], AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password})
        token = resp["AuthenticationResult"]["IdToken"]
        if _token_groups(token):
            print(f"  note: {email} still carries groups {_token_groups(token)}; "
                  f"the grantless negative case would not be valid")
            return ""
        return token
    except Exception as exc:  # noqa: BLE001
        print(f"  note: could not provision a grantless user ({exc})")
        return ""


def _granted_skills_from_token(token: str, agent_card_name: str) -> set:
    """The skills this token grants on this agent, read from `cognito:groups`.

    Only for the report line — the authoritative check is the Runtime authorizer,
    which refuses before the container is reached. Printing it makes a refusal
    diagnosable: "0 granted" explains a 403 that would otherwise look like an outage.
    """
    import base64

    from common import a2a_groups  # type: ignore

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return set()
    return set(a2a_groups.skills_for_agent(
        claims.get("cognito:groups") or [], agent_card_name))


def _card_skill_ids(agent: str) -> list[str]:
    """Read the skill ids this agent publishes straight from its card.json."""
    card_path = HERE / agent / "card.json"
    card = json.loads(card_path.read_text(encoding="utf-8"))
    return [s["id"] for s in (card.get("skills") or []) if s.get("id")]


async def smoke_one(entry: dict, user_token: str) -> bool:
    agent_long = AGENT_SHORT_TO_LONG[entry["agent"]]
    if agent_long not in PROMPTS:
        # A new agent added to the roster without a probe here would otherwise
        # crash the whole run with a KeyError, taking the other agents' results
        # with it — the report would look like a total outage.
        print(f"\n=== {agent_long} ===")
        print(f"  FAIL: no smoke-test prompt for {agent_long}; add one to PROMPTS")
        return False
    prompt, marker = PROMPTS[agent_long]
    invocation_url = entry["invocationUrl"]
    # The invocation URL ends with /invocations; A2A expects the endpoint root.
    # AgentCore Runtime proxies POST /invocations to the container's POST /.
    # The a2a-sdk client resolver fetches /.well-known/agent-card.json which
    # the container exposes — AgentCore's edge passes that GET through.
    endpoint = invocation_url.rsplit("/invocations", 1)[0]

    # The end user's own token is the ONLY credential now: the Runtime authorizer
    # validates it and matches its `cognito:groups` claim against this agent's grant
    # groups, and the server derives the skill set from the same claim.
    headers = {"Authorization": f"Bearer {user_token}"}
    print(f"\n=== {agent_long} ===")
    print(f"  endpoint: {endpoint}")
    print(f"  granted skills (from the token claim): "
          f"{sorted(_granted_skills_from_token(user_token, agent_long))}")
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


async def expect_refusal(entry: dict, token: str, label: str) -> bool:
    """Send a request that SHOULD be refused and report whether it was.

    This is the half of the smoke test that proves authorization exists. A passing
    positive test says nothing about it: an agent that authorizes NOTHING answers
    every positive probe perfectly.

    Two layers can refuse, and both count:
      - the Runtime's `customJWTAuthorizer.customClaims`, which rejects a token whose
        `cognito:groups` holds no grant on this agent, before the container is
        reached. That arrives as a transport-level 403.
      - the container, which derives the skill set from the same claim and refuses
        with a readable "Request refused: ..." reply.
    """
    invocation_url = entry["invocationUrl"]
    headers = {"Authorization": f"Bearer {token}"}

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
            if not refused:
                print("  !! NOT REFUSED — this token was authorized when it should "
                      "not have been")
            print(f"  reply: {(reply or '(none)')[:220]}")
            return refused
        except Exception as e:
            # A transport-level rejection also counts as a refusal.
            print(f"  refused at transport: {e}")
            return True


async def main() -> int:
    state = json.loads(DEPLOYED_STATE.read_text())

    # Every agent's Runtime authorizer admits only an end user's idToken whose
    # `cognito:groups` holds a grant on it, so the smoke test presents one the way
    # the orchestrator does. Sign in as the admin from cdk-outputs.json.
    user_token, access_token = fetch_admin_tokens()
    print(f"fetched user idToken ({len(user_token) if user_token else 0} chars)")
    if not user_token:
        print("cannot smoke test without a user idToken — every agent would refuse")
        return 2

    # A positive probe against an agent this user holds no grant on is not a test:
    # the authorizer refuses before the container, which is CORRECT behaviour and
    # would be reported as a failure. Skip it and say so, rather than turning a
    # working authorization boundary into six red lines.
    results = {}
    skipped = []
    for entry in state["agents"]:
        agent_long = AGENT_SHORT_TO_LONG[entry["agent"]]
        if user_token and not _granted_skills_from_token(user_token, agent_long):
            skipped.append(entry["agent"])
            print(f"\n=== {agent_long} ===")
            print("  SKIPPED: this user holds no grant on this agent, so a positive "
                  "probe would only re-test the authorizer's refusal. Grant it on "
                  "Build -> SubAgent Policy and sign in again to include it.")
            continue
        results[entry["agent"]] = await smoke_one(entry, user_token)

    if skipped:
        print(f"\nnote: {len(skipped)} agent(s) skipped for lack of a grant: "
              f"{skipped}")

    # ---- negative cases ----------------------------------------------------
    # Each of these is a way authorization can fail OPEN, which a positive probe
    # cannot detect: an agent that authorizes nothing answers every positive probe
    # perfectly.
    if state["agents"]:
        probe = state["agents"][0]

        # 1. A token from the right pool that is not a user identity. The admin's
        #    ACCESS token is signed by the same issuer, but it has no `aud` (so the
        #    authorizer's allowedAudience refuses it) and `token_use` is "access" (so
        #    the container would refuse it too). If this passes, something other than
        #    an end user's idToken can reach a specialist.
        if access_token:
            results["negative:non-id-token-rejected"] = await expect_refusal(
                probe, access_token, "access token from the same pool (not an idToken)")
        else:
            print("\n--- negative: access token — SKIPPED (sign-in returned none) ---")

        #    It replaces the pre-migration check that the old m2m service token was
        #    refused; that token type is no longer minted anywhere.

        # 2. A real, valid user token belonging to someone with NO grant on any
        #    agent. This is the case that matters most: a legitimate user must not
        #    reach a specialist they were not granted.
        ungranted = fetch_ungranted_user_token()
        if ungranted:
            results["negative:valid-user-without-a-grant"] = await expect_refusal(
                probe, ungranted, "valid user token with no grant")
        else:
            print("\n--- negative: valid user with no grant — SKIPPED "
                  "(could not provision a grantless user) ---")

        # 3. Cross-agent: a token granted on agent A must not open agent B. Only
        #    meaningful when the two agents have genuinely different grants, which is
        #    why it looks for an agent the user holds nothing on.
        from common import a2a_groups  # type: ignore

        claims_groups = _token_groups(user_token)
        held = set(a2a_groups.grants_from_claim(claims_groups))
        other = next(
            (e for e in state["agents"]
             if AGENT_SHORT_TO_LONG[e["agent"]] not in held), None)
        if other is not None:
            results["negative:cross-agent-grant"] = await expect_refusal(
                other, user_token,
                f"granted elsewhere but not on {AGENT_SHORT_TO_LONG[other['agent']]}")
        else:
            print("\n--- negative: cross-agent — SKIPPED (this user is granted "
                  "on every deployed agent, so there is no negative to test) ---")

    print()
    print("summary:", results)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
