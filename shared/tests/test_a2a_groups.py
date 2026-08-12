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


def test_all_groups_for_agent_is_sorted_and_deduped():
    """The authorizer's CONTAINS_ANY list must be stable, or every deploy diffs."""
    assert g.all_groups_for_agent("home-security-agent",
                                  ["risk_assessment", "incident_response",
                                   "risk_assessment"]) == [
        "a2a-home-security-agent.incident_response",
        "a2a-home-security-agent.risk_assessment",
    ]


def test_one_agents_group_never_grants_another():
    """The cross-agent case the smoke test also covers, pinned cheaply here."""
    claim = g.all_groups_for_agent("home-security-agent", ["risk_assessment"])
    assert g.skills_for_agent(claim, "device-control-agent") == frozenset()
