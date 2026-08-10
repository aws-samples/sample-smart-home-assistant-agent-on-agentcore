"""Approving a skill from the Admin Console.

The approval state machine is Registry-managed and had no caller in this repo:
`agent-registry:UpdateRegistryRecordStatus` was granted to this Lambda in the CDK
stack and never invoked, so a skill published from the Skill ERP sat in
PENDING_APPROVAL and the only way to move it was the AWS console.

The transitions were measured against a throwaway record rather than read off the
docs, because this API has already contradicted them six times in this project:

    DRAFT            -> PENDING_APPROVAL | DEPRECATED | DRAFT   (NOT REJECTED)
    PENDING_APPROVAL -> APPROVED | REJECTED
    REJECTED         -> APPROVED                                (reversible)

So the interesting cases are the ones the API refuses. Rejecting a DRAFT record
raises a ValidationException whose message is a bare list of enum members; that is
turned into an explanation, because an operator reading "PENDING_APPROVAL,
DEPRECATED, DRAFT, UPDATING" has no way to know the record simply has not been
submitted yet.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDA_DIR = os.path.dirname(HERE)
sys.path.insert(0, LAMBDA_DIR)

import index  # noqa: E402


def _event(**body):
    return {
        "httpMethod": "POST",
        "resource": "/registry/records",
        "body": json.dumps(body),
        "requestContext": {"authorizer": {"claims": {
            "email": "reviewer@smarthome.local", "cognito:groups": "admin"}}},
    }


def _review(status="PENDING_APPROVAL", **body):
    """Run the handler against a record in the given status."""
    control = MagicMock()
    control.get_registry_record.return_value = {"status": status}
    with patch.object(index, "registry_control", control), \
         patch.object(index, "REGISTRY_ID", "reg-1"), \
         patch.object(index.registry_ns, "approve_record",
                      return_value="APPROVED") as approve, \
         patch.object(index.registry_ns, "set_record_status") as set_status:
        out = index.review_registry_record(_event(**body))
    return out, json.loads(out["body"]), approve, set_status


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------

def test_approving_a_pending_record_calls_the_registry():
    out, body, approve, _ = _review(recordId="rec-1", decision="approve")
    assert out["statusCode"] == 200
    assert body["status"] == "APPROVED"
    assert body["previousStatus"] == "PENDING_APPROVAL"
    approve.assert_called_once()


def test_approving_a_draft_record_works_because_approve_submits_first():
    """A DRAFT record cannot go straight to APPROVED — `approve_record` submits it
    for approval and then approves, which is why approve accepts both states."""
    out, body, approve, _ = _review(status="DRAFT", recordId="rec-1",
                                    decision="approve")
    assert out["statusCode"] == 200
    approve.assert_called_once()


def test_the_reviewer_is_recorded_in_the_status_reason():
    """`statusReason` is the only place the Registry keeps *why*, so it is also the
    only place an audit can learn *who*."""
    _, body, approve, _ = _review(recordId="rec-1", decision="approve",
                                  reason="looks good")
    assert body["reviewedBy"] == "reviewer@smarthome.local"
    assert "reviewer@smarthome.local" in approve.call_args.kwargs["reason"]
    assert "looks good" in approve.call_args.kwargs["reason"]


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------

def test_rejecting_requires_a_reason():
    """The author sees only the statusReason. An unexplained rejection is
    indistinguishable from the system having lost their skill."""
    out, body, _, set_status = _review(recordId="rec-1", decision="reject")
    assert out["statusCode"] == 400
    assert "reason is required" in body["error"]
    set_status.assert_not_called()


def test_rejecting_a_pending_record_sets_rejected():
    out, body, _, set_status = _review(recordId="rec-1", decision="reject",
                                       reason="tools too broad")
    assert out["statusCode"] == 200
    assert body["status"] == "REJECTED"
    assert set_status.call_args.args[3] == "REJECTED"
    assert "tools too broad" in set_status.call_args.kwargs["reason"]


def test_rejecting_a_draft_record_explains_instead_of_leaking_an_enum():
    """Measured: the API refuses with "PENDING_APPROVAL, DEPRECATED, DRAFT,
    UPDATING", which does not tell an operator that the record simply has not been
    submitted for review yet."""
    out, body, _, set_status = _review(status="DRAFT", recordId="rec-1",
                                       decision="reject", reason="no")
    assert out["statusCode"] == 409
    assert "has not been submitted" in body["error"]
    assert body["status"] == "DRAFT"
    set_status.assert_not_called()


def test_a_rejected_record_can_still_be_approved():
    """REJECTED -> APPROVED is allowed, verified on a real record. A reviewer who
    changes their mind must not have to ask the author to republish."""
    out, body, approve, _ = _review(status="REJECTED", recordId="rec-1",
                                    decision="approve")
    assert out["statusCode"] == 200
    assert body["status"] == "APPROVED"


# ---------------------------------------------------------------------------
# Deprecate
# ---------------------------------------------------------------------------

def test_deprecating_works_from_draft():
    """The one transition a DRAFT record does have out of the review flow, which is
    what the reject path points an operator at."""
    out, body, _, set_status = _review(status="DRAFT", recordId="rec-1",
                                       decision="deprecate")
    assert out["statusCode"] == 200
    assert body["status"] == "DEPRECATED"


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

def test_an_unknown_decision_is_refused():
    out, body, _, _ = _review(recordId="rec-1", decision="delete")
    assert out["statusCode"] == 400
    assert "decision must be one of" in body["error"]


def test_a_missing_record_id_is_refused():
    out, body, _, _ = _review(decision="approve")
    assert out["statusCode"] == 400
    assert "recordId" in body["error"]


def test_an_unknown_record_is_a_404_not_a_500():
    control = MagicMock()
    control.get_registry_record.side_effect = Exception("ResourceNotFoundException")
    with patch.object(index, "registry_control", control), \
         patch.object(index, "REGISTRY_ID", "reg-1"):
        out = index.review_registry_record(_event(recordId="ghost",
                                                 decision="approve"))
    assert out["statusCode"] == 404


def test_a_registry_failure_reports_the_status_it_was_in():
    """So an operator can tell "the call failed" from "the record moved"."""
    control = MagicMock()
    control.get_registry_record.return_value = {"status": "PENDING_APPROVAL"}
    with patch.object(index, "registry_control", control), \
         patch.object(index, "REGISTRY_ID", "reg-1"), \
         patch.object(index.registry_ns, "approve_record",
                      side_effect=Exception("ConflictException")):
        out = index.review_registry_record(_event(recordId="rec-1",
                                                 decision="approve"))
    body = json.loads(out["body"])
    assert out["statusCode"] == 500
    assert body["status"] == "PENDING_APPROVAL"


def test_no_registry_configured_is_reported_rather_than_crashing():
    with patch.object(index, "REGISTRY_ID", ""):
        out = index.review_registry_record(_event(recordId="r", decision="approve"))
    assert out["statusCode"] == 500
    assert "REGISTRY_ID" in json.loads(out["body"])["error"]


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_the_route_is_dispatched():
    """The handler is useless if POST /registry/records does not reach it."""
    import inspect

    src = inspect.getsource(index._dispatch)
    assert 'resource == "/registry/records" and method == "POST"' in src
    assert "review_registry_record(event)" in src


def test_the_api_gateway_resource_has_a_post_method():
    """A handler with no method on the resource answers 403 from API Gateway, which
    reads as a permissions problem rather than a missing route."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(LAMBDA_DIR)))
    stack = open(os.path.join(root, "cdk", "lib", "smarthome-stack.ts"),
                 encoding="utf-8").read()
    assert 'registryRecordsResource.addMethod("POST"' in stack


