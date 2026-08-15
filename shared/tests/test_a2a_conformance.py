"""Tests for the sub-agent authorizer conformance rule.

The point of this rule is that registering a card makes an agent DISCOVERABLE while
its authorizer decides whether it is CALLABLE, and nothing connected the two. Every
case below is a real way for those to disagree, and none of them raises or logs on
its own — the Integration Registry page shows `approved` throughout.

The severity split is the part worth protecting: OPEN means someone can reach the
agent who should not, CLOSED means granted users are refused. Collapsing them into
"non-conformant" would put "authorization is not happening" next to "a skill was
added without a redeploy" and let an operator triage them the same way.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import a2a_conformance as conf  # noqa: E402

DISCOVERY = ("https://cognito-idp.us-west-2.amazonaws.com/"
             "us-west-2_HwYYt6qLz/.well-known/openid-configuration")
CLIENT = "7031k7ctqes8mfe5jrf7kljttf"

CARD = {
    "name": "energy-optimization-agent",
    "skills": [{"id": "estimate_savings"}, {"id": "tariff_analysis"}],
}
# The DOOR list: one stable agent-level group, unchanged as the card gains skills.
DOOR = ["a2a-energy-optimization-agent"]
# The per-skill groups an admin hands out and the container reads. These are what the
# authorizer used to enumerate, which is what coupled a card edit to a redeploy.
SKILL_GROUPS = ["a2a-energy-optimization-agent.estimate_savings",
                "a2a-energy-optimization-agent.tariff_analysis"]


def _authorizer(groups=None, discovery=DISCOVERY, audience=None, claims=True,
                claim_name=conf.CLAIM_NAME, operator=conf.CLAIM_OPERATOR):
    jwt = {"discoveryUrl": discovery,
           "allowedAudience": [CLIENT] if audience is None else audience}
    if claims:
        jwt["customClaims"] = [{
            "inboundTokenClaimName": claim_name,
            "inboundTokenClaimValueType": conf.CLAIM_VALUE_TYPE,
            "authorizingClaimMatchValue": {
                "claimMatchValue": {
                    "matchValueStringList": DOOR if groups is None else groups},
                "claimMatchOperator": operator,
            },
        }]
    return {"customJWTAuthorizer": jwt}


def _codes(findings):
    return {f["code"] for f in findings}


# ---------------------------------------------------------------------------
# The happy case
# ---------------------------------------------------------------------------

def test_a_correctly_deployed_agent_has_no_findings():
    assert conf.check(CARD, _authorizer(), DISCOVERY, CLIENT) == []


def test_the_door_list_is_one_stable_group_not_the_skill_list():
    """The change that removes the "add a skill -> redeploy" coupling.

    Whether the card has two skills or twenty, the authorizer's CONTAINS_ANY list is
    the same one entry — so a card edit is no longer also a runtime change.
    """
    groups, findings = conf.expected_groups(CARD)
    assert groups == DOOR and findings == []

    wider = {"name": CARD["name"],
             "skills": CARD["skills"] + [{"id": "peak_shift"}, {"id": "forecast"}]}
    assert conf.expected_groups(wider)[0] == DOOR


def test_the_grantable_groups_are_still_per_skill():
    """The container still needs them: they are how it knows WHICH skills were granted."""
    groups, findings = conf.grantable_groups(CARD)
    assert groups == SKILL_GROUPS and findings == []


def test_the_generator_output_passes_its_own_check():
    """`authorizer_for` and `check` must agree, or the contract we hand out is wrong.

    They derive the group list from the same function, and this is what keeps that
    true if either side is edited.
    """
    generated = {"customJWTAuthorizer":
                 conf.authorizer_for(CARD, DISCOVERY, CLIENT)}
    assert conf.check(CARD, generated, DISCOVERY, CLIENT) == []


# ---------------------------------------------------------------------------
# OPEN: more access than intended
# ---------------------------------------------------------------------------

def test_no_claim_check_is_reported_as_open():
    """The dangerous one: every user of the pool reaches every skill, silently."""
    findings = conf.check(CARD, _authorizer(claims=False), DISCOVERY, CLIENT)
    assert _codes(findings) == {"no-claim-check"}
    assert conf.worst_severity(findings) == conf.OPEN


def test_no_jwt_authorizer_at_all_is_open():
    findings = conf.check(CARD, {}, DISCOVERY, CLIENT)
    assert _codes(findings) == {"no-jwt-authorizer"}
    assert conf.worst_severity(findings) == conf.OPEN


def test_checking_the_wrong_claim_is_open():
    """A grant group cannot gate anything if the authorizer reads another claim."""
    findings = conf.check(CARD, _authorizer(claim_name="custom:tier"),
                          DISCOVERY, CLIENT)
    assert "claim-wrong-name" in _codes(findings)
    assert conf.worst_severity(findings) == conf.OPEN


def test_a_group_belonging_to_ANOTHER_agent_is_open():
    """The real door someone was never granted: a foreign agent's group listed here.

    Note the narrowing against the old rule. A leftover group of this card's OWN
    agent is no longer OPEN — its holders were granted this agent, so they are not
    reaching anything they were not given. A group naming a DIFFERENT agent is, and
    that is the case that was previously buried in the same finding.
    """
    findings = conf.check(CARD, _authorizer(groups=DOOR + [
        "a2a-home-security-agent.arm_system"]), DISCOVERY, CLIENT)
    assert _codes(findings) == {"claim-extra-groups"}
    assert conf.worst_severity(findings) == conf.OPEN
    assert "a2a-home-security-agent.arm_system" in findings[0]["detail"]


# ---------------------------------------------------------------------------
# CLOSED: granted users are refused
# ---------------------------------------------------------------------------

def test_a_foreign_cognito_pool_is_closed():
    findings = conf.check(CARD, _authorizer(discovery="https://example.invalid/oidc"),
                          DISCOVERY, CLIENT)
    assert "wrong-pool" in _codes(findings)
    assert conf.worst_severity(findings) == conf.CLOSED


def test_a_missing_audience_is_closed():
    """`allowedClients` validates client_id, which only an access token carries; an
    idToken carries the app client in `aud`. This is the shape that refuses a fully
    granted user with a message about client_id."""
    findings = conf.check(CARD, _authorizer(audience=["some-other-client"]),
                          DISCOVERY, CLIENT)
    assert "wrong-audience" in _codes(findings)


def test_an_authorizer_naming_nothing_of_this_card_is_closed():
    findings = conf.check(CARD, _authorizer(groups=["a2a-home-security-agent"]),
                          DISCOVERY, CLIENT)
    assert "claim-missing-groups" in _codes(findings)
    assert conf.worst_severity(findings) == conf.OPEN  # the foreign group outranks it


def test_an_empty_match_list_is_closed():
    findings = conf.check(CARD, _authorizer(groups=[]), DISCOVERY, CLIENT)
    assert _codes(findings) == {"claim-missing-groups"}
    assert conf.worst_severity(findings) == conf.CLOSED


# ---------------------------------------------------------------------------
# The migration window: still coupled, but NOT broken
# ---------------------------------------------------------------------------

def test_a_pre_migration_authorizer_is_info_not_closed():
    """The 8 built-ins look like this until their next deploy, and they WORK.

    Every granted user holds these per-skill groups, CONTAINS_ANY passes on any one
    of them, and the container applies the same check afterwards. Reporting CLOSED
    ("granted users are refused") would be false, and a red row an operator cannot
    reproduce is how a check earns being ignored. The finding it does raise names the
    real cost: the next skill added to this card will refuse its grantees.
    """
    findings = conf.check(CARD, _authorizer(groups=SKILL_GROUPS), DISCOVERY, CLIENT)
    assert _codes(findings) == {"claim-skill-groups-only"}
    assert conf.worst_severity(findings) == conf.INFO
    assert "ADDING A SKILL" in findings[0]["detail"]


def test_a_partial_pre_migration_list_is_still_only_info():
    """One skill group is enough for the door, which is exactly why the old
    per-skill enumeration bought no authorization."""
    findings = conf.check(CARD, _authorizer(groups=[SKILL_GROUPS[0]]),
                          DISCOVERY, CLIENT)
    assert _codes(findings) == {"claim-skill-groups-only"}
    assert conf.worst_severity(findings) == conf.INFO


def test_naming_both_shapes_is_fully_conformant():
    """The state a cautious operator lands in mid-migration. Nothing to report."""
    assert conf.check(CARD, _authorizer(groups=DOOR + SKILL_GROUPS),
                      DISCOVERY, CLIENT) == []


def test_a_card_whose_names_cannot_be_encoded_is_closed():
    card = {"name": "energy-optimization-agent",
            "skills": [{"id": "estimate savings"}]}
    _groups, findings = conf.expected_groups(card)
    assert _codes(findings) == {"card-unencodable"}


def test_a_card_with_no_name_or_no_skills_is_closed():
    assert _codes(conf.expected_groups({"skills": [{"id": "x"}]})[1]) == \
        {"card-no-name"}
    assert _codes(conf.expected_groups({"name": "a"})[1]) == {"card-no-skills"}


# ---------------------------------------------------------------------------
# Ordering and the unreadable case
# ---------------------------------------------------------------------------

def test_open_outranks_closed_when_both_are_present():
    """Too much access is worse than too little; a list sorted the other way buries it."""
    findings = conf.check(
        CARD, _authorizer(groups=["a2a-home-security-agent.arm_system"],
                          discovery="https://example.invalid/oidc"),
        DISCOVERY, CLIENT)
    assert {"wrong-pool", "claim-missing-groups", "claim-extra-groups"} <= _codes(findings)
    assert conf.worst_severity(findings) == conf.OPEN


def test_an_unreadable_runtime_is_info_not_a_verdict():
    """Not knowing is not the same as being wrong, and must not read as either."""
    findings = conf.check(CARD, None, DISCOVERY, CLIENT)
    assert _codes(findings) == {"runtime-unreadable"}
    assert conf.worst_severity(findings) == conf.INFO


def test_no_findings_has_no_severity():
    assert conf.worst_severity([]) == ""
