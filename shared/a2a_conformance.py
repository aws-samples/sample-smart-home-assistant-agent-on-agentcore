"""Does a sub-agent's Runtime authorizer match the card it registered?

Registering an APPROVED AgentCard is enough to be DISCOVERED: the console lists it,
the orchestrator registers `a2a_*` tools for it, and the delegation prompt names it —
all derived from the Registry with no code change anywhere. It is NOT enough to be
callable. Authorization happens at the sub-agent's own Runtime, whose
`customJWTAuthorizer` has to be configured against this deployment's Cognito pool and
has to enumerate this card's grant groups. That configuration lives with whoever
deployed the runtime, and nothing checked it until this module existed.

Both ways of getting it wrong are silent, and they fail in OPPOSITE directions:

  - Too loose (`customClaims` absent) — every authenticated user of the pool can
    reach the agent. There is no error anywhere; authorization is simply not
    happening. This is the dangerous one.
  - Too tight (wrong pool, wrong audience, a group the card does not declare) —
    granted users are refused at the door with 401. The orchestrator still registers
    the tool, because the card is APPROVED, so the model calls it, gets
    `A2A agent call failed: ...` and apologises.

Neither shows up on the Integration Registry page, which reads Registry STATUS. A
record can read `Reachable / approved` while nobody can call the agent, or while
everybody can.

Pure on purpose: it takes a card and an authorizer config and returns findings, so
the rule is testable without AWS and the same rule can back the console route, a CLI
check and the generator that prints the correct config.
"""

from __future__ import annotations

import a2a_groups

CLAIM_NAME = "cognito:groups"
CLAIM_OPERATOR = "CONTAINS_ANY"
CLAIM_VALUE_TYPE = "STRING_ARRAY"

# A finding's severity says which DIRECTION it fails in, because the two need
# opposite responses and an operator reading one line has to be able to tell.
#
#   OPEN   - more access than intended. Someone can reach the agent who should not.
#   CLOSED - less access than intended. Granted users are refused.
#   INFO   - neither, but worth stating.
OPEN = "open"
CLOSED = "closed"
INFO = "info"


def _finding(code: str, severity: str, detail: str) -> dict:
    return {"code": code, "severity": severity, "detail": detail}


def grantable_groups(card: dict) -> tuple[list[str], list[dict]]:
    """The per-skill groups an admin can hand out, plus findings about the card.

    These are what the CONTAINER reads to decide which skills a caller holds. They
    are NOT what the authorizer matches — see `expected_groups`.

    A card whose name or skill id is not group-name-encodable cannot be granted at
    all: `wanted_groups` skips it with a warning and the user ends up "granted" in
    DynamoDB and refused at the door. Reported as CLOSED rather than raising, so one
    bad card does not stop a sweep over all of them.
    """
    findings: list[dict] = []
    name = card.get("name") or ""
    if not name:
        return [], [_finding("card-no-name", CLOSED,
                             "the card has no name, so no group name can be derived")]
    skills = [s.get("id") or "" for s in (card.get("skills") or [])]
    if not any(skills):
        return [], [_finding("card-no-skills", CLOSED,
                             "the card declares no skill ids, so nothing can be granted")]

    groups: list[str] = []
    for skill in skills:
        try:
            groups.append(a2a_groups.group_name(name, skill))
        except a2a_groups.GroupNameError as exc:
            findings.append(_finding(
                "card-unencodable", CLOSED,
                f"skill {skill!r} cannot be encoded as a group name ({exc}); a grant "
                "on it is silently skipped"))
    return sorted(groups), findings


def expected_groups(card: dict) -> tuple[list[str], list[dict]]:
    """The CONTAINS_ANY list this card's Runtime authorizer should carry.

    One entry — `a2a-<cardName>` — and that is the point: it never changes as the
    card gains skills, so a card edit is not also a runtime redeploy. The findings
    still come from walking the skills, because a card that cannot be granted at all
    is worth reporting even though the door list does not name its skills.

    Kept as the name `check` and `authorizer_for` both use, so "what we hand out" and
    "what we check" cannot drift. Use `grantable_groups` for the admin-facing list.
    """
    skill_groups, findings = grantable_groups(card)
    if not skill_groups:
        return [], findings
    try:
        return [a2a_groups.agent_group_name(card.get("name") or "")], findings
    except a2a_groups.GroupNameError as exc:
        findings.append(_finding(
            "card-unencodable", CLOSED,
            f"the card name cannot be encoded as a group name ({exc}), so no "
            "authorizer can be configured for it"))
        return [], findings