# ---------------------------------------------------------------------------
# The feedback loop
# ---------------------------------------------------------------------------

def test_the_erp_returns_the_status_reason_to_the_author():
    """Requiring a reason to reject is pointless if the author never sees it.
    Observed on the deployment: the ERP showed a bare "Rejected" while the
    Registry held "control_device is too broad… (by admin@smarthome.local)".
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(LAMBDA_DIR)))
    erp = open(os.path.join(root, "cdk", "lambda", "skill-erp-api", "index.py"),
               encoding="utf-8").read()
    assert erp.count('"statusReason": r.get("statusReason", "")') >= 2, (
        "the ERP's skill and A2A listings must both surface statusReason, or a "
        "rejected author is told no and never told why")


def test_the_erp_ui_renders_the_reason_beside_a_rejection():
    root = os.path.dirname(os.path.dirname(os.path.dirname(LAMBDA_DIR)))
    for name in ("SkillManager.tsx", "A2AAgentsTab.tsx"):
        body = open(os.path.join(root, "skill-erp", "src", "components", name),
                    encoding="utf-8").read()
        assert "renderStatus(r.status, r.statusReason)" in body, name
        assert "text-status-error" in body, name


def test_approve_record_moves_a_rejected_record_and_does_not_report_success_falsely():
    """`approve_record` handled DRAFT and PENDING_APPROVAL and fell through for
    everything else — RETURNING the unchanged status. So approving a REJECTED
    record answered "REJECTED" as though the call had worked, and the UI showed
    "Record is now REJECTED" after the reviewer pressed Approve. Observed live.
    """
    import agent_registry as ar

    client = MagicMock()
    client.get_registry_record.return_value = {"status": "REJECTED"}
    calls = []

    def _set(c, rid, rec, status, reason=""):
        calls.append(status)
        client.get_registry_record.return_value = {"status": status}

    with patch.object(ar, "set_record_status", side_effect=_set):
        final = ar.approve_record(client, "reg", "rec-1", reason="changed my mind")

    assert calls == ["APPROVED"], "a rejected record was never moved"
    assert final == "APPROVED"
    # And it must NOT have tried to submit-for-approval: REJECTED is already past
    # that step, and the API refuses the transition.
    client.submit_registry_record_for_approval.assert_not_called()
