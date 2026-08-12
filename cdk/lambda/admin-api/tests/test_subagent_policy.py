"""Tests for materialising A2A grant intent into Cognito group membership.

The dangerous mistakes here are all quiet:

  - union instead of replace, which widens every narrowed user's access
  - touching a group this module does not own, which could strip `admin`
  - guessing a group name for an unknown recordId, creating a grant no authorizer
    matches and a user who is "granted" but always refused
  - forgetting that a removal needs a forced token refresh, so a revoke appears to
    work and does nothing for an hour
"""
from unittest.mock import MagicMock

import pytest

import a2a_groups
import subagent_policy as sp


# --- merge semantics -------------------------------------------------------

def test_per_user_replaces_global_per_subagent():
    """The semantics every existing deployment already has."""
    global_grants = {"rec-qa": ["answer", "troubleshoot"], "rec-light": ["compose"]}
    user_grants = {"rec-qa": ["answer"]}
    assert sp.effective_grants(global_grants, user_grants) == {
        "rec-qa": ["answer"],        # replaced, so the user has FEWER
        "rec-light": ["compose"],    # inherited
    }


def test_an_empty_user_list_narrows_to_nothing_rather_than_inheriting():
    """How an admin takes a sub-agent away from one user while global keeps it."""
    assert sp.effective_grants({"rec-qa": ["answer"]}, {"rec-qa": []}) == {"rec-qa": []}


def test_no_user_grants_is_pure_inheritance():
    assert sp.effective_grants({"rec-qa": ["answer"]}, {}) == {"rec-qa": ["answer"]}


# --- group naming from intent ---------------------------------------------

def test_wanted_groups_maps_record_ids_through_card_names():
    wanted = sp.wanted_groups(
        {"rec-qa": ["answer_from_docs", "troubleshoot_from_docs"]},
        {"rec-qa": "knowledge-qa-agent"})
    assert wanted == {
        "a2a-knowledge-qa-agent.answer_from_docs",
        "a2a-knowledge-qa-agent.troubleshoot_from_docs",
    }


def test_unknown_record_id_is_skipped_not_guessed():
    """A guessed name is a grant no authorizer matches: granted but always refused."""
    assert sp.wanted_groups({"rec-mystery": ["skill"]}, {}) == set()


def test_unencodable_skill_is_skipped_not_sanitised():
    wanted = sp.wanted_groups({"rec-qa": ["ok_skill", "bad skill"]},
                              {"rec-qa": "knowledge-qa-agent"})
    assert wanted == {"a2a-knowledge-qa-agent.ok_skill"}


# --- materialisation ------------------------------------------------------

def _cognito_stub(existing_groups):
    c = MagicMock()
    c.admin_list_groups_for_user.return_value = {
        "Groups": [{"GroupName": n} for n in existing_groups]}
    c.exceptions.GroupExistsException = type("GroupExistsException", (Exception,), {})
    return c


def test_materialise_adds_and_removes_only_the_difference():
    c = _cognito_stub(["a2a-keep.s1", "a2a-drop.s1", "admin"])
    result = sp.materialise_user(c, "pool", "alice",
                                 {"a2a-keep.s1", "a2a-new.s1"})
    assert result["added"] == ["a2a-new.s1"]
    assert result["removed"] == ["a2a-drop.s1"]
    assert result["narrowed"] is True
    # `admin` is not ours and must survive untouched.
    removed = [kw.kwargs["GroupName"]
               for kw in c.admin_remove_user_from_group.call_args_list]
    assert removed == ["a2a-drop.s1"]


def test_groups_this_module_does_not_own_are_invisible_to_it():
    """`admin` must never appear in a diff, or a reconcile would offer to strip it."""
    c = _cognito_stub(["admin", "some-other-group", "a2a-keep.s1"])
    assert sp.current_groups(c, "pool", "alice") == {"a2a-keep.s1"}


def test_materialise_does_not_create_groups_itself():
    """Groups are shared, so creating them per user is waste — and measured, it
    throttled Cognito hard enough that 33 of 39 users got no membership at all.
    `ensure_groups` does it once, up front."""
    c = _cognito_stub([])
    sp.materialise_user(c, "pool", "alice", {"a2a-new.s1"})
    assert not c.create_group.called, "materialise_user created a group per user again"
    assert c.admin_add_user_to_group.called


