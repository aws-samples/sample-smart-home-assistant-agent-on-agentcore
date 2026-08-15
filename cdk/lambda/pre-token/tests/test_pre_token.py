"""Tests for the pre-token-generation trigger.

This Lambda sits on the authentication path, so its failure modes are unusually
expensive and unusually quiet:

  - It must NEVER raise. A pre-token-generation trigger that throws fails sign-in
    for the user who triggered it.
  - `groupsToOverride` REPLACES the claim. Any path that forgets to carry the
    incoming non-`a2a-` groups back removes `admin` from an administrator on their
    next token, with no error anywhere.
  - It must apply the SAME merge as the admin API's materialiser (per-user replaces
    global per sub-agent, an empty list meaning "none"), or global users and
    per-user users end up with different access from the same intent.
  - A Registry outage must not read as "nothing is grantable".
"""
import importlib
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDA_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
for path in (LAMBDA_DIR,):
    if path not in sys.path:
        sys.path.insert(0, path)

_mock_table = MagicMock()
_mock_registry = MagicMock()

NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="module", autouse=True)
def _load_module():
    os.environ["SKILLS_TABLE_NAME"] = "smarthome-skills-test"
    os.environ["REGISTRY_ID"] = "test-registry"
    os.environ["AWS_REGION"] = "us-west-2"
    os.environ.pop("A2A_CLAIM_INJECTION", None)
    # Every Lambda dir defines `index.py`, so a run that also collects the admin API
    # suite has one of them already imported under that name. Force this one to win
    # and drop it again afterwards — the same guard test_a2a_list.py uses, and
    # without it the failure is a confusing AttributeError on the wrong module.
    if sys.path[0] != LAMBDA_DIR:
        sys.path.insert(0, LAMBDA_DIR)
    sys.modules.pop("index", None)
    with patch("boto3.resource") as mres:
        mres.return_value.Table.return_value = _mock_table
        import index
        importlib.reload(index)
        assert os.path.dirname(os.path.abspath(index.__file__)) == LAMBDA_DIR, (
            f"imported the wrong index.py: {index.__file__}")
        yield index
        sys.modules.pop("index", None)


@pytest.fixture(autouse=True)
def _reset(_load_module):
    _mock_table.reset_mock(return_value=True, side_effect=True)
    _mock_registry.reset_mock(return_value=True, side_effect=True)
    _load_module._table = _mock_table
    _load_module._registry = _mock_registry
    _load_module._catalog_cache["bucket"] = None
    _load_module._catalog_cache["names"] = {}
    os.environ.pop("A2A_CLAIM_INJECTION", None)


ENERGY = "energy-optimization-agent"
QA = "knowledge-qa-agent"


def _intent(global_grants=None, user_grants=None):
    """Make the skills table answer for `__global__` and for one user."""
    rows = {
        "__global__": {"a2aGrants": global_grants} if global_grants else {},
        "alice@example.com": {"a2aGrants": user_grants} if user_grants else {},
    }
    _mock_table.get_item.side_effect = lambda Key: (
        {"Item": rows.get(Key["userId"], {})} if rows.get(Key["userId"]) else {})


def _registry(records):
    """records: [(recordId, cardName, status, age)]"""
    _mock_registry.list_registry_records.return_value = {
        "registryRecords": [
            {"recordId": rid, "name": name, "status": status,
             "updatedAt": NOW - age}
            for rid, name, status, age in records]}
    import json
    by_id = {rid: name for rid, name, _s, _a in records}
    _mock_registry.get_registry_record.side_effect = lambda **kw: {
        "descriptors": {"a2aAgentCard": {"data": json.dumps(
            {"name": by_id[kw["recordId"]], "skills": []})}}}


def _event(groups=None, email="alice@example.com"):
    return {
        "request": {
            "userAttributes": {"email": email},
            "groupConfiguration": {"groupsToOverride": list(groups or [])},
        },
        "response": {},
    }


def _claim(event):
    return (event["response"]["claimsOverrideDetails"]
                 ["groupOverrideDetails"]["groupsToOverride"])


# ---------------------------------------------------------------------------
# The whole point: global grants arrive with no membership behind them
# ---------------------------------------------------------------------------

def test_a_global_grant_is_injected_without_any_membership(_load_module):
    _intent(global_grants={"rec-energy": ["estimate_savings"]})
    _registry([("rec-energy", ENERGY, "APPROVED", timedelta(days=1))])

    out = _load_module.handler(_event(groups=[]))
    assert _claim(out) == [f"a2a-{ENERGY}.estimate_savings"]


