"""Validate an AgentCard (and optionally an authorizer) against a published manifest.

Offline, stdlib only, and — the point — **it reads its rules out of the manifest rather
than out of this repo.** No import of `a2a_groups` or `a2a_conformance`, no AWS call, no
`cdk-outputs.json`. Give it the manifest JSON an agent team copied from
`?action=a2a-manifest` and their own `card.json`, and it answers before anything is
deployed or registered.

Why rules-from-the-manifest and not rules-from-here
---------------------------------------------------
Two things fall out of it, and both are the reason this file exists at all:

1. **A third party can run it.** They do not have this repo, our credentials, or our
   account. A checker that imported our enforcement modules would be a checker only we
   could run, which puts us back in their debugging loop — exactly the coupling this
   whole line of work removes.
2. **It proves the manifest is SUFFICIENT.** If every check here can be expressed in
   terms of manifest fields, then the manifest really does carry the whole contract. A
   rule this file needs and cannot find is a manifest bug, and it says so
   (`manifest-incomplete`) instead of silently falling back to a hardcoded default —
   which is how a published contract quietly stops being the contract.

The authorizer half deliberately mirrors `a2a_conformance.check`. Those two must agree,
or we would bless a configuration at pre-flight and refuse it at approval;
`shared/tests/test_a2a_preflight.py` pins that both ways.

Severities are tuned to "before you deploy", not to an access direction
----------------------------------------------------------------------
    BLOCK  this will be rejected, or will not work. Fix it first.
    RISK   it will work, and it is dangerous. The too-permissive authorizer.
    NOTE   worth knowing; not a defect.

`a2a_conformance`'s OPEN/CLOSED split answers "which way is the door wrong", which is the
right question for a live agent and the wrong one for a card that has never been
registered — "capabilities is missing" is neither too open nor too closed.
"""

from __future__ import annotations

import json
import re

BLOCK = "block"
RISK = "risk"
NOTE = "note"

_SEVERITY_ORDER = (BLOCK, RISK, NOTE)

# The manifest major version this checker understands. A minor bump is additive by the
# manifest's own contract, so only the major is compared.
UNDERSTOOD_MAJOR = 1


def _finding(code: str, severity: str, detail: str) -> dict:
    return {"code": code, "severity": severity, "detail": detail}


def worst_severity(findings: list[dict]) -> str:
    """`block`, `risk`, `note` or `""`. BLOCK first: it is what stops a deploy."""
    for level in _SEVERITY_ORDER:
        if any(f["severity"] == level for f in findings):
            return level
    return ""


def is_blocking(findings: list[dict]) -> bool:
    """Anything that should stop a deploy or a registration.

    RISK counts. A too-permissive authorizer means authorization is not happening at
    all, and shipping it is worse than shipping nothing — it is the one finding whose
    production symptom is "everything works".
    """
    return any(f["severity"] in (BLOCK, RISK) for f in findings)


# ---------------------------------------------------------------------------
# Reading the manifest, and complaining when it cannot be read
# ---------------------------------------------------------------------------

def _dig(manifest: dict, path: str):
    """`manifest["a"]["b"]` for "a.b", or None. Never raises on a missing branch."""
    node = manifest
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _require(manifest: dict, path: str, findings: list[dict]):
    """A manifest field this checker needs. Records `manifest-incomplete` if absent.

    Deliberately NOT defaulted. A silent fallback to a hardcoded pattern or group
    template is how a published contract stops being the contract: the check would keep
    passing against a rule the platform no longer applies.
    """
    value = _dig(manifest, path)
    if value in (None, "", [], {}):
        findings.append(_finding(
            "manifest-incomplete", BLOCK,
            f"the manifest has no `{path}`, so this rule cannot be checked. Re-copy it "
            "from the platform (Admin Console -> Integration Registry -> A2A Agents -> "
            "Platform manifest); if it is genuinely absent, that is a platform bug"))
        return None
    return value


