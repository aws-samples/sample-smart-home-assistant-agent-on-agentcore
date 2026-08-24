"""The machine-readable contract a third party needs to plug an A2A agent in here.

One document answering "what do I have to know about your deployment", so nobody has
to hand-copy a pool id out of a chat message. Served at
`GET /registry/records?action=a2a-manifest` and shown in Admin Console ->
Integration Registry -> A2A Agents -> "Platform manifest".

Generated, never written by hand
--------------------------------
Every rule in here is READ OUT of the module that enforces it:

  - group shapes and the name pattern      `a2a_groups`
  - claim name / value type / operator     `a2a_conformance` (the same constants
                                           `check` compares against)
  - the authorizer template                `a2a_conformance.authorizer_for`, on a
                                           placeholder card
  - the session-propagation channel        `a2a_session`

That indirection is the whole point. A manifest retyped from the docs is a second
definition of an authorization boundary, and it drifts silently — the failure is a
third party configuring exactly what we published and still being refused. If it is
generated, "what we publish" and "what we check" cannot disagree.

Deployment-specific identifiers (pool, app client, region, registry, gateway) are
PASSED IN by the caller, which reads them from its own environment. This module does
no I/O, so the rule is testable without AWS.

Not a secret
------------
Everything here is public: the discovery URL, the app client id and the group naming
are all visible in the browser apps' own bundles and in any issued token. The route
is still behind admin auth, because "who is integrating with us" is not public even
when the values are.
"""

from __future__ import annotations

import a2a_conformance as conf
import a2a_groups
import a2a_session
import agent_registry as registry_ns

# Bumped when a consumer would have to change. Additive fields do not bump it; a
# renamed or removed field, or a changed rule, does. A third party's CI can pin it and
# fail loudly rather than silently reading a field that stopped meaning what it meant.
MANIFEST_VERSION = "1.0"

# The placeholder a template's `{cardName}` / `{skillId}` stand in for. Chosen to be
# obviously fake AND group-name safe, so it survives `authorizer_for` unharmed.
_PLACEHOLDER_CARD = "YOUR-AGENT-NAME"
_PLACEHOLDER_SKILL = "YOUR_SKILL_ID"

DOCS = {
    "onboarding": "docs/a2a-agent-onboarding.md",
    "runbook": "docs/agentcore-deploy-runbook.md",
    "contractGenerator": "scripts/a2a-authorizer-contract.py",
    # Offline, and the only one of these an agent team can run without access to this
    # account: it takes THIS DOCUMENT plus their card and answers before anything is
    # deployed. `shared/tests/test_manifest_docs_paths_exist.py` exists because this key
    # once pointed at a script that had not been written, and a dangling path in the
    # contract is worse than an absent entry — the reader cannot tell "not built yet"
    # from "you gave me the wrong path".
    "preflight": "scripts/a2a-preflight.py",
    "smokeTest": "scripts/a2a-delegation-smoke.py",
}


def _templated(value: str) -> str:
    """Swap the safe placeholders back out for `{cardName}` / `{skillId}`."""
    return (value.replace(_PLACEHOLDER_CARD, "{cardName}")
                 .replace(_PLACEHOLDER_SKILL, "{skillId}"))


# The key an AgentCard's `securitySchemes` map uses for this deployment's credential.
# Named for what it IS rather than for the protocol it rides on: `oauth2` was the old
# key and it described a client_credentials flow that no longer exists here, which is
# exactly the confusion this name avoids.
SECURITY_SCHEME_NAME = "endUserIdToken"


def card_security(*, discovery_url: str, door_group: str) -> tuple[dict, list]:
    """`(securitySchemes, security)` an AgentCard here should declare.

    This is the ONE part of the platform contract that belongs in each agent's own
    card rather than only in this document, because `securitySchemes` is an A2A
    protocol field: a caller reads it from the card to decide what credential to send.
    Published here as a template so nobody has to invent it, and so a card that
    declares something else is detectable (`a2a_preflight` reports it).

    `openIdConnect` rather than `http`/`bearer`, though both would be true. The OIDC
    form names the ISSUER, which is the thing a caller actually has to get right —
    it is the same discovery URL the Runtime authorizer validates against, so a token
    from anywhere else is refused at the door.

    `security` carries an EMPTY scope list on purpose. Authorization here is not a
    scope: it is the `cognito:groups` claim, and A2A has no field for "the caller must
    hold group X". Putting the group in a scopes array would make it look like
    something an OAuth server would issue on request, which is the opposite of the
    truth — an administrator grants it. So the requirement is stated in the
    description, and the machine-readable rule stays in `groups.doorGroup`.
    """
    return (
        {SECURITY_SCHEME_NAME: {
            "type": "openIdConnect",
            "openIdConnectUrl": discovery_url,
            "description": (
                "Send the END USER's own OIDC id token as "
                "`Authorization: Bearer <token>`. There is no machine-to-machine "
                "credential and no second header. Authorization is the "
                f"`{conf.CLAIM_NAME}` claim on that same token: the caller must hold "
                f"`{door_group}`, which a platform administrator grants. A token from "
                "any other issuer is refused before your container is reached."),
        }},
        [{SECURITY_SCHEME_NAME: []}],
    )


