"""The scan routes, and the Bedrock call shape underneath them.

Three classes of thing are pinned here.

**The route contract.** Scanning rides on `/registry/records?action=skill-scan` rather
than a path of its own, because this Lambda's auto-generated API Gateway resource policy
is near the 20KB cap — a new path is not free. GET reads reports back, POST writes one.

**The Bedrock call is validated against the REAL botocore shape.** A MagicMock accepts
any keyword shape, so a route test that mocks boto3 proves nothing about whether the
request would leave the Lambda. This repo has already shipped a guaranteed 500 that way
(`update_registry_record` with an unwrapped `description`), so the Converse kwargs are
checked against the service model the same way
`skill-erp-api/tests/test_registry_update_payload.py` checks the Registry ones.

**Failures degrade in the honest direction.** A model that is throttled, a reply that is
not JSON, a report row that cannot be written — each of those must leave the reviewer
with less confidence, never with a clean-looking report.
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
import skill_scan  # noqa: E402

SKILL_MD = (
    "---\n"
    'name: demo-skill\n'
    'description: "A demo skill."\n'
    "allowed_tools: [query_device_state]\n"
    'x-submitted-by: "author@example.com"\n'
    "---\n"
    "\n"
    "# Demo skill\n\nRead the room temperature and report it.\n"
)

RISKY_MD = SKILL_MD + "\nUse the key AKIAIOSFODNN7EXAMPLE for the vendor bridge.\n"


def _record(skill_md=SKILL_MD, version="0.1.0"):
    """A GetRegistryRecord response carrying a GA-shaped SKILL descriptor."""
    return {
        "recordId": "rec-1",
        "name": "demo-skill",
        "displayName": "demo-skill",
        "description": "A demo skill.",
        "status": "PENDING_APPROVAL",
        "recordVersion": version,
        "descriptors": {
            "agentSkillsDefinition": {
                "data": json.dumps({"_meta": {"license": "Apache-2.0"}}),
                "additionalData": {"skillMd": {"data": skill_md}},
            }
        },
    }


def _post(body, record=None, ddb=None, judge=None):
    control = MagicMock()
    control.get_registry_record.return_value = record if record is not None else _record()
    ddb = ddb if ddb is not None else MagicMock(**{"query.return_value": {"Items": []}})
    event = {
        "httpMethod": "POST",
        "resource": "/registry/records",
        "queryStringParameters": {"action": "skill-scan"},
        "body": json.dumps(body),
        "requestContext": {"authorizer": {"claims": {
            "email": "reviewer@smarthome.local", "cognito:groups": "admin"}}},
    }
    with patch.object(index, "registry_control", control), \
         patch.object(index, "table", ddb), \
         patch.object(index, "REGISTRY_ID", "reg-1"), \
         patch.object(index, "_semantic_judge",
                      judge if judge is not None else (lambda _p: [])):
        out = index.handler(event, None)
    return out, json.loads(out["body"]), ddb


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def test_the_post_route_reaches_the_scanner():
    out, body, _ = _post({"recordId": "rec-1"})
    assert out["statusCode"] == 200
    assert body["recordId"] == "rec-1"
    assert body["verdict"] == "PASS"
    assert body["scannerVersion"] == skill_scan.SCANNER_VERSION


def test_the_get_route_returns_the_latest_report_per_record():
    ddb = MagicMock()
    ddb.query.return_value = {"Items": [
        {"recordId": "rec-1", "verdict": "WARN", "score": 8, "scannedAt": "2026-08-24T01:00:00+00:00"},
        {"recordId": "rec-1", "verdict": "FAIL", "score": 40, "scannedAt": "2026-08-24T02:00:00+00:00"},
        {"recordId": "rec-2", "verdict": "PASS", "score": 0, "scannedAt": "2026-08-24T00:30:00+00:00"},
    ]}
    event = {"httpMethod": "GET", "resource": "/registry/records",
             "queryStringParameters": {"action": "skill-scan"},
             "requestContext": {"authorizer": {"claims": {
                 "cognito:groups": "admin"}}}}
    with patch.object(index, "table", ddb):
        out = index.handler(event, None)
    reports = json.loads(out["body"])["reports"]
    assert out["statusCode"] == 200
    # The newest row wins, not whichever came back last.
    assert reports["rec-1"]["verdict"] == "FAIL"
    assert reports["rec-2"]["verdict"] == "PASS"


def test_the_scan_action_does_not_fall_through_to_the_reviewer():
    """Without its own POST branch this landed on `review_registry_record`, which would
    answer "decision must be one of..." for a button that reviews nothing."""
    out, body, _ = _post({"recordId": "rec-1"})
    assert "decision must be one of" not in json.dumps(body)


def test_a_missing_record_id_is_a_400():
    out, body, _ = _post({})
    assert out["statusCode"] == 400
    assert "recordId is required" in body["error"]


# ---------------------------------------------------------------------------
# What is scanned
# ---------------------------------------------------------------------------

def test_the_descriptor_is_read_for_content_tools_and_license():
    """Everything the scanner needs is already in the record — no extra reads."""
    payload = index._scan_payload_from_record(_record())
    assert payload["allowedTools"] == ["query_device_state"]
    assert payload["license"] == "Apache-2.0"
    assert payload["metadata"]["submitted-by"] == "author@example.com"
    assert "Read the room temperature" in payload["skillMd"]


def test_findings_are_returned_and_persisted():
    out, body, ddb = _post({"recordId": "rec-1"}, record=_record(RISKY_MD))
    assert body["verdict"] == "FAIL"
    assert "SS08" in [f["id"] for f in body["findings"]]
    item = ddb.put_item.call_args.kwargs["Item"]
    assert item["userId"] == index.SCAN_OWNER_KEY
    assert item["skillName"] == "scan#rec-1#0.1.0"
    assert item["verdict"] == "FAIL"
    assert item["scannedBy"] == "reviewer@smarthome.local"


def test_reports_are_kept_per_version_rather_than_overwritten():
    """"This version was scanned, the next one was not" is the AST07 question. An
    overwriting store cannot answer it."""
    _, _, ddb_a = _post({"recordId": "rec-1"}, record=_record(version="0.1.0"))
    _, _, ddb_b = _post({"recordId": "rec-1"}, record=_record(version="0.2.0"))
    assert ddb_a.put_item.call_args.kwargs["Item"]["skillName"] == "scan#rec-1#0.1.0"
    assert ddb_b.put_item.call_args.kwargs["Item"]["skillName"] == "scan#rec-1#0.2.0"


def test_an_agent_record_is_refused_rather_than_scanned_empty():
    """An AGENT record has no SKILL.md. Returning an empty report would read as
    "scanned, nothing found" for a record this scanner cannot speak about."""
    agent_record = {
        "recordId": "rec-a", "status": "PENDING_APPROVAL", "recordVersion": "0.1.0",
        "descriptors": {"a2aAgentCard": {"data": json.dumps(
            {"name": "sha2alight", "url": "https://example.invalid", "skills": []})}},
    }
    out, body, _ = _post({"recordId": "rec-a"}, record=agent_record)
    assert out["statusCode"] == 400
    assert "a2a-conformance" in body["error"]


def test_a_record_with_no_skill_md_is_reported_not_passed():
    empty = {"recordId": "rec-e", "status": "DRAFT", "recordVersion": "0.1.0",
             "descriptors": {"agentSkillsDefinition": {"data": "{}"}}}
    out, body, _ = _post({"recordId": "rec-e"}, record=empty)
    assert out["statusCode"] == 422
    assert "nothing to scan" in body["error"]


def test_a_previous_report_is_passed_in_so_drift_can_be_seen():
    prior = {"recordId": "rec-1", "contentHash": "0" * 64,
             "scannedAt": "2026-08-01T00:00:00+00:00"}
    ddb = MagicMock()
    ddb.query.return_value = {"Items": [prior]}
    _, body, _ = _post({"recordId": "rec-1"}, ddb=ddb)
    assert "SS13" in [f["id"] for f in body["findings"]]


def test_a_failed_write_still_returns_the_report():
    ddb = MagicMock()
    ddb.query.return_value = {"Items": []}
    ddb.put_item.side_effect = RuntimeError("throttled")
    out, body, _ = _post({"recordId": "rec-1"}, ddb=ddb)
    assert out["statusCode"] == 200
    assert body["persisted"] is False


# ---------------------------------------------------------------------------
# The semantic tier
# ---------------------------------------------------------------------------

def test_the_semantic_tier_can_be_skipped_per_request():
    def judge(_prompt):
        raise AssertionError("the judge should not be called when semantic=false")
    out, body, _ = _post({"recordId": "rec-1", "semantic": False}, judge=judge)
    assert out["statusCode"] == 200
    assert body["llmTier"] == "skipped"


def test_a_throttled_model_is_reported_and_the_static_verdict_stands():
    def judge(_prompt):
        raise RuntimeError("ThrottlingException")
    out, body, _ = _post({"recordId": "rec-1"}, record=_record(RISKY_MD), judge=judge)
    assert body["llmTier"].startswith("unavailable")
    assert body["verdict"] == "FAIL"  # SS08 is static; the model was never needed


@pytest.mark.parametrize("reply", ["not json at all", "{}", '{"findings": "nope"}'])
def test_an_unreadable_reply_is_not_treated_as_a_clean_result(reply):
    """"The model said something I could not read" and "the model found nothing" must
    not present the same way."""
    with pytest.raises(ValueError):
        index._parse_semantic_findings(reply)


def test_a_well_formed_empty_reply_is_a_clean_semantic_result():
    assert index._parse_semantic_findings('{"findings": []}') == []


def test_the_judge_reply_is_parsed_out_of_surrounding_prose():
    reply = 'Here is my analysis:\n{"findings": [{"id": "SS20", "severity": "high"}]}\nDone.'
    assert index._parse_semantic_findings(reply)[0]["id"] == "SS20"


def test_the_converse_request_matches_the_real_botocore_shape():
    """Validated against the service model, not against a MagicMock.

    A MagicMock accepts `bedrock_runtime.converse(anything=...)` and reports success, so
    a mocked route test cannot tell a valid request from one that will raise
    ParamValidationError in production. This builds the same kwargs the handler builds
    and runs botocore's own validator over them.
    """
    import botocore.session
    from botocore.validate import validate_parameters

    captured = {}

    def fake_converse(**kwargs):
        captured.update(kwargs)
        return {"output": {"message": {"content": [{"text": '{"findings": []}'}]}}}

    runtime = MagicMock()
    runtime.converse.side_effect = fake_converse
    with patch.object(index, "bedrock_runtime", runtime):
        index._semantic_judge("a prompt")

    model = botocore.session.get_session().get_service_model("bedrock-runtime")
    shape = model.operation_model("Converse").input_shape
    validate_parameters(captured, shape)  # raises ParamValidationError if wrong

    assert captured["inferenceConfig"]["temperature"] == 0, (
        "the judge must be deterministic, or two runs of the same skill differ for "
        "reasons that have nothing to do with the skill")


def test_the_prompt_sent_to_the_model_frames_the_skill_as_untrusted_data():
    """The one defence against the skill talking to the judge instead of being read."""
    sent = {}

    def judge(prompt):
        sent["prompt"] = prompt
        return []
    _post({"recordId": "rec-1"}, judge=judge)
    assert "UNTRUSTED DATA" in sent["prompt"]
    assert "Read the room temperature" in sent["prompt"]


# ---------------------------------------------------------------------------
# The review path records what the scan said
# ---------------------------------------------------------------------------

def _review(scan_row=None, record=None, decision="approve"):
    control = MagicMock()
    control.get_registry_record.return_value = record if record is not None else {
        "status": "PENDING_APPROVAL"}
    ddb = MagicMock()
    ddb.query.return_value = {"Items": [scan_row] if scan_row else []}
    event = {
        "httpMethod": "POST", "resource": "/registry/records",
        "body": json.dumps({"recordId": "rec-1", "decision": decision,
                            "reason": "looks fine"}),
        "requestContext": {"authorizer": {"claims": {
            "email": "reviewer@smarthome.local"}}},
    }
    with patch.object(index, "registry_control", control), \
         patch.object(index, "table", ddb), \
         patch.object(index, "REGISTRY_ID", "reg-1"), \
         patch.object(index.registry_ns, "approve_record",
                      return_value="APPROVED") as approve, \
         patch.object(index.registry_ns, "set_record_status"):
        out = index.review_registry_record(event)
    return json.loads(out["body"]), approve


def test_approving_records_the_scan_verdict_in_the_status_reason():
    body, approve = _review(scan_row={
        "recordId": "rec-1", "verdict": "WARN", "score": 8, "riskTier": "L2",
        "llmTier": "ok", "scannedAt": "2026-08-24T02:00:00+00:00",
        "scannedBy": "reviewer@smarthome.local"})
    reason = approve.call_args.kwargs["reason"]
    assert "scan WARN 8" in reason
    assert "looks fine" in reason
    assert "reviewer@smarthome.local" in reason
    assert body["scan"]["verdict"] == "WARN"


def test_approving_an_unscanned_skill_is_allowed_and_says_so():
    """The scan is evidence, not a gate — a reviewer may be right to overrule it, and
    may be right to approve without one. What must not happen is that the record cannot
    later say which of those it was."""
    body, approve = _review(scan_row=None)
    assert "scan not run" in approve.call_args.kwargs["reason"]
    assert body["scan"]["verdict"] == "NOT_SCANNED"
    assert body["status"] == "APPROVED"


def test_a_failing_scan_does_not_block_the_approval():
    body, approve = _review(scan_row={
        "recordId": "rec-1", "verdict": "FAIL", "score": 60, "riskTier": "L3"})
    assert body["status"] == "APPROVED"
    assert "scan FAIL 60" in approve.call_args.kwargs["reason"]


def test_rejecting_does_not_carry_a_scan_note():
    """The note exists to record what an APPROVAL was made against. A rejection's
    reason is the author's only feedback and should not be diluted."""
    body, _ = _review(decision="reject")
    assert body["scan"] is None


def test_an_agent_approval_is_untouched_by_the_scan_note():
    """AGENT records go through the conformance gate, which is a different mechanism."""
    agent_record = {
        "status": "PENDING_APPROVAL",
        "descriptors": {"a2aAgentCard": {"data": json.dumps(
            {"name": "sha2alight", "url": "https://example.invalid", "skills": []})}},
    }
    with patch.object(index, "_conformance_gate", return_value=None):
        body, approve = _review(record=agent_record)
    assert body["scan"] is None
    assert "scan" not in approve.call_args.kwargs["reason"]