def check_manifest(manifest: dict) -> list[dict]:
    """Is this manifest one we can check against at all?"""
    findings: list[dict] = []
    version = str(_dig(manifest, "manifestVersion") or "")
    if not version:
        findings.append(_finding(
            "manifest-no-version", BLOCK,
            "the manifest carries no manifestVersion, so it cannot be trusted to mean "
            "what this checker assumes"))
        return findings
    major = version.split(".")[0]
    if not major.isdigit():
        findings.append(_finding("manifest-no-version", BLOCK,
                                 f"manifestVersion {version!r} is not a version"))
    elif int(major) != UNDERSTOOD_MAJOR:
        findings.append(_finding(
            "manifest-version-unknown", BLOCK,
            f"this checker understands manifest major {UNDERSTOOD_MAJOR}, the manifest "
            f"is {version}. A major bump means a rule changed — update the checker "
            "rather than trusting this result"))
    return findings


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

def _substitute(template: str, card_name: str = "", skill_id: str = "") -> str:
    return (template.replace("{cardName}", card_name)
                    .replace("{skillId}", skill_id))


def check_card(card: dict, manifest: dict) -> list[dict]:
    """Everything decidable about a card from the manifest alone."""
    findings: list[dict] = []

    required = _require(manifest, "card.requiredFields", findings)
    name_pattern = _require(manifest, "card.namePattern", findings)
    skill_pattern = _require(manifest, "card.skillIdPattern", findings)
    door_tmpl = _require(manifest, "groups.doorGroup", findings)
    skill_tmpl = _require(manifest, "groups.skillGroup", findings)
    max_len = _dig(manifest, "groups.maxLength")

    # --- completeness -----------------------------------------------------
    if required:
        missing = [f for f in required if not card.get(f)]
        if missing:
            # Worth spelling out: the service's own message for this is
            # "does not match any supported version", which sends people looking for a
            # version problem.
            findings.append(_finding(
                "card-incomplete", BLOCK,
                f"the card is missing {missing}. The Registry rejects this as \"does "
                "not match any supported version\", which sounds like a version "
                "problem and is a completeness problem"))

    name = card.get("name") or ""
    skills = card.get("skills") or []

    # --- name and skill ids ------------------------------------------------
    if name and name_pattern:
        if not re.match(name_pattern, name):
            findings.append(_finding(
                "card-name-unencodable", BLOCK,
                f"the card name {name!r} does not match {name_pattern} — no group name "
                "can be derived from it, so every grant on this agent is SILENTLY "
                "skipped: an admin sees the user as granted and the user is refused at "
                "the door"))

    if not skills:
        findings.append(_finding(
            "card-no-skills", BLOCK,
            "the card declares no skills, so there is nothing an admin can grant"))
    else:
        seen: set[str] = set()
        for index, skill in enumerate(skills):
            skill_id = (skill or {}).get("id") or ""
            if not skill_id:
                findings.append(_finding(
                    "skill-no-id", BLOCK,
                    f"skills[{index}] has no `id`; the id is what a grant names"))
                continue
            if skill_id in seen:
                findings.append(_finding(
                    "skill-duplicate-id", BLOCK,
                    f"skill id {skill_id!r} appears more than once; grants collapse to "
                    "one entry and the console shows a duplicate row"))
            seen.add(skill_id)
            if skill_pattern and not re.match(skill_pattern, skill_id):
                findings.append(_finding(
                    "skill-id-unencodable", BLOCK,
                    f"skill id {skill_id!r} does not match {skill_pattern} — a grant on "
                    "it is silently skipped"))

    # --- group-name length -------------------------------------------------
    # A name that is fine on its own can still produce an over-long group once the skill
    # is appended, and that failure surfaces only when someone tries to GRANT it.
    if name and max_len and door_tmpl and skill_tmpl:
        door = _substitute(door_tmpl, name)
        if len(door) > max_len:
            findings.append(_finding(
                "group-name-too-long", BLOCK,
                f"the door group {door!r} is {len(door)} characters, over the "
                f"{max_len} limit"))
        for skill in skills:
            skill_id = (skill or {}).get("id") or ""
            if not skill_id:
                continue
            group = _substitute(skill_tmpl, name, skill_id)
            if len(group) > max_len:
                findings.append(_finding(
                    "group-name-too-long", BLOCK,
                    f"the grant group {group!r} is {len(group)} characters, over the "
                    f"{max_len} limit — this skill cannot be granted even though the "
                    "card is otherwise valid"))

    # --- the url -----------------------------------------------------------
    url = card.get("url") or ""
    if url:
        findings.extend(_check_url(url, manifest))

    return findings


