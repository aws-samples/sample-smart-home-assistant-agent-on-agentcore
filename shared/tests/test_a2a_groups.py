"""Tests for the A2A grant group naming (shared/a2a_groups.py).

This convention is an authorization boundary, and both directions of a mistake are
silent: a group written one way and matched another either denies a granted user
with no error anywhere, or offers the model a tool the platform will refuse
mid-turn. So the round trip, the rejection of unencodable input, and the handling of
foreign groups are all pinned.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "shared"))

import a2a_groups as g


def test_group_name_shape():
    assert g.group_name("knowledge-qa-agent", "answer_from_docs") == \
        "a2a-knowledge-qa-agent.answer_from_docs"


def test_round_trip():
    name = g.group_name("light-effect-agent", "compose_effect")
    assert g.parse_group(name) == ("light-effect-agent", "compose_effect")


def test_unencodable_input_raises_rather_than_sanitising():
    """A sanitised name is one some other caller sanitises differently."""
    for agent, skill in [("", "x"), ("a", ""), ("bad name", "x"),
                         ("a.b", "x"), ("agent", "sk.ill"), ("-lead", "x")]:
        with pytest.raises(g.GroupNameError):
            g.group_name(agent, skill)


def test_overlong_name_raises():
    with pytest.raises(g.GroupNameError):
        g.group_name("a" * 100, "b" * 100)


def test_foreign_groups_are_ignored_not_rejected():
    """The same claim carries `admin`, and pools hold unrelated groups."""
    assert g.parse_group("admin") is None
    assert g.parse_group("") is None
    assert g.parse_group("some-other-group") is None


def test_malformed_own_groups_do_not_decode_to_a_grant():
    """A grant on the empty skill would match nothing but read as real in the UI."""
    assert g.parse_group("a2a-") is None
    assert g.parse_group("a2a-agent-with-no-skill") is None
    assert g.parse_group("a2a-.skill") is None
    assert g.parse_group("a2a-agent.") is None


def test_grants_from_claim_groups_by_agent():
    claim = [
        "admin",
        "a2a-knowledge-qa-agent.answer_from_docs",
        "a2a-knowledge-qa-agent.troubleshoot_from_docs",
        "a2a-light-effect-agent.compose_effect",
        "not-ours",
    ]
    assert g.grants_from_claim(claim) == {
        "knowledge-qa-agent": ["answer_from_docs", "troubleshoot_from_docs"],
        "light-effect-agent": ["compose_effect"],
    }


def test_empty_and_missing_claims_are_no_grants():
    assert g.grants_from_claim(None) == {}
    assert g.grants_from_claim([]) == {}
    assert g.grants_from_claim(["admin"]) == {}


def test_skills_for_agent_is_empty_for_an_unmentioned_agent():
    """Empty must mean no access — this is the fail-closed path."""
    claim = ["a2a-knowledge-qa-agent.answer_from_docs"]
    assert g.skills_for_agent(claim, "knowledge-qa-agent") == {"answer_from_docs"}
    assert g.skills_for_agent(claim, "home-security-agent") == frozenset()
    assert g.skills_for_agent([], "knowledge-qa-agent") == frozenset()


def test_skill_groups_for_agent_is_sorted_and_deduped():
    """These reach the container, so a reordering would be a prompt-cache miss."""
    assert g.skill_groups_for_agent("home-security-agent",
                                    ["risk_assessment", "incident_response",
                                     "risk_assessment"]) == [
        "a2a-home-security-agent.incident_response",
        "a2a-home-security-agent.risk_assessment",
    ]


def test_one_agents_group_never_grants_another():
    """The cross-agent case the smoke test also covers, pinned cheaply here."""
    claim = g.skill_groups_for_agent("home-security-agent", ["risk_assessment"])
    assert g.skills_for_agent(claim, "device-control-agent") == frozenset()


# ---------------------------------------------------------------------------
# The agent-level group: the door key
# ---------------------------------------------------------------------------

def test_the_door_list_does_not_move_when_skills_change():
    """The whole reason `authorizer_groups` exists.

    `CONTAINS_ANY` has no wildcard, so a list derived from the skills made "add a
    skill to the card" into "redeploy the runtime, or its grantees are refused at the
    door with nothing explaining why". One stable entry removes that with no loss of
    door strength — the door only ever asked "any grant on this agent".
    """
    assert g.authorizer_groups("home-security-agent", ["risk_assessment"]) == \
        ["a2a-home-security-agent"]
    assert g.authorizer_groups("home-security-agent",
                               ["risk_assessment", "incident_response", "new"]) == \
        ["a2a-home-security-agent"]
    assert g.authorizer_groups("home-security-agent") == ["a2a-home-security-agent"]


def test_the_old_name_is_gone_rather_than_aliased():
    """Five deployment units carry a COPY of this module.

    An alias would let a stale copy keep emitting the per-skill list while every
    reader assumed the stable one — silent, and in the direction that locks users out.
    An AttributeError on the first call is the failure we want.
    """
    assert not hasattr(g, "all_groups_for_agent")


def test_the_agent_group_contributes_no_skills():
    """It must not decode as a grant, or the orchestrator would offer a tool for `""`."""
    door = g.agent_group_name("home-security-agent")
    assert g.parse_group(door) is None
    assert g.grants_from_claim([door]) == {}
    assert g.skills_for_agent([door], "home-security-agent") == frozenset()


def test_agent_of_group_decodes_both_shapes():
    """What the revocation sweep runs on. `parse_group` cannot answer for the door
    key, and a sweep blind to it would strip the skill groups that gate nothing and
    leave the one that opens the door."""
    assert g.agent_of_group("a2a-home-security-agent") == "home-security-agent"
    assert g.agent_of_group("a2a-home-security-agent.risk_assessment") == \
        "home-security-agent"
    assert g.agent_of_group("admin") is None
    assert g.agent_of_group("") is None
    assert g.agent_of_group("a2a-") is None


def test_an_unsafe_agent_name_raises_rather_than_being_sanitised():
    with pytest.raises(g.GroupNameError):
        g.agent_group_name("home security agent")
    with pytest.raises(g.GroupNameError):
        g.agent_group_name("")
