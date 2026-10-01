"""The pure half of Registry event notifications: parse, key, summarize, fold.

An EventBridge event from AWS Agent Registry carries only `registryRecordId` and
`registryId`, so nothing here renders on its own — the enrichment happens in the
handler. What is tested here is the part that decides WHICH events count and WHICH
of them an admin still has to act on, because that is the part a wrong answer costs
attention: a Pending Approval that stays highlighted after the record was approved
teaches the admin to ignore the panel.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDA_DIR = os.path.dirname(HERE)
if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)

import registry_events as re_  # noqa: E402


def _event(detail_type="Registry Record State changed to Pending Approval",
           source="aws.agent-registry", record_id="rec-1", registry_id="reg-1",
           event_id="evt-1", time="2026-09-19T07:00:00Z", detail=None):
    if detail is None:
        detail = {"registryRecordId": record_id, "registryId": registry_id}
    return {
        "version": "0",
        "id": event_id,
        "detail-type": detail_type,
        "source": source,
        "account": "123456789012",
        "time": time,
        "region": "us-west-2",
        "resources": [f"arn:aws:agent-registry:us-west-2:123456789012:registry/{registry_id}/record/{record_id}"],
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# parse_event
# ---------------------------------------------------------------------------

def test_parse_pending_event_yields_transition_and_ids():
    parsed = re_.parse_event(_event())
    assert parsed == {
        "eventId": "evt-1",
        "detailType": "Registry Record State changed to Pending Approval",
        "transition": "PENDING_APPROVAL",
        "recordId": "rec-1",
        "registryId": "reg-1",
        "recordArn": "arn:aws:agent-registry:us-west-2:123456789012:registry/reg-1/record/rec-1",
        "occurredAt": "2026-09-19T07:00:00Z",
    }


@pytest.mark.parametrize("detail_type,transition", [
    ("Registry Record State changed to Draft", "DRAFT"),
    ("Registry Record State changed to Approved", "APPROVED"),
    ("Registry Record State changed to Rejected", "REJECTED"),
    ("Registry Record State changed to Deprecated", "DEPRECATED"),
])
def test_parse_maps_every_record_transition(detail_type, transition):
    assert re_.parse_event(_event(detail_type=detail_type))["transition"] == transition


def test_parse_ignores_other_sources():
    assert re_.parse_event(_event(source="aws.bedrock-agentcore")) is None


def test_parse_ignores_registry_lifecycle_events():
    assert re_.parse_event(_event(detail_type="Registry Ready")) is None


def test_parse_ignores_event_missing_record_id():
    assert re_.parse_event(_event(detail={"registryId": "reg-1"})) is None


def test_parse_tolerates_missing_resources():
    evt = _event()
    evt.pop("resources")
    assert re_.parse_event(evt)["recordArn"] == ""


# ---------------------------------------------------------------------------
# event_key
# ---------------------------------------------------------------------------

def test_event_key_orders_by_time_then_disambiguates_by_event_id():
    a = re_.event_key("2026-09-19T07:00:00Z", "evt-a")
    b = re_.event_key("2026-09-19T07:00:00Z", "evt-b")
    later = re_.event_key("2026-09-19T07:00:01Z", "evt-a")
    assert a != b
    assert a < later and b < later


# ---------------------------------------------------------------------------
# summarize_record
# ---------------------------------------------------------------------------

def test_summarize_prefers_display_name_and_reads_ga_fields():
    detail = {
        "recordId": "rec-1", "name": "acme-weather-1", "displayName": "Weather",
        "recordType": "SKILL", "recordVersion": "3", "status": "PENDING_APPROVAL",
        "description": "Forecasts", "statusReason": "submitted",
    }
    assert re_.summarize_record(detail) == {
        "recordType": "SKILL", "name": "Weather", "recordVersion": "3",
        "description": "Forecasts", "statusReason": "submitted",
    }


def test_summarize_falls_back_to_name_and_empty_strings():
    assert re_.summarize_record({"name": "legacy"}) == {
        "recordType": "", "name": "legacy", "recordVersion": "",
        "description": "", "statusReason": "",
    }


# ---------------------------------------------------------------------------
# fold — which rows still need a decision
# ---------------------------------------------------------------------------

def _row(record_id, transition, key):
    return {"recordId": record_id, "transition": transition, "eventKey": key}


def test_fold_marks_latest_pending_as_actionable():
    out = re_.fold([_row("rec-1", "PENDING_APPROVAL", "2#b")])
    assert out["pendingCount"] == 1
    assert out["events"][0]["actionable"] is True


def test_fold_clears_pending_once_the_record_moves_on():
    # Newest first, as the DynamoDB query returns them.
    out = re_.fold([
        _row("rec-1", "APPROVED", "2#b"),
        _row("rec-1", "PENDING_APPROVAL", "1#a"),
    ])
    assert out["pendingCount"] == 0
    assert [e["actionable"] for e in out["events"]] == [False, False]


def test_fold_highlights_only_the_latest_of_two_pendings():
    out = re_.fold([
        _row("rec-1", "PENDING_APPROVAL", "2#b"),
        _row("rec-1", "PENDING_APPROVAL", "1#a"),
    ])
    assert out["pendingCount"] == 1
    assert [e["actionable"] for e in out["events"]] == [True, False]


def test_fold_counts_pendings_across_records():
    out = re_.fold([
        _row("rec-2", "PENDING_APPROVAL", "3#c"),
        _row("rec-1", "PENDING_APPROVAL", "2#b"),
    ])
    assert out["pendingCount"] == 2


def test_fold_sorts_newest_first_regardless_of_input_order():
    out = re_.fold([
        _row("rec-1", "PENDING_APPROVAL", "1#a"),
        _row("rec-1", "APPROVED", "2#b"),
    ])
    assert [e["eventKey"] for e in out["events"]] == ["2#b", "1#a"]
    assert out["pendingCount"] == 0


def test_fold_of_nothing_is_empty():
    assert re_.fold([]) == {"events": [], "pendingCount": 0}


# EventBridge stamps `time` to the second, and the Skill ERP's create-and-submit
# produces a Draft and a Pending Approval inside the same second (measured
# 2026-09-19: 07:59:09Z for both). Their ids then decide the order, and when the
# Draft's id happens to sort higher the record's "latest" state reads as Draft and
# the submission is never highlighted. Within one second the only plausible order
# is the state machine's, so that is the tiebreak; the arrival time comes last.

def _stamped(record_id, transition, occurred, event_id, received):
    return {"recordId": record_id, "transition": transition,
            "eventKey": f"{occurred}#{event_id}", "occurredAt": occurred,
            "receivedAt": received}


def test_fold_breaks_a_same_second_tie_by_state_machine_order():
    out = re_.fold([
        _stamped("rec-1", "DRAFT", "2026-09-19T07:59:09Z", "dcb1", "2026-09-19T07:59:10.783+00:00"),
        _stamped("rec-1", "PENDING_APPROVAL", "2026-09-19T07:59:09Z", "dc12", "2026-09-19T07:59:10.848+00:00"),
    ])
    assert [e["transition"] for e in out["events"]] == ["PENDING_APPROVAL", "DRAFT"]
    assert out["events"][0]["actionable"] is True
    assert out["pendingCount"] == 1


def test_fold_same_second_tie_ignores_arrival_order_when_states_differ():
    # Delivered out of order: the Draft arrived AFTER the Pending. Still Pending wins.
    out = re_.fold([
        _stamped("rec-1", "PENDING_APPROVAL", "2026-09-19T07:59:09Z", "a", "2026-09-19T07:59:10.100+00:00"),
        _stamped("rec-1", "DRAFT", "2026-09-19T07:59:09Z", "b", "2026-09-19T07:59:10.900+00:00"),
    ])
    assert out["events"][0]["transition"] == "PENDING_APPROVAL"
    assert out["pendingCount"] == 1


def test_fold_a_later_second_still_beats_state_order():
    # A Draft one second AFTER the Pending is a real edit and must win.
    out = re_.fold([
        _stamped("rec-1", "PENDING_APPROVAL", "2026-09-19T07:59:09Z", "a", "2026-09-19T07:59:10.1+00:00"),
        _stamped("rec-1", "DRAFT", "2026-09-19T07:59:10Z", "b", "2026-09-19T07:59:11.1+00:00"),
    ])
    assert out["events"][0]["transition"] == "DRAFT"
    assert out["pendingCount"] == 0