def _check_url(url: str, manifest: dict) -> list[dict]:
    """Is the card's `url` a shape the platform can resolve back to a runtime?"""
    findings: list[dict] = []
    gateway_tmpl = _dig(manifest, "gateway.targetUrlTemplate") or ""
    gateway_url = _dig(manifest, "gateway.url") or ""

    if gateway_url and url.startswith(gateway_url.rstrip("/") + "/"):
        target = url[len(gateway_url.rstrip("/")) + 1:].strip("/")
        if not target:
            findings.append(_finding(
                "url-gateway-no-target", BLOCK,
                f"the url points at the gateway host with no target path. Expected "
                f"{gateway_tmpl or gateway_url + '/{targetName}'}"))
        elif "/" in target:
            findings.append(_finding(
                "url-gateway-deep-path", BLOCK,
                f"a gateway target path is one segment; got {target!r}"))
        else:
            findings.append(_finding(
                "url-uses-gateway", NOTE,
                f"routing through the platform gateway as target {target!r}. The target "
                "must exist before this card resolves — ask the platform to run its "
                "gateway reconcile, or register your runtime URL directly first"))
        return findings

    if "/runtimes/" in url and url.rstrip("/").endswith("/invocations"):
        findings.append(_finding("url-direct-runtime", NOTE,
                                 "registering your own runtime URL directly"))
        return findings

    findings.append(_finding(
        "url-unresolvable", BLOCK,
        f"the url {url!r} is neither a Runtime invocations URL "
        "(.../runtimes/<arn>/invocations) nor a target under this deployment's gateway. "
        "The platform resolves this url back to a runtime to read its authorizer, and "
        "cannot check — or route to — anything else"))
    return findings


# ---------------------------------------------------------------------------
# The authorizer
# ---------------------------------------------------------------------------