def check(card: dict, authorizer: dict | None, discovery_url: str,
          app_client_id: str) -> list[dict]:
    """Findings for one sub-agent. Empty list means conformant.

    `authorizer` is the Runtime's `authorizerConfiguration` as GetAgentRuntime
    returns it, or None when the runtime could not be read.
    """
    findings: list[dict] = []
    wanted, card_findings = expected_groups(card)
    findings.extend(card_findings)

    if authorizer is None:
        findings.append(_finding(
            "runtime-unreadable", INFO,
            "the runtime behind this card could not be read, so its authorizer "
            "could not be checked"))
        return findings

    jwt = (authorizer or {}).get("customJWTAuthorizer")
    if not jwt:
        # No JWT authorizer at all. Either the runtime is IAM-authorized — in which
        # case the orchestrator's Bearer token cannot reach it — or it is open.
        findings.append(_finding(
            "no-jwt-authorizer", OPEN,
            "the runtime has no customJWTAuthorizer, so it does not validate this "
            "deployment's user tokens and cannot check grants"))
        return findings

    if (jwt.get("discoveryUrl") or "") != discovery_url:
        findings.append(_finding(
            "wrong-pool", CLOSED,
            f"discoveryUrl is {jwt.get('discoveryUrl')!r}, not this deployment's "
            f"{discovery_url!r} — every call is refused"))

    audience = list(jwt.get("allowedAudience") or [])
    if app_client_id and app_client_id not in audience:
        # `allowedClients` validates `client_id`, which only an ACCESS token carries;
        # a Cognito idToken carries the app client in `aud`. Getting this wrong
        # refuses a fully granted user with a message about client_id.
        findings.append(_finding(
            "wrong-audience", CLOSED,
            f"allowedAudience {audience} does not include the app client "
            f"{app_client_id!r} — an idToken from this pool is refused"))

    claims = list(jwt.get("customClaims") or [])
    if not claims:
        findings.append(_finding(
            "no-claim-check", OPEN,
            "customClaims is empty, so ANY authenticated user of this pool can "
            "reach every skill on this agent — grants are not enforced"))
        return findings

    claim = claims[0]
    if (claim.get("inboundTokenClaimName") or "") != CLAIM_NAME:
        findings.append(_finding(
            "claim-wrong-name", OPEN,
            f"the claim checked is {claim.get('inboundTokenClaimName')!r}, not "
            f"{CLAIM_NAME!r}, so grant groups are not what gates this agent"))
        return findings
    if (claim.get("inboundTokenClaimValueType") or "") != CLAIM_VALUE_TYPE:
        findings.append(_finding(
            "claim-wrong-value-type", INFO,
            f"inboundTokenClaimValueType is "
            f"{claim.get('inboundTokenClaimValueType')!r}, expected "
            f"{CLAIM_VALUE_TYPE!r}"))

    match = (claim.get("authorizingClaimMatchValue") or {})
    operator = match.get("claimMatchOperator") or ""
    if operator != CLAIM_OPERATOR:
        findings.append(_finding(
            "claim-wrong-operator", INFO,
            f"claimMatchOperator is {operator!r}, expected {CLAIM_OPERATOR!r}"))

    configured = sorted(
        (match.get("claimMatchValue") or {}).get("matchValueStringList") or [])

    # Three buckets rather than a set difference, because "names a group this card
    # does not declare" splits into a harmless case and a dangerous one, and a plain
    # diff reports them identically.
    #
    #   wanted            the stable agent group — what this should name
    #   legacy            this card's own per-skill groups — works, but couples a
    #                     card edit to an UpdateAgentRuntime
    #   foreign           a group belonging to some OTHER agent — a real door for
    #                     someone this card never declared
    card_name = card.get("name") or ""
    legacy, foreign = [], []
    for g in configured:
        if g in wanted:
            continue
        owner = a2a_groups.agent_of_group(g)
        (legacy if owner and owner == card_name else foreign).append(g)

    missing = [g for g in wanted if g not in configured]
    if missing and not legacy:
        findings.append(_finding(
            "claim-missing-groups", CLOSED,
            f"the authorizer does not name {missing}, and names no group of this "
            "card's either — every caller is refused at the door"))
    elif missing and legacy:
        # NOT closed: this runtime works today. Every granted user holds these
        # per-skill groups, CONTAINS_ANY passes on any one of them, and the container
        # applies the same check afterwards. What it is, is still coupled — adding a
        # skill to the card will refuse the users granted it until someone redeploys.
        # Reported so the fix is discoverable, at the severity the facts support.
        findings.append(_finding(
            "claim-skill-groups-only", INFO,
            f"the authorizer enumerates this card's per-skill groups ({len(legacy)}) "
            f"instead of the stable {missing[0]!r}. Callers are authorized correctly, "
            "but ADDING A SKILL will refuse its grantees until this runtime is "
            "redeployed. Re-run scripts/a2a-authorizer-contract.py to drop the "
            "coupling; no grant changes are needed"))
    if foreign:
        findings.append(_finding(
            "claim-extra-groups", OPEN,
            f"the authorizer names {foreign}, which belong to a different agent than "
            f"{card_name!r} — holders of those groups reach this agent without ever "
            "being granted it"))
    return findings


def worst_severity(findings: list[dict]) -> str:
    """`open`, `closed`, `info` or `""`. OPEN outranks CLOSED: too much access is
    worse than too little, and a list sorted the other way buries it."""
    for level in (OPEN, CLOSED, INFO):
        if any(f["severity"] == level for f in findings):
            return level
    return ""


def authorizer_for(card: dict, discovery_url: str, app_client_id: str) -> dict:
    """The `customJWTAuthorizer` a card SHOULD be deployed with.

    The other half of this module: `check` says what is wrong, this says what right
    looks like, and both derive the group list from the same place. Used by
    scripts/a2a-authorizer-contract.py so an agent's author can copy it rather than
    infer it from a failure.
    """
    groups, _ = expected_groups(card)
    return {
        "discoveryUrl": discovery_url,
        "allowedAudience": [app_client_id],
        "customClaims": [{
            "inboundTokenClaimName": CLAIM_NAME,
            "inboundTokenClaimValueType": CLAIM_VALUE_TYPE,
            "authorizingClaimMatchValue": {
                "claimMatchValue": {"matchValueStringList": groups},
                "claimMatchOperator": CLAIM_OPERATOR,
            },
        }],
    }
