"""`_submit_for_approval` has to wait for the record to stop being modified.

Every handler here does the same two things back to back: write the record, then
submit it for review. The write leaves the record transient for a moment, and
`SubmitRegistryRecordForApproval` on a transient record fails with
`ConflictException: Registry record cannot be modified while in CREATING/UPDATING
state`.

That failure is swallowed into a `submitWarning` (on purpose — the record is saved
by then, and a 5xx would invite a duplicate-creating retry), so the visible result
was a 200/201 while the record sat in DRAFT, never queued for review. Measured
against the live service on both the create and the update path.

The old wait was `_poll_until_out_of_creating`, which returned as soon as the status
was anything other than CREATING — and UPDATING is something other than CREATING.
That is the bug these tests pin: waiting for the wrong set of statuses looks
exactly like waiting.
"""
import importlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_mock_table = MagicMock()
_mock_ac = MagicMock()


@pytest.fixture(scope="module", autouse=True)
def _install_mocks():
    lambda_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    if sys.path[0] != lambda_dir:
        sys.path.insert(0, lambda_dir)
    sys.modules.pop("index", None)
    with patch("boto3.resource") as mres, patch("boto3.client") as mclient:
        mres.return_value.Table.return_value = _mock_table
        mclient.return_value = _mock_ac
        import index  # noqa: F401
        importlib.reload(index)
        yield
        sys.modules.pop("index", None)


@pytest.fixture(autouse=True)
def _reset_mocks():
    # `side_effect=True` matters: a plain reset_mock() keeps side effects, so a
    # test that queued a 3-item side_effect leaves the NEXT test's calls raising
    # StopIteration. That surfaced as `_poll_until_settled` silently swallowing
    # the exception and burning its whole timeout — a 60-second test run and a
    # failure that pointed at the wrong function.
    _mock_table.reset_mock(return_value=True, side_effect=True)
    _mock_ac.reset_mock(return_value=True, side_effect=True)
    yield


def test_waits_out_updating_before_submitting():
    """UPDATING is transient too, and that is what the old check missed."""
    import index
    _mock_ac.get_registry_record.side_effect = [
        {"status": "CREATING"}, {"status": "UPDATING"}, {"status": "DRAFT"}]

    assert index._submit_for_approval("rec-1") is None

    assert _mock_ac.get_registry_record.call_count == 3, \
        "submitted without waiting for the record to settle"
    _mock_ac.submit_registry_record_for_approval.assert_called_once_with(
        registryId="test-registry-id", recordId="rec-1")


def test_retries_once_when_the_submit_still_races():
    """The status can flip between the poll and the call."""
    import index
    _mock_ac.get_registry_record.return_value = {"status": "DRAFT"}
    _mock_ac.submit_registry_record_for_approval.side_effect = [
        Exception("ConflictException: cannot be modified while in UPDATING state"),
        None,
    ]

    assert index._submit_for_approval("rec-2") is None
    assert _mock_ac.submit_registry_record_for_approval.call_count == 2


def test_a_real_error_is_reported_not_retried_forever():
    """Only the transient message is retried; anything else is surfaced once."""
    import index
    _mock_ac.get_registry_record.return_value = {"status": "DRAFT"}
    _mock_ac.submit_registry_record_for_approval.side_effect = Exception(
        "AccessDeniedException: not authorized")

    err = index._submit_for_approval("rec-3")
    assert "AccessDenied" in err
    assert _mock_ac.submit_registry_record_for_approval.call_count == 1


def test_the_wait_gives_up_rather_than_blocking_the_request():
    """A record stuck transient must not hold the Lambda open forever.

    Real seconds, deliberately: the clock is not patched here. Patching
    `index.time` replaces the attribute on the `time` MODULE, which every other
    library in the process shares — including pytest's own bookkeeping. A one
    second timeout is cheap enough not to need it.
    """
    import index
    _mock_ac.get_registry_record.return_value = {"status": "UPDATING"}

    assert index._poll_until_settled("rec-4", timeout_seconds=1) == "UPDATING"
    # Bounded: ~1s at 0.5s per turn, not an unbounded loop.
    assert 1 <= _mock_ac.get_registry_record.call_count <= 4


def test_update_route_reports_no_submit_warning_once_it_settles():
    """The end state the user cares about: an edit is queued for review."""
    import index
    _mock_ac.get_registry_record.return_value = {
        "recordId": "rec-5", "name": "my-skill", "description": "before",
        "status": "APPROVED",
        "descriptors": {"agentSkillsDefinition": {
            "data": "{}",
            "additionalData": {"skillMd": {"data": (
                "---\nname: my-skill\ndescription: \"d\"\n---\n\nbody\n")}},
        }},
    }
    _mock_table.get_item.return_value = {"Item": {"ownerSub": "alice-sub"}}

    resp = index.handler({
        "httpMethod": "PUT", "resource": "/my-skills/{recordId}",
        "pathParameters": {"recordId": "rec-5"},
        "body": json.dumps({"description": "after"}),
        "requestContext": {"authorizer": {"claims": {
            "sub": "alice-sub", "email": "alice@example.com"}}},
    }, None)

    assert resp["statusCode"] == 200
    assert "submitWarning" not in json.loads(resp["body"])
    _mock_ac.submit_registry_record_for_approval.assert_called_once()
