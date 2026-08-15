"""Tests for the one rule that decides whether a record's skills may be held.

Two enforcement points read this: the admin Lambda's sweep (which revokes `a2a-`
Cognito group memberships) and the pre-token-generation trigger (which decides
which groups to inject for GLOBAL grants, since those are not memberships at all).
If they disagreed, the same record in the same state would leave global users
working and per-user users revoked.

The grace window exists because approval is a HUMAN step and any edit to an
APPROVED record resets it to DRAFT. Without it, every version bump would revoke
every user's grants. With it, a genuinely rejected record still loses them at once
— which is the distinction these tests pin down.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_registry as ar  # noqa: E402

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()
GRACE = 3600


def _record(status, updated_delta=timedelta(0)):
    return {"status": status, "updatedAt": NOW + updated_delta}


# ---------------------------------------------------------------------------
# Approved and judged
# ---------------------------------------------------------------------------

def test_approved_is_grantable_regardless_of_age():
    ok, why = ar.grantable(_record(ar.STATUS_APPROVED, timedelta(days=-90)),
                           NOW_TS, GRACE)
    assert ok and why == "approved"


def test_rejected_is_refused_immediately():
    """A judgement, not a transition — no window."""
    ok, why = ar.grantable(_record(ar.STATUS_REJECTED), NOW_TS, GRACE)
    assert not ok and "REJECTED" in why


def test_deprecated_is_refused_immediately():
    ok, why = ar.grantable(_record(ar.STATUS_DEPRECATED), NOW_TS, GRACE)
    assert not ok and "DEPRECATED" in why


def test_a_missing_record_is_refused_with_a_usable_reason():
    """A deleted record's groups must go, and the admin must be told why."""
    ok, why = ar.grantable(None, NOW_TS, GRACE)
    assert not ok and "no such record" in why


def test_an_unknown_status_is_refused_rather_than_assumed_safe():
    ok, why = ar.grantable(_record("SOMETHING_NEW"), NOW_TS, GRACE)
    assert not ok and "SOMETHING_NEW" in why


def test_an_absent_status_is_refused():
    ok, _ = ar.grantable({"updatedAt": NOW}, NOW_TS, GRACE)
    assert not ok


# ---------------------------------------------------------------------------
# In flight: the re-approval window
# ---------------------------------------------------------------------------

def test_a_fresh_draft_keeps_its_grants():
    """The version-bump case: an edit resets APPROVED to DRAFT.

    Revoking here would mean every deploy that touches a card revokes every
    user's grants until a human clicks approve.
    """
    ok, why = ar.grantable(_record(ar.STATUS_DRAFT, timedelta(minutes=-5)),
                           NOW_TS, GRACE)
    assert ok and "window" in why


def test_pending_approval_keeps_its_grants_inside_the_window():
    ok, _ = ar.grantable(_record(ar.STATUS_PENDING_APPROVAL, timedelta(minutes=-59)),
                         NOW_TS, GRACE)
    assert ok


def test_a_stale_draft_loses_its_grants():
    ok, why = ar.grantable(_record(ar.STATUS_DRAFT, timedelta(hours=-2)),
                           NOW_TS, GRACE)
    assert not ok and "past the" in why


def test_the_window_boundary_is_inclusive():
    assert ar.grantable(_record(ar.STATUS_DRAFT, timedelta(seconds=-GRACE)),
                        NOW_TS, GRACE)[0]
    assert not ar.grantable(_record(ar.STATUS_DRAFT, timedelta(seconds=-GRACE - 1)),
                            NOW_TS, GRACE)[0]


def test_a_zero_window_refuses_an_in_flight_record():
    """`A2A_GRANT_GRACE_SECONDS=0` must mean what it says.

    A record whose timestamp is exactly `now` is still inside a zero window — the
    comparison is inclusive — but nothing observed by a sweep is ever that fresh,
    so one second of age is the honest case to pin.
    """
    ok, _ = ar.grantable(_record(ar.STATUS_DRAFT, timedelta(seconds=-1)), NOW_TS, 0)
    assert not ok


def test_an_unreadable_timestamp_is_refused_not_treated_as_just_now():
    """Otherwise a record with no timestamp stays grantable forever."""
    ok, why = ar.grantable({"status": ar.STATUS_DRAFT}, NOW_TS, GRACE)
    assert not ok and "timestamp" in why


# ---------------------------------------------------------------------------
# Timestamp shapes: boto3 gives datetimes, the console API gives ISO strings
# ---------------------------------------------------------------------------

def test_an_iso_string_timestamp_is_understood():
    ok, _ = ar.grantable(
        {"status": ar.STATUS_DRAFT, "updatedAt": "2026-08-15T11:55:00+00:00"},
        NOW_TS, GRACE)
    assert ok


def test_a_trailing_z_timestamp_is_understood():
    ok, _ = ar.grantable(
        {"status": ar.STATUS_DRAFT, "updatedAt": "2026-08-15T11:55:00Z"},
        NOW_TS, GRACE)
    assert ok


def test_created_at_is_used_when_a_record_has_never_been_updated():
    ok, _ = ar.grantable(
        {"status": ar.STATUS_DRAFT, "createdAt": NOW - timedelta(minutes=1)},
        NOW_TS, GRACE)
    assert ok


# ---------------------------------------------------------------------------
# Configuration and the console's countdown
# ---------------------------------------------------------------------------

def test_the_grace_default_applies_when_unset_or_blank():
    assert ar.grant_grace_seconds({}) == ar.DEFAULT_GRANT_GRACE_SECONDS
    assert ar.grant_grace_seconds({"A2A_GRANT_GRACE_SECONDS": ""}) == \
        ar.DEFAULT_GRANT_GRACE_SECONDS


def test_the_grace_is_configurable_and_a_bad_value_falls_back():
    assert ar.grant_grace_seconds({"A2A_GRANT_GRACE_SECONDS": "120"}) == 120
    # Read on the token path; a typo must not break sign-in.
    assert ar.grant_grace_seconds({"A2A_GRANT_GRACE_SECONDS": "soon"}) == \
        ar.DEFAULT_GRANT_GRACE_SECONDS


def test_remaining_seconds_counts_down_for_an_in_flight_record():
    assert ar.grace_remaining_seconds(
        _record(ar.STATUS_DRAFT, timedelta(minutes=-10)), NOW_TS, GRACE) == 3000


def test_remaining_seconds_is_none_when_nothing_is_on_the_clock():
    for record in (_record(ar.STATUS_APPROVED),
                   _record(ar.STATUS_REJECTED),
                   _record(ar.STATUS_DRAFT, timedelta(hours=-2)),
                   {"status": ar.STATUS_DRAFT},
                   None):
        assert ar.grace_remaining_seconds(record, NOW_TS, GRACE) is None
