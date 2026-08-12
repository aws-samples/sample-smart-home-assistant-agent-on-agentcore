"""Tests for deriving a sub-agent's skill grant from a signed Cognito group claim.

This replaced a client-asserted header, so the tests that matter are the ones where
a mistake would *widen* access rather than break something. Every case below is one
where the wrong behaviour still returns a normal-looking answer:

  - empty claim read as "unrestricted" instead of "no grant"
  - one agent's group granting access to another agent
  - a skill group granting a skill it does not name
  - a missing agent name defaulting to "match everything"
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(os.path.dirname(HERE))
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from common import server as srv
from common import a2a_groups as ag


@pytest.fixture(autouse=True)
def _restore_module_state():
    skills, name = srv._SKILL_IDS, srv._AGENT_NAME
    yield
    srv._SKILL_IDS, srv._AGENT_NAME = skills, name


def _claims(groups, sub="user-1", email="u@example.com"):
    return {"sub": sub, "email": email, "cognito:groups": groups}


def test_claim_grants_only_the_named_skills():
    claims = _claims(["a2a-home-security-agent.risk_assessment"])
    assert srv.skills_from_claims(claims, "home-security-agent") == {"risk_assessment"}


def test_no_groups_is_no_grant_not_unrestricted():
    """The inversion nobody notices, because everything keeps working."""
    assert srv.skills_from_claims(_claims([]), "home-security-agent") == frozenset()
    assert srv.skills_from_claims({"sub": "u"}, "home-security-agent") == frozenset()
    assert srv.skills_from_claims(_claims(["admin"]), "home-security-agent") == frozenset()


def test_another_agents_group_grants_nothing_here():
    claims = _claims(["a2a-device-control-agent.orchestrate_devices"])
    assert srv.skills_from_claims(claims, "home-security-agent") == frozenset()


def test_a_skill_group_does_not_grant_a_sibling_skill():
    claims = _claims(["a2a-home-security-agent.risk_assessment"])
    granted = srv.skills_from_claims(claims, "home-security-agent")
    assert "incident_response" not in granted


def test_missing_agent_name_refuses_rather_than_matching_everything():
    with pytest.raises(PermissionError):
        srv.skills_from_claims(_claims(["a2a-x.y"]), "")


def test_enforce_refuses_a_caller_with_no_overlap():
    srv._SKILL_IDS = frozenset({"risk_assessment", "incident_response"})
    with pytest.raises(PermissionError):
        srv.enforce_allowed_skills(frozenset(), srv._SKILL_IDS)
    with pytest.raises(PermissionError):
        srv.enforce_allowed_skills(frozenset({"compose_effect"}), srv._SKILL_IDS)
    # One overlapping skill is enough to be admitted to the agent.
    srv.enforce_allowed_skills(frozenset({"risk_assessment"}), srv._SKILL_IDS)


def test_the_authorizer_match_list_covers_every_published_skill():
    """The runtime authorizer enumerates skills, so a new skill needs a redeploy.

    If this list were built from anything narrower than the card's skills, a user
    granted only the missing skill would be refused at the door with no clue why.
    """
    skills = ["risk_assessment", "incident_response"]
    groups = ag.all_groups_for_agent("home-security-agent", skills)
    for skill in skills:
        claim_with_only_this = [ag.group_name("home-security-agent", skill)]
        assert claim_with_only_this[0] in groups
        assert srv.skills_from_claims(
            _claims(claim_with_only_this), "home-security-agent") == {skill}


def test_strip_bearer_handles_both_shapes():
    assert srv._strip_bearer("Bearer abc.def.ghi") == "abc.def.ghi"
    assert srv._strip_bearer("bearer abc") == "abc"
    assert srv._strip_bearer("abc") == "abc"
    assert srv._strip_bearer("") == ""
    assert srv._strip_bearer(None) == ""
