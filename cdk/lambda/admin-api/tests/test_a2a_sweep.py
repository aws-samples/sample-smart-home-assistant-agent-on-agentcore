"""Tests for the revocation sweep: a record that may not be granted keeps no groups.

The sweep is the only thing that makes a non-APPROVED record actually uncallable.
Until it existed, status was enforced in two places that both sit ABOVE the
platform — the orchestrator's tool list and the console's catalog — while the real
door (a Cognito group matched by each sub-agent's authorizer) knew nothing about
Registry status. A caller holding the group and the URL still got in, and the URL is
published in the AgentCard.

Three things here are load-bearing and none of them fails loudly:

  - Scanning from the GROUP side. A deleted record cannot be read, so its skills
    and card name are gone and no group name can be derived from it.
  - Refusing to act on an empty or failed registry read. "Nothing is grantable" and
    "I could not find out" are the same value, and acting on the second revokes the
    entire pool.
  - Leaving groups this module does not own, and names it cannot parse, alone.
"""
from unittest.mock import MagicMock

import pytest

import a2a_groups
import subagent_policy as sp


# ---------------------------------------------------------------------------
# Which groups go (pure)
# ---------------------------------------------------------------------------

def test_a_group_whose_agent_is_not_grantable_is_revoked():
    groups = ["a2a-energy-optimization-agent.estimate_savings",
              "a2a-knowledge-qa-agent.answer_from_docs"]
    assert sp.groups_to_revoke(groups, {"knowledge-qa-agent"}) == [
        "a2a-energy-optimization-agent.estimate_savings"]


def test_every_skill_group_of_a_doomed_agent_goes():
    groups = ["a2a-energy-optimization-agent.estimate_savings",
              "a2a-energy-optimization-agent.tariff_analysis",
              "a2a-energy-optimization-agent.usage_audit"]
    assert sp.groups_to_revoke(groups, set()) == sorted(groups)


def test_nothing_is_revoked_when_every_agent_is_grantable():
    groups = ["a2a-knowledge-qa-agent.answer_from_docs"]
    assert sp.groups_to_revoke(groups, {"knowledge-qa-agent"}) == []


def test_an_unparseable_group_with_our_prefix_is_left_alone():
    """Deleting memberships on a guess is worse than an orphan group.

    The prefix is ours, but a name that does not decode means something upstream
    changed shape — and this function's output is fed straight into removals.
    """
    assert sp.groups_to_revoke(["a2a-no-separator-here"], set()) == []


def test_a_hand_made_group_for_an_unregistered_agent_is_revoked():
    """Closes the one hole nothing else covers.

    A sub-agent's authorizer is configured at deploy time from its own card, not
    from the Registry, so a group hand-made in Cognito would admit its holder even
    with no record at all.
    """
    assert sp.groups_to_revoke(["a2a-ghost-agent.some_skill"],
                               {"knowledge-qa-agent"}) == \
        ["a2a-ghost-agent.some_skill"]


# ---------------------------------------------------------------------------
# Listing groups: only ours, and paginated
# ---------------------------------------------------------------------------

def test_all_a2a_groups_ignores_groups_this_module_does_not_own():
    cognito = MagicMock()
    cognito.list_groups.return_value = {"Groups": [
        {"GroupName": "admin"},
        {"GroupName": "a2a-knowledge-qa-agent.answer_from_docs"},
        {"GroupName": "some-other-app-group"},
    ]}
    assert sp.all_a2a_groups(cognito, "pool") == [
        "a2a-knowledge-qa-agent.answer_from_docs"]


def test_all_a2a_groups_follows_pagination():
    cognito = MagicMock()
    cognito.list_groups.side_effect = [
        {"Groups": [{"GroupName": "a2a-a.one"}], "NextToken": "t"},
        {"Groups": [{"GroupName": "a2a-b.two"}]},
    ]
    assert sp.all_a2a_groups(cognito, "pool") == ["a2a-a.one", "a2a-b.two"]


def test_users_in_group_follows_pagination():
    cognito = MagicMock()
    cognito.list_users_in_group.side_effect = [
        {"Users": [{"Username": "u1"}], "NextToken": "t"},
        {"Users": [{"Username": "u2"}, {"Username": ""}]},
    ]
    assert sp.users_in_group(cognito, "pool", "a2a-a.one") == ["u1", "u2"]


# ---------------------------------------------------------------------------
# Revoking
# ---------------------------------------------------------------------------

def _cognito_with_holders(holders: dict):
    cognito = MagicMock()

    def _list(**kwargs):
        return {"Users": [{"Username": u}
                          for u in holders.get(kwargs["GroupName"], [])]}

    cognito.list_users_in_group.side_effect = _list
    return cognito


def test_every_holder_loses_the_group_and_is_signed_out():
    """Without the sign-out the grant survives in the current token for up to an hour.

    A revocation that does nothing for an hour is the failure this whole change
    exists to remove, so it is asserted rather than assumed.
    """
    cognito = _cognito_with_holders({"a2a-doomed.skill": ["alice", "bob"]})
    out = sp.revoke_groups(cognito, "pool", ["a2a-doomed.skill"])

    removed = {(c.kwargs["Username"], c.kwargs["GroupName"])
               for c in cognito.admin_remove_user_from_group.call_args_list}
    assert removed == {("alice", "a2a-doomed.skill"), ("bob", "a2a-doomed.skill")}
    assert out["affectedUsers"] == {"alice": ["a2a-doomed.skill"],
                                    "bob": ["a2a-doomed.skill"]}
    assert out["signedOut"] == ["alice", "bob"]
    assert not out["errors"]


def test_a_user_losing_two_groups_is_signed_out_once():
    """`AdminUserGlobalSignOut` shares the same 25 RPS quota as the removals."""
    cognito = _cognito_with_holders({"a2a-doomed.one": ["alice"],
                                     "a2a-doomed.two": ["alice"]})
    out = sp.revoke_groups(cognito, "pool", ["a2a-doomed.one", "a2a-doomed.two"])
    assert out["affectedUsers"] == {"alice": ["a2a-doomed.one", "a2a-doomed.two"]}
    assert cognito.admin_user_global_sign_out.call_count == 1


def test_a_group_nobody_holds_costs_no_writes():
    """The steady state: with everything approved this is a list and no mutations."""
    cognito = _cognito_with_holders({"a2a-doomed.skill": []})
    out = sp.revoke_groups(cognito, "pool", ["a2a-doomed.skill"])
    assert out["affectedUsers"] == {}
    assert cognito.admin_remove_user_from_group.call_count == 0
    assert cognito.admin_user_global_sign_out.call_count == 0


def test_one_failing_removal_does_not_stop_the_sweep():
    """This runs on a timer; one unlucky group must not strand the rest."""
    cognito = _cognito_with_holders({"a2a-doomed.skill": ["alice", "bob"]})

    def _remove(**kwargs):
        if kwargs["Username"] == "alice":
            raise RuntimeError("nope")

    cognito.admin_remove_user_from_group.side_effect = _remove
    out = sp.revoke_groups(cognito, "pool", ["a2a-doomed.skill"])
    assert out["affectedUsers"] == {"bob": ["a2a-doomed.skill"]}
    assert any("alice" in e for e in out["errors"])


def test_a_group_whose_holders_cannot_be_listed_is_reported_not_raised():
    cognito = MagicMock()
    cognito.list_users_in_group.side_effect = RuntimeError("throttled")
    out = sp.revoke_groups(cognito, "pool", ["a2a-doomed.skill"])
    assert out["affectedUsers"] == {}
    assert out["errors"]