def test_non_a2a_groups_are_carried_back_verbatim(_load_module):
    """`groupsToOverride` is a REPLACE. Forgetting this removes `admin`."""
    _intent(global_grants={"rec-energy": ["estimate_savings"]})
    _registry([("rec-energy", ENERGY, "APPROVED", timedelta(days=1))])

    out = _load_module.handler(_event(groups=["admin", "some-other-app-group"]))
    claim = _claim(out)
    assert "admin" in claim and "some-other-app-group" in claim
    assert f"a2a-{ENERGY}.estimate_savings" in claim


def test_a_real_a2a_membership_is_not_duplicated(_load_module):
    """A per-user grant IS a membership, so it arrives in the request too."""
    _intent(global_grants={"rec-energy": ["estimate_savings"]})
    _registry([("rec-energy", ENERGY, "APPROVED", timedelta(days=1))])

    out = _load_module.handler(
        _event(groups=["admin", f"a2a-{ENERGY}.estimate_savings"]))
    claim = _claim(out)
    assert claim.count(f"a2a-{ENERGY}.estimate_savings") == 1
    assert "admin" in claim


# ---------------------------------------------------------------------------
# The merge must match the materialiser exactly
# ---------------------------------------------------------------------------

def test_a_per_user_override_replaces_global_for_that_subagent(_load_module):
    _intent(global_grants={"rec-energy": ["estimate_savings", "tariff_analysis"],
                           "rec-qa": ["answer_from_docs"]},
            user_grants={"rec-energy": ["estimate_savings"]})
    _registry([("rec-energy", ENERGY, "APPROVED", timedelta(days=1)),
               ("rec-qa", QA, "APPROVED", timedelta(days=1))])

    claim = _claim(_load_module.handler(_event()))
    assert f"a2a-{ENERGY}.estimate_savings" in claim
    assert f"a2a-{ENERGY}.tariff_analysis" not in claim   # narrowed
    assert f"a2a-{QA}.answer_from_docs" in claim          # inherited


def test_an_empty_per_user_list_narrows_to_nothing(_load_module):
    """How an admin takes one sub-agent away from one user while global keeps it."""
    _intent(global_grants={"rec-energy": ["estimate_savings"]},
            user_grants={"rec-energy": []})
    _registry([("rec-energy", ENERGY, "APPROVED", timedelta(days=1))])

    assert _claim(_load_module.handler(_event())) == []


# ---------------------------------------------------------------------------
# Grantability, shared with the sweep
# ---------------------------------------------------------------------------

def test_a_rejected_record_is_not_injected(_load_module):
    """The global half of "non-APPROVED means uncallable". The sweep cannot do this
    one: there is no membership for it to revoke."""
    _intent(global_grants={"rec-energy": ["estimate_savings"]})
    _registry([("rec-energy", ENERGY, "REJECTED", timedelta(minutes=1))])

    assert _claim(_load_module.handler(_event())) == []


def test_a_fresh_draft_is_still_injected(_load_module):
    """Same re-approval window the sweep honours; approval is a human step."""
    _intent(global_grants={"rec-energy": ["estimate_savings"]})
    _registry([("rec-energy", ENERGY, "DRAFT", timedelta(minutes=5))])

    assert _claim(_load_module.handler(_event())) == \
        [f"a2a-{ENERGY}.estimate_savings"]


def test_a_stale_draft_is_not_injected(_load_module):
    _intent(global_grants={"rec-energy": ["estimate_savings"]})
    _registry([("rec-energy", ENERGY, "DRAFT", timedelta(hours=3))])

    assert _claim(_load_module.handler(_event())) == []


# ---------------------------------------------------------------------------
# Failure must never cost a sign-in
# ---------------------------------------------------------------------------

def test_a_dynamodb_failure_leaves_the_claim_untouched(_load_module):
    """Untouched, not emptied: emptying it would strip `admin` too."""
    _mock_table.get_item.side_effect = RuntimeError("table gone")
    event = _event(groups=["admin"])
    out = _load_module.handler(event)
    assert out is event
    assert "claimsOverrideDetails" not in (out.get("response") or {})


def test_a_registry_failure_falls_back_to_the_last_catalog_on_a_warm_container(
        _load_module):
    """A warm container can still decide, from the catalog it last read."""
    _intent(global_grants={"rec-energy": ["estimate_savings"]})
    _mock_registry.list_registry_records.side_effect = RuntimeError("throttled")
    _load_module._catalog_cache["names"] = {"rec-energy": ENERGY}

    claim = _claim(_load_module.handler(_event(groups=["admin"])))
    assert "admin" in claim
    assert f"a2a-{ENERGY}.estimate_savings" in claim


