"""Pre token generation trigger: GLOBAL A2A grants, injected into `cognito:groups`.

Why this Lambda exists
----------------------
A global grant means "every user may reach this sub-agent". Materialising that as
one Cognito group membership per user is a constant written a million times, and the
admin API already could not do it at the scale it has: a single `__global__` save
fans out to EVERY user (a list-groups plus per-group add/remove, plus a forced
sign-out for anyone narrowed), and its own comment records that 39 users serially
already blew past API Gateway's 29s ceiling.

Cognito's quotas make that a wall rather than a slope. Measured from the service
quota tables, not from memory:

  - `AdminAddUserToGroup` / `AdminRemoveUserFromGroup` / `AdminUserGlobalSignOut`
    share the `UserUpdate` category at **25 RPS, account-wide per Region, NOT
    adjustable**. One million users is ~11 hours of nothing but this, competing with
    every other admin write.
  - **100 groups per user**, not adjustable. Per-user grants can live within it;
    global ones would spend it on a value that is the same for everybody.
  - Changing a user's group membership marks that user a monthly active user, so a
    full re-materialisation bills a million MAUs.

So global grants are not stored as memberships at all. They are computed here, at
token issue, and written into the `cognito:groups` claim — the same claim each
sub-agent Runtime's authorizer matches with `CONTAINS_ANY`, and the same claim the
orchestrator reads to decide which `a2a_*` tools to offer. Neither of those knows or
cares whether a group in the claim is backed by a membership.

Per-user grants stay real memberships. They are the minority, they need
`AdminUserGlobalSignOut` to take effect immediately, and keeping them in Cognito
keeps `ListUsersInGroup` working as the reverse lookup for "who can reach this
agent".

Three ways this could break sign-in, and what is done about each
---------------------------------------------------------------
1. **Raising.** A pre-token-generation trigger that throws fails authentication.
   Every path here is wrapped; the fallback returns the event untouched, which is
   exactly equivalent to this Lambda not being installed.
2. **Replacing the claim.** `groupsToOverride` is a REPLACE, not a merge. The
   incoming `request.groupConfiguration.groupsToOverride` is the user's real
   membership list, and it is carried back verbatim except for `a2a-` names. Drop
   that and one sign-in silently removes `admin` from an administrator.
3. **Depending on the Registry.** The grantability filter needs the record catalog.
   On a catalog failure the last successfully cached catalog is used instead of the
   filter, and the failure is logged at ERROR: the orchestrator applies its own
   APPROVED check when building tools, so a momentary Registry blip degrades to "no
   tool is offered" rather than to "every user loses every specialist until Registry
   recovers".

   A container whose FIRST catalog read fails has nothing cached, and there the
   trigger returns the event UNTOUCHED rather than injecting an empty list. That
   distinction is the whole safety property: this trigger REPLACES the claim, so
   answering "I could not find out" with [] strips every A2A grant from the token —
   including the per-user ones that are real Cognito memberships and have nothing to
   do with global grants. Measured on 2026-08-15, when a missing
   `agent-registry-control` service model in Lambda's bundled botocore did exactly
   that: sign-in worked perfectly and every specialist quietly became unreachable.

`A2A_CLAIM_INJECTION=off` disables the whole thing without a deploy.
"""

from __future__ import annotations

import logging
import os
import time

import boto3

import a2a_groups
import agent_registry as registry_ns
import subagent_policy

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SKILLS_TABLE_NAME = os.environ.get("SKILLS_TABLE_NAME", "")
REGISTRY_ID = os.environ.get("REGISTRY_ID", "")
GLOBAL_SCOPE = subagent_policy.GLOBAL_SCOPE

# Cached across invocations on purpose: a warm container answers sign-ins without a
# Registry round trip. The bucket forces a miss at least once a minute, which is the
# same 60s staleness the orchestrator's own card cache accepts (`tools/a2a.py`).
_CATALOG_TTL_SECONDS = 60

_table = None
_registry = None


def _skills_table():
    global _table
    if _table is None:
        _table = boto3.resource(
            "dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2")
        ).Table(SKILLS_TABLE_NAME)
    return _table


