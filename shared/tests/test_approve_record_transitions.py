"""`approve_record` must not report a success it did not achieve.

The record state machine is narrower than its enum: DRAFT reaches APPROVED only via
SubmitRegistryRecordForApproval, and DEPRECATED reaches nothing at all. Measured
against the live GA service on 2026-08-15, every target from DEPRECATED answers
`ValidationException: Cannot update registry record in DEPRECATED status (terminal
state)`.

The function used to fall through for any status it did not handle and RETURN that
status. So an approve on a deprecated record answered 200 with `status:
DEPRECATED` — the caller had to notice the field to learn nothing had happened. That
was already fixed once, for REJECTED; these tests pin the general shape so it cannot
come back for the next status.

Why it matters more than a cosmetic error: recovering a DEPRECATED record means
recreating it, which mints a new recordId, and `__a2a_permissions__` keys grants by
recordId. A silent no-op therefore reads as "approval didn't stick" while the real
situation is "every grant on this agent is void until an admin repoints it".
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_registry as ar  # noqa: E402


def _client(*statuses):
    """A client whose GetRegistryRecord walks `statuses`, repeating the last."""
    client = MagicMock()
    seq = list(statuses)

    def _get(**_kw):
        return {"status": seq.pop(0) if len(seq) > 1 else seq[0]}

    client.get_registry_record.side_effect = _get
    return client


def test_an_already_approved_record_is_a_no_op():
    client = _client(ar.STATUS_APPROVED)
    assert ar.approve_record(client, "reg", "rec") == ar.STATUS_APPROVED
    client.update_registry_record_status.assert_not_called()


def test_a_draft_is_submitted_then_approved():
    client = _client(ar.STATUS_DRAFT, ar.STATUS_PENDING_APPROVAL, ar.STATUS_APPROVED)
    assert ar.approve_record(client, "reg", "rec") == ar.STATUS_APPROVED
    client.submit_registry_record_for_approval.assert_called_once()
    client.update_registry_record_status.assert_called_once()


def test_a_rejected_record_goes_straight_to_approved():
    """Lets a reviewer change their mind without asking the author to republish."""
    client = _client(ar.STATUS_REJECTED, ar.STATUS_APPROVED)
    assert ar.approve_record(client, "reg", "rec") == ar.STATUS_APPROVED
    client.submit_registry_record_for_approval.assert_not_called()


def test_a_deprecated_record_raises_rather_than_reporting_its_own_status():
    client = _client(ar.STATUS_DEPRECATED)
    with pytest.raises(ar.RecordNotApprovable) as exc:
        ar.approve_record(client, "reg", "rec")
    # The message has to carry the consequence, not just the refusal: recreating is
    # the only way back and it voids grants keyed on the old recordId.
    assert "terminal" in str(exc.value)
    assert "recordId" in str(exc.value)
    client.update_registry_record_status.assert_not_called()


def test_a_status_that_never_settles_raises_too():
    """A record stuck in UPDATING past the timeout has not been approved."""
    client = _client(ar.STATUS_UPDATING)
    with pytest.raises(ar.RecordNotApprovable):
        ar.approve_record(client, "reg", "rec", timeout=0)


def test_an_unknown_status_raises():
    client = _client("SOMETHING_NEW")
    with pytest.raises(ar.RecordNotApprovable):
        ar.approve_record(client, "reg", "rec")