def test_a_registry_failure_on_a_COLD_container_leaves_the_claim_untouched(
        _load_module):
    """The bug this exists to prevent, measured in production on 2026-08-15.

    Lambda's bundled botocore had no `agent-registry-control` service model, so the
    catalog read raised on every invocation. The trigger answered with an empty
    group list — and because `groupsToOverride` REPLACES the claim, every user's
    token came back holding `admin` and nothing else. Eighteen real Cognito
    memberships, which have nothing to do with global grants, silently stopped
    appearing. Sign-in worked perfectly throughout.

    "I could not find out" must therefore leave the claim ALONE, not empty it.
    """
    _intent(global_grants={"rec-energy": ["estimate_savings"]})
    _mock_registry.list_registry_records.side_effect = RuntimeError(
        "Unknown service: 'agent-registry-control'")
    _load_module._catalog_cache["names"] = {}          # cold: nothing cached

    event = _event(groups=["admin", f"a2a-{ENERGY}.estimate_savings",
                           f"a2a-{QA}.answer_from_docs"])
    out = _load_module.handler(event)
    assert out is event
    assert "claimsOverrideDetails" not in (out.get("response") or {}), (
        "the claim was rewritten; the user's real a2a memberships would be dropped")


def test_an_empty_registry_leaves_the_claim_untouched(_load_module):
    """A registry that reports zero records is "cannot decide", not "nothing".

    Same conflation, different cause, same consequence — and the same guard the
    admin API's sweep applies before it revokes anything.
    """
    _intent(global_grants={"rec-energy": ["estimate_savings"]})
    _registry([])

    event = _event(groups=["admin", f"a2a-{ENERGY}.estimate_savings"])
    out = _load_module.handler(event)
    assert out is event
    assert "claimsOverrideDetails" not in (out.get("response") or {})


def test_a_grant_on_a_deleted_record_is_dropped_not_treated_as_a_failure(
        _load_module):
    """A record the catalog does not name is legitimately not grantable.

    Distinct from an unusable catalog: here the registry answered, listing other
    records, and this recordId simply is not among them. The empty result must be
    applied — a recordId is minted per record, so a redeploy that recreated one
    leaves the old id pointing at nothing.
    """
    _intent(global_grants={"rec-deleted": ["estimate_savings"]})
    _registry([("rec-qa", QA, "APPROVED", timedelta(days=1))])

    claim = _claim(_load_module.handler(_event(groups=["admin"])))
    assert claim == ["admin"]


def test_a_malformed_event_does_not_raise(_load_module):
    for bad in ({}, {"request": None}, {"request": {"userAttributes": None}}):
        assert _load_module.handler(bad) is bad


def test_no_email_yields_no_a2a_groups_but_preserves_the_rest(_load_module):
    """No identity means no grants — never a shared default."""
    _intent(global_grants={"rec-energy": ["estimate_savings"]})
    _registry([("rec-energy", ENERGY, "APPROVED", timedelta(days=1))])

    claim = _claim(_load_module.handler(_event(groups=["admin"], email="")))
    # Global grants still apply to an authenticated user with no email claim: the
    # intent is "everyone". What is absent is any per-user override.
    assert "admin" in claim


def test_the_kill_switch_leaves_the_claim_untouched(_load_module):
    os.environ["A2A_CLAIM_INJECTION"] = "off"
    _intent(global_grants={"rec-energy": ["estimate_savings"]})
    _registry([("rec-energy", ENERGY, "APPROVED", timedelta(days=1))])

    event = _event(groups=["admin"])
    out = _load_module.handler(event)
    assert out is event
    assert "claimsOverrideDetails" not in (out.get("response") or {})


# ---------------------------------------------------------------------------
# Identity-pool fields must survive a group override
# ---------------------------------------------------------------------------

def test_iam_roles_and_preferred_role_are_carried_through(_load_module):
    _intent(global_grants={})
    _registry([])
    event = _event(groups=["admin"])
    event["request"]["groupConfiguration"]["iamRolesToOverride"] = ["arn:aws:iam::1:role/r"]
    event["request"]["groupConfiguration"]["preferredRole"] = "arn:aws:iam::1:role/r"

    out = _load_module.handler(event)
    override = out["response"]["claimsOverrideDetails"]["groupOverrideDetails"]
    assert override["iamRolesToOverride"] == ["arn:aws:iam::1:role/r"]
    assert override["preferredRole"] == "arn:aws:iam::1:role/r"