def test_ensure_groups_creates_each_once_and_tolerates_existing():
    c = _cognito_stub([])
    calls = []

    def create(**kw):
        calls.append(kw["GroupName"])
        if kw["GroupName"] == "a2a-already.s1":
            raise c.exceptions.GroupExistsException("exists")

    c.create_group.side_effect = create
    failures = sp.ensure_groups(c, "pool", {"a2a-new.s1", "a2a-already.s1"})
    assert sorted(calls) == ["a2a-already.s1", "a2a-new.s1"]
    assert failures == []


def test_throttling_is_retried_rather_than_losing_the_grant():
    """A throttle must not leave a user without a grant the admin just gave."""
    attempts = {"n": 0}

    class Throttle(Exception):
        pass
    Throttle.__name__ = "TooManyRequestsException"

    def flaky(**_kw):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise Throttle("Too many requests")
        return "ok"

    assert sp._with_retry(flaky, attempts=4) == "ok"
    assert attempts["n"] == 3


def test_a_non_throttle_error_is_not_retried():
    calls = {"n": 0}

    def boom(**_kw):
        calls["n"] += 1
        raise ValueError("bad request")

    with pytest.raises(ValueError):
        sp._with_retry(boom)
    assert calls["n"] == 1, "a real error was retried, hiding it behind delay"


def test_pure_addition_is_not_flagged_as_narrowed():
    """Only a removal needs the forced sign-out, so this drives real behaviour."""
    c = _cognito_stub(["a2a-keep.s1"])
    result = sp.materialise_user(c, "pool", "alice",
                                 {"a2a-keep.s1", "a2a-extra.s1"})
    assert result["narrowed"] is False


def test_no_change_touches_nothing():
    c = _cognito_stub(["a2a-keep.s1"])
    result = sp.materialise_user(c, "pool", "alice", {"a2a-keep.s1"})
    assert result == {"username": "alice", "added": [], "removed": [],
                     "narrowed": False}
    assert not c.admin_add_user_to_group.called
    assert not c.admin_remove_user_from_group.called


def test_current_groups_paginates():
    c = MagicMock()
    c.admin_list_groups_for_user.side_effect = [
        {"Groups": [{"GroupName": "a2a-a.s1"}], "NextToken": "t"},
        {"Groups": [{"GroupName": "a2a-b.s1"}]},
    ]
    assert sp.current_groups(c, "pool", "alice") == {"a2a-a.s1", "a2a-b.s1"}


# --- sign-out -------------------------------------------------------------

def test_sign_out_failure_does_not_fail_the_save():
    """The membership change landed; only its timing is off."""
    c = MagicMock()
    c.admin_user_global_sign_out.side_effect = RuntimeError("throttled")
    assert sp.force_token_refresh(c, "pool", "alice") is False


def test_sign_out_success_is_reported():
    c = MagicMock()
    assert sp.force_token_refresh(c, "pool", "alice") is True


# --- reconcile ------------------------------------------------------------

def test_diff_reports_both_directions_without_changing_anything():
    c = _cognito_stub(["a2a-have.s1", "a2a-stale.s1"])
    d = sp.diff_user(c, "pool", "alice", {"a2a-have.s1", "a2a-want.s1"})
    assert d["missing"] == ["a2a-want.s1"]
    assert d["extra"] == ["a2a-stale.s1"]
    assert d["inSync"] is False
    assert not c.admin_add_user_to_group.called
    assert not c.admin_remove_user_from_group.called


def test_in_sync_user_reports_clean():
    c = _cognito_stub(["a2a-have.s1"])
    assert sp.diff_user(c, "pool", "alice", {"a2a-have.s1"})["inSync"] is True


def test_read_intent_normalises_dynamodb_sets():
    table = MagicMock()
    table.get_item.return_value = {"Item": {"a2aGrants": {"rec-a": {"s2", "s1"}}}}
    assert sp.read_intent(table, "alice") == {"rec-a": ["s1", "s2"]}


def test_read_intent_missing_row_is_no_grants():
    table = MagicMock()
    table.get_item.return_value = {}
    assert sp.read_intent(table, "alice") == {}