def check_authorizer(card: dict, authorizer: dict, manifest: dict) -> list[dict]:
    """Compare the authorizer you are ABOUT to deploy against the manifest.

    `authorizer` is what goes in `UpdateAgentRuntime`'s `authorizerConfiguration` — so
    either `{"customJWTAuthorizer": {...}}` or the inner object; both are accepted,
    because getting that nesting wrong is a copy-paste slip and not worth a finding.

    Mirrors `a2a_conformance.check`, and is pinned against it by test, so a pass here is
    a pass at the approval gate.
    """
    findings: list[dict] = []
    name = card.get("name") or ""

    jwt = authorizer.get("customJWTAuthorizer") if isinstance(authorizer, dict) else None
    if jwt is None:
        jwt = authorizer if isinstance(authorizer, dict) else {}
    if not jwt:
        findings.append(_finding(
            "no-jwt-authorizer", RISK,
            "there is no customJWTAuthorizer here. Without one the runtime does not "
            "validate this deployment's user tokens and cannot check grants at all"))
        return findings

    want_discovery = _require(manifest, "authorizer.discoveryUrl", findings)
    want_audience = _require(manifest, "authorizer.allowedAudience", findings)
    want_claim = _require(manifest, "authorizer.claimName", findings)
    want_type = _dig(manifest, "authorizer.claimValueType")
    want_op = _require(manifest, "authorizer.claimMatchOperator", findings)
    door_tmpl = _require(manifest, "groups.doorGroup", findings)

    if want_discovery and (jwt.get("discoveryUrl") or "") != want_discovery:
        findings.append(_finding(
            "wrong-pool", BLOCK,
            f"discoveryUrl is {jwt.get('discoveryUrl')!r}, not the manifest's "
            f"{want_discovery!r} — every call is refused"))

    # The single most common silent failure, so it gets its own finding rather than
    # being folded into a missing-audience complaint.
    if "allowedClients" in jwt and not jwt.get("allowedAudience"):
        findings.append(_finding(
            "uses-allowedClients", BLOCK,
            "this sets `allowedClients`, which validates `client_id` — a claim only an "
            "ACCESS token carries. A Cognito idToken puts the app client in `aud`, so "
            "every fully granted user is refused with \"Claim 'client_id' value "
            "mismatch with configuration\". Use `allowedAudience`"))
    elif want_audience:
        have = list(jwt.get("allowedAudience") or [])
        missing = [a for a in want_audience if a not in have]
        if missing:
            findings.append(_finding(
                "wrong-audience", BLOCK,
                f"allowedAudience {have} does not include {missing} — an idToken from "
                "this deployment's pool is refused"))

    claims = list(jwt.get("customClaims") or [])
    if not claims:
        findings.append(_finding(
            "no-claim-check", RISK,
            "customClaims is empty, so ANY authenticated user of this deployment's pool "
            "reaches every skill on this agent. Grants are not enforced, and there is "
            "no error anywhere — everything appears to work"))
        return findings

    claim = claims[0]
    if want_claim and (claim.get("inboundTokenClaimName") or "") != want_claim:
        findings.append(_finding(
            "claim-wrong-name", RISK,
            f"the claim checked is {claim.get('inboundTokenClaimName')!r}, not "
            f"{want_claim!r}, so grant groups are not what gates this agent"))
        return findings
    if want_type and (claim.get("inboundTokenClaimValueType") or "") != want_type:
        findings.append(_finding(
            "claim-wrong-value-type", NOTE,
            f"inboundTokenClaimValueType is "
            f"{claim.get('inboundTokenClaimValueType')!r}, expected {want_type!r}"))

    match = claim.get("authorizingClaimMatchValue") or {}
    if want_op and (match.get("claimMatchOperator") or "") != want_op:
        findings.append(_finding(
            "claim-wrong-operator", NOTE,
            f"claimMatchOperator is {match.get('claimMatchOperator')!r}, expected "
            f"{want_op!r}"))

    configured = sorted((match.get("claimMatchValue") or {})
                        .get("matchValueStringList") or [])
    if not door_tmpl or not name:
        return findings
    door = _substitute(door_tmpl, name)

    if door in configured:
        # Extra entries are only dangerous when they belong to ANOTHER agent: a holder
        # of this card's own per-skill group was granted this agent.
        skill_tmpl = _dig(manifest, "groups.skillGroup") or ""
        prefix = _substitute(skill_tmpl, name, "") or (door + ".")
        foreign = [g for g in configured
                   if g != door and not g.startswith(prefix)]
        if foreign:
            findings.append(_finding(
                "claim-extra-groups", RISK,
                f"the authorizer also names {foreign}, which do not belong to "
                f"{name!r} — holders of those groups reach this agent without ever "
                "being granted it"))
        return findings

    # No door group. Is it the still-coupled-but-working pre-migration shape?
    skill_tmpl = _dig(manifest, "groups.skillGroup") or ""
    own_skill_groups = [g for g in configured
                       if skill_tmpl and g.startswith(_substitute(skill_tmpl, name, ""))]
    if own_skill_groups:
        findings.append(_finding(
            "claim-skill-groups-only", NOTE,
            f"this enumerates {len(own_skill_groups)} per-skill group(s) instead of the "
            f"stable {door!r}. Callers are authorized correctly, but ADDING A SKILL will "
            "refuse its grantees until this runtime is redeployed. The manifest's "
            "authorizer.template drops that coupling; no grant changes are needed"))
        return findings

    findings.append(_finding(
        "claim-missing-groups", BLOCK,
        f"the authorizer does not name {door!r}, and names no group of this card's "
        "either — every caller is refused at the door"))
    return findings


# ---------------------------------------------------------------------------
# The whole thing
# ---------------------------------------------------------------------------

def check(card: dict, manifest: dict, authorizer: dict | None = None) -> list[dict]:
    """All pre-flight findings. Empty means ready to deploy and register.

    Manifest problems short-circuit: a rule that cannot be read cannot be checked, and
    reporting card findings derived from half a contract would be worse than reporting
    none.
    """
    manifest_findings = check_manifest(manifest)
    if any(f["severity"] == BLOCK for f in manifest_findings):
        return manifest_findings

    findings = list(manifest_findings)
    findings.extend(check_card(card, manifest))
    if authorizer is not None:
        findings.extend(check_authorizer(card, authorizer, manifest))
    return findings


def expected_authorizer(card: dict, manifest: dict) -> dict:
    """The authorizer this card SHOULD be deployed with, per the manifest.

    Built by substituting into `manifest.authorizer.template`, so it is the platform's
    own answer rather than this file's reconstruction of it. Raises `KeyError` when the
    manifest has no template — a caller asking for the right answer needs to know it is
    unavailable, not receive a guess.
    """
    template = _dig(manifest, "authorizer.template")
    if not template:
        raise KeyError("the manifest carries no authorizer.template")
    name = card.get("name") or ""
    return json.loads(_substitute(json.dumps(template), name))
