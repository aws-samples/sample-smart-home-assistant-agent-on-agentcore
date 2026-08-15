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
GROUPS = ["a2a-energy-optimization-agent.estimate_savings",
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
                    "matchValueStringList": GROUPS if groups is None else groups},
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


def test_the_expected_groups_come_from_the_card():
    groups, findings = conf.expected_groups(CARD)
    assert groups == GROUPS and findings == []


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


def test_a_group_the_card_no_longer_declares_is_open():
    """A skill removed from the card leaves its holders still getting in."""
    findings = conf.check(CARD, _authorizer(groups=GROUPS + [
        "a2a-energy-optimization-agent.retired_skill"]), DISCOVERY, CLIENT)
    assert _codes(findings) == {"claim-extra-groups"}
    assert conf.worst_severity(findings) == conf.OPEN


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


def test_a_skill_added_without_a_redeploy_is_closed():
    """CONTAINS_ANY has no wildcard, so a new skill has to be enumerated."""
    findings = conf.check(CARD, _authorizer(groups=[GROUPS[0]]), DISCOVERY, CLIENT)
    assert _codes(findings) == {"claim-missing-groups"}
    assert conf.worst_severity(findings) == conf.CLOSED
    assert "tariff_analysis" in findings[0]["detail"]


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
        CARD, _authorizer(groups=["a2a-energy-optimization-agent.retired"],
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