def build(*, region: str, registry_id: str, user_pool_id: str, app_client_id: str,
          discovery_url: str, gateway_url: str = "",
          grant_grace_seconds: int = registry_ns.DEFAULT_GRANT_GRACE_SECONDS) -> dict:
    """The manifest, as a JSON-serialisable dict.

    `gateway_url` is optional and omitted when empty: routing through the A2A gateway
    is opt-in, and publishing a blank field would read like a broken deployment rather
    than an unused option.
    """
    placeholder_card = {
        "name": _PLACEHOLDER_CARD,
        "skills": [{"id": _PLACEHOLDER_SKILL}],
    }
    # Straight from the generator the console's conformance check compares against.
    authorizer_template = conf.authorizer_for(
        placeholder_card, discovery_url, app_client_id)
    door_group = a2a_groups.agent_group_name(_PLACEHOLDER_CARD)
    skill_group = a2a_groups.group_name(_PLACEHOLDER_CARD, _PLACEHOLDER_SKILL)
    card_schemes, card_sec = card_security(
        discovery_url=discovery_url, door_group=_templated(door_group))

    manifest: dict = {
        "manifestVersion": MANIFEST_VERSION,

        "deployment": {
            "region": region,
            "registryId": registry_id,
            "recordType": "AGENT",
            "userPoolId": user_pool_id,
        },

        # ---- what you must configure on YOUR runtime -----------------------
        "authorizer": {
            "type": "customJWTAuthorizer",
            "discoveryUrl": discovery_url,
            # Named explicitly because the wrong one of these is the single most
            # common silent failure: `allowedClients` validates `client_id`, which
            # only an ACCESS token carries, while a Cognito idToken puts the app
            # client in `aud`. A fully granted user is then refused with a message
            # about client_id.
            "audienceField": "allowedAudience",
            "allowedAudience": [app_client_id],
            "claimName": conf.CLAIM_NAME,
            "claimValueType": conf.CLAIM_VALUE_TYPE,
            "claimMatchOperator": conf.CLAIM_OPERATOR,
            "matchValueTemplate": [_templated(door_group)],
            "stable": True,
            "stabilityNote": (
                "This list has ONE entry and does not change as your card gains or "
                "loses skills, so a card edit is not also a runtime redeploy. "
                "Renaming the card DOES change it — a rename is a different agent to "
                "every enforcement point and existing grants will not follow it."),
            "template": {"customJWTAuthorizer": {
                **{k: v for k, v in authorizer_template.items()
                   if k != "customClaims"},
                "customClaims": [{
                    **{k: v for k, v in authorizer_template["customClaims"][0].items()
                       if k != "authorizingClaimMatchValue"},
                    "authorizingClaimMatchValue": {
                        "claimMatchValue": {
                            "matchValueStringList": [_templated(door_group)]},
                        "claimMatchOperator": conf.CLAIM_OPERATOR,
                    },
                }],
            }},
        },

        # ---- the group naming both sides derive independently --------------
        "groups": {
            "prefix": a2a_groups.GROUP_PREFIX,
            "separator": a2a_groups.SEPARATOR,
            "doorGroup": _templated(door_group),
            "doorGroupPurpose": (
                "What your Runtime authorizer matches. Answers 'may this caller reach "
                "this agent at all'."),
            "skillGroup": _templated(skill_group),
            "skillGroupPurpose": (
                "What YOUR CONTAINER reads out of the same signed claim to decide "
                "which skills the caller holds. Do NOT list these in the authorizer."),
            "namePattern": a2a_groups._SAFE.pattern,
            "maxLength": a2a_groups.MAX_GROUP_NAME,
            "note": (
                "A card name or skill id outside namePattern cannot be encoded as a "
                "group, so grants on it are SILENTLY SKIPPED — the user reads as "
                "granted in the console and is refused at the door."),
        },

        # ---- what the Registry will accept --------------------------------
        "card": {
            "requiredFields": ["name", "description", "version", "url", "skills",
                               "capabilities", "defaultInputModes",
                               "defaultOutputModes", "securitySchemes"],
            "namePattern": a2a_groups._SAFE.pattern,
            "skillIdPattern": a2a_groups._SAFE.pattern,
            # Copy these two into your card verbatim. They are the only part of this
            # document that belongs in the card as well as here — see `card_security`.
            "securitySchemes": card_schemes,
            "security": card_sec,
            "securityNote": (
                "DECLARATIVE. It tells a caller what to send; it does not enforce "
                "anything, and nothing on this platform reads it to make an "
                "authorization decision — your Runtime authorizer does that. So "
                "declaring a scheme does not protect your agent, and declaring the "
                "wrong one does not lock it: it strands every caller who believes "
                "you. Keep it true."),
            "urlNote": (
                "Your Runtime's invocations URL, or a gateway target URL. This is what "
                "the conformance check resolves back to a runtime to read its "
                "authorizer."),
            "rejectionNote": (
                "An incomplete card is rejected as 'does not match any supported "
                "version', which sounds like a version problem and is a completeness "
                "problem."),
        },

        # ---- how a delegated request arrives -----------------------------
        "invocation": {
            "protocol": "A2A",
            "method": "message/send",
            "authorization": (
                "Bearer <the END USER's own Cognito idToken>. There is no m2m token "
                "and no second header; the grant is the cognito:groups claim on that "
                "token."),
            "requestHeaderAllowlist": [
                "Authorization",
                "X-A2A-Allowed-Skills",
                "X-SuperApp-User-Token",
            ],
            "headerAllowlistNote": (
                "AgentCore's Runtime edge DROPS any header not on your runtime's "
                "allowlist, silently — the request arrives looking like one that chose "
                "not to send it. `Authorization` matters most: it is both the "
                "credential and the source of the grant claim."),
            "sessionIdMetadataKey": a2a_session.SESSION_ID_METADATA_KEY,
            "sessionIdNote": (
                "The orchestrator's session id travels in the A2A "
                f"`Message.metadata.{a2a_session.SESSION_ID_METADATA_KEY}`. Read it "
                "from there and log it, and your spans join the parent turn. It is "
                "ALSO sent as a platform session header, but no container can read "
                "that: the allowlist rejects `x-amzn-*` except the "
                "`X-Amzn-Bedrock-AgentCore-Runtime-Custom-` prefix."),
        },

        # ---- the lifecycle rules that decide whether you are callable -----
        "lifecycle": {
            "grantableStatuses": ["APPROVED"],
            "inFlightStatuses": sorted(registry_ns.IN_FLIGHT_STATUSES),
            "reapprovalGraceSeconds": grant_grace_seconds,
            "graceNote": (
                "Editing an APPROVED record knocks it back to DRAFT, which makes you "
                "undiscoverable until re-approved. Grants survive that window rather "
                "than being revoked, so an upgrade is downtime, not data loss."),
            "terminalStatuses": ["DEPRECATED"],
            "terminalNote": (
                "DEPRECATED is TERMINAL and the record then vanishes from the API "
                "entirely. Recovery means a new record with a NEW recordId, and grants "
                "are stored per recordId, so every grant is lost. Use `reject` to "
                "disable something reversibly."),
            "versioningNote": (
                "Upgrade in place (same recordId). A new record gets a new recordId, "
                "which orphans every existing grant while looking like success."),
        },

        "docs": DOCS,
    }

    if gateway_url:
        manifest["gateway"] = {
            "url": gateway_url,
            "optional": True,
            "targetUrlTemplate": f"{gateway_url.rstrip('/')}/{{targetName}}",
            "targetNameTemplate": "{cardName}",
            "note": (
                "Optional. Registering your own runtime URL works and is fully "
                "supported; it just sits outside this deployment's central A2A egress "
                "point, per-target audit and Cedar kill switch. A target is created "
                "for an APPROVED record automatically — point your card's `url` at "
                "targetUrlTemplate to route through it."),
        }

    return manifest