def _registry_client():
    global _registry
    if _registry is None:
        _registry = registry_ns.registry_client(
            os.environ.get("AWS_REGION", "us-west-2"))
    return _registry


_catalog_cache: dict = {"bucket": None, "names": {}}


class CatalogUnavailable(RuntimeError):
    """The Registry could not be read, so no `a2a-` decision can be made at all.

    Distinguished from "this user is entitled to nothing" because the two must
    produce opposite behaviour. Answering an unreadable catalog with an empty group
    list looks identical to a legitimate revocation, and this trigger REPLACES the
    claim — so it would strip every A2A grant from every user's token, including the
    per-user ones that are real Cognito memberships and nothing to do with this
    Lambda. Measured on 2026-08-15: exactly that happened, and sign-in kept working
    perfectly while every specialist quietly became unreachable.
    """



def _grantable_record_names() -> dict[str, str]:
    """{recordId: AgentCard name} for every record whose skills may be granted.

    Raises on a Registry failure so the caller can tell "nothing is grantable" from
    "I could not find out" — those are the same value otherwise, and the difference
    decides whether a user keeps their specialists.

    Also raises when the registry reports ZERO agent records. An empty catalog and a
    half-provisioned one are the same value too, and the same guard exists in the
    admin API's sweep for the same reason. An empty GRANTABLE set out of a non-empty
    catalog is a real decision and is returned normally: that is what a rejected
    record, a stale draft or a narrowed-to-nothing override each produce.
    """
    bucket = int(time.time() // _CATALOG_TTL_SECONDS)
    if _catalog_cache["bucket"] == bucket:
        return _catalog_cache["names"]

    client = _registry_client()
    now = time.time()
    grace = registry_ns.grant_grace_seconds()
    names: dict[str, str] = {}
    seen = 0
    token = None
    while True:
        kwargs = {
            "registryId": REGISTRY_ID,
            "maxResults": 50,
            "filters": [{"name": "recordType",
                         "values": [registry_ns.RECORD_TYPE_AGENT]}],
        }
        if token:
            kwargs["nextToken"] = token
        resp = client.list_registry_records(**kwargs)
        for record in resp.get("registryRecords", []):
            record_id = record.get("recordId", "")
            if not record_id:
                continue
            seen += 1
            ok, reason = registry_ns.grantable(record, now, grace)
            if not ok:
                logger.info("record %s is not grantable (%s); its groups will not "
                            "be injected", record_id, reason)
                continue
            # The card name, because that is what a group name encodes and what the
            # sub-agent's authorizer was configured with. One extra GetRegistryRecord
            # per record per minute per container.
            try:
                detail = client.get_registry_record(
                    registryId=REGISTRY_ID, recordId=record_id)
                card_raw = registry_ns.read_agent_card(detail)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not read the card for %s: %s", record_id, exc)
                continue
            if not card_raw:
                continue
            import json

            try:
                card_name = (json.loads(card_raw) or {}).get("name") or ""
            except ValueError:
                card_name = ""
            if card_name:
                names[record_id] = card_name
        token = resp.get("nextToken")
        if not token:
            break

    if not seen:
        raise CatalogUnavailable("the registry reported zero AGENT records")

    _catalog_cache["bucket"] = bucket
    _catalog_cache["names"] = names
    return names


def _last_known_record_names() -> dict[str, str]:
    """The last catalog this container read successfully, or {}.

    The fallback when the Registry cannot be reached. `{}` on a cold container is
    the accepted limit of it — see the module docstring — and means no `a2a-` group
    is injected on that token rather than that the wrong ones are.
    """
    return _catalog_cache.get("names") or {}


def a2a_groups_for(email: str) -> list[str]:
    """The `a2a-` group names this user's grant INTENT entitles them to.

    Global merged with the user's own override, per-user REPLACING global per
    sub-agent — `subagent_policy.effective_grants`, the same function the admin API
    materialises with. Two implementations of that merge would not fail loudly; one
    class of user would simply end up with more access than an admin granted.

    Raises `CatalogUnavailable` when the record catalog cannot be resolved. It does
    NOT return [] in that case — see that class.
    """
    table = _skills_table()
    global_intent = subagent_policy.read_intent(table, GLOBAL_SCOPE)
    user_intent = subagent_policy.read_intent(table, email) if email else {}
    effective = subagent_policy.effective_grants(global_intent, user_intent)
    if not effective:
        return []

    try:
        names = _grantable_record_names()
    except Exception as exc:  # noqa: BLE001
        names = _last_known_record_names()
        if not names:
            raise CatalogUnavailable(str(exc)) from exc
        # A warm container can still answer from the last catalog it read. Loud,
        # because a claim built without a fresh grantability check is a state
        # someone must be able to see.
        logger.error("REGISTRY UNAVAILABLE while building the a2a claim for %s (%s) "
                     "— falling back to the last catalog this container read",
                     email, exc)

    # An empty result here is a DECISION, not a failure: every granted record was
    # rejected, deprecated, past its window, or narrowed to nothing. The
    # "cannot decide" cases raise above, before this point.
    return sorted(subagent_policy.wanted_groups(effective, names))


def handler(event, _context=None):
    """Cognito pre-token-generation V1_0. Never raises.

    Returns the event with `response.claimsOverrideDetails.groupOverrideDetails`
    set. Fires on sign-in AND on `TokenGeneration_RefreshTokens`, so a change to
    global intent — or a record leaving the grantable set — takes effect on the
    user's next token rather than needing a sign-out no one could afford at scale.
    """
    try:
        if os.environ.get("A2A_CLAIM_INJECTION", "on").strip().lower() == "off":
            logger.info("A2A_CLAIM_INJECTION=off — leaving the claim untouched")
            return event

        request = event.get("request") or {}
        email = ((request.get("userAttributes") or {}).get("email") or "").strip()
        # The user's REAL memberships, which is what `groupsToOverride` replaces.
        existing = list(
            (request.get("groupConfiguration") or {}).get("groupsToOverride") or [])

        # Everything that is not ours is carried back verbatim. Dropping this line
        # would have one sign-in remove `admin` from an administrator.
        preserved = [g for g in existing
                     if not str(g).startswith(a2a_groups.GROUP_PREFIX)]

        try:
            injected = a2a_groups_for(email)
        except CatalogUnavailable as exc:
            # Leave the claim ALONE. Not "inject nothing" — returning early here is
            # what makes the promise at the top of this file true: the user holds
            # exactly what their real memberships give them, which is what they
            # would hold if this trigger were not installed.
            logger.error("cannot decide a2a groups for %s (%s) — leaving the claim "
                         "untouched so real memberships still apply", email, exc)
            return event


        merged = preserved + [g for g in injected if g not in preserved]
        logger.info("claim for %s: %d preserved + %d a2a group(s)",
                    email or "<no email>", len(preserved), len(injected))

        response_block = event.setdefault("response", {})
        override = response_block.setdefault("claimsOverrideDetails", {}) or {}
        response_block["claimsOverrideDetails"] = override
        group_override = override.setdefault("groupOverrideDetails", {}) or {}
        override["groupOverrideDetails"] = group_override
        group_override["groupsToOverride"] = merged
        # Carried through untouched. These are only meaningful for identity-pool
        # role selection, and a group override that silently blanked them would
        # change which IAM role a federated user requests.
        incoming_roles = (request.get("groupConfiguration") or {}).get(
            "iamRolesToOverride")
        if incoming_roles is not None:
            group_override["iamRolesToOverride"] = list(incoming_roles)
        preferred = (request.get("groupConfiguration") or {}).get("preferredRole")
        if preferred:
            group_override["preferredRole"] = preferred

        return event
    except Exception as exc:  # noqa: BLE001
        # A raise here fails AUTHENTICATION. Returning the event untouched is
        # exactly equivalent to this trigger not being installed: the user signs in
        # and holds whatever their real memberships give them.
        logger.exception("pre-token-generation failed; leaving the claim "
                         "untouched (%s)", exc)
        return event
