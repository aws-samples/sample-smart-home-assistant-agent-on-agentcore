import json
import os
from unittest.mock import patch

import pytest
from botocore.stub import Stubber

os.environ.setdefault("AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/text")
os.environ.setdefault("VOICE_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/voice")
os.environ.setdefault("SKILLS_TABLE_NAME", "skills-test")

import optimization  # noqa: E402


def _admin_event(method, resource, path_params=None, body=None, qs=None):
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_params or {},
        "queryStringParameters": qs or {},
        "body": json.dumps(body) if body else None,
        "requestContext": {"authorizer": {"claims": {
            "cognito:groups": "admin",
            "email": "admin@example.com",
        }}},
    }


def test_start_recommendation_happy_path():
    stub = Stubber(optimization._agentcore_data())
    stub.add_response(
        "start_recommendation",
        {
            "recommendationId": "rec-123",
            "recommendationArn": "arn:aws:bedrock-agentcore:us-west-2:1:recommendation/rec-123",
            "name": "test",
            "type": "SYSTEM_PROMPT_RECOMMENDATION",
            "recommendationConfig": {"systemPromptRecommendationConfig": {
                "agentTraces": {"cloudwatchLogs": {
                    "logGroupArns": ["arn:aws:logs:us-west-2:1:log-group:aws/spans:*"],
                    "serviceNames": ["text"],
                    "startTime": "2026-05-07T00:00:00Z",
                    "endTime": "2026-05-14T00:00:00Z",
                }},
                "evaluationConfig": {"evaluators": [{"evaluatorArn": "arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate"}]},
                "systemPrompt": {"text": "current prompt"},
            }},
            "status": "PENDING",
            "createdAt": "2026-05-14T00:00:00Z",
            "updatedAt": "2026-05-14T00:00:00Z",
        },
    )
    with stub, patch.object(optimization, "PREVIEW_UNAVAILABLE", False), \
         patch.object(optimization, "_load_current_prompt", return_value="current prompt"), \
         patch.object(optimization, "_write_rec_row") as write_row:
        ev = _admin_event("POST", "/optimization/recommendations", body={
            "scope": "__global__",
            "agentType": "text",
            "evaluatorArn": "arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate",
            "logGroupArn": "arn:aws:logs:us-west-2:1:log-group:aws/spans:*",
            "startTime": "2026-05-07T00:00:00Z",
            "endTime": "2026-05-14T00:00:00Z",
            "name": "test",
        })
        resp = optimization.start_recommendation(ev)
    assert resp["statusCode"] == 202
    body = json.loads(resp["body"])
    assert body["recommendationId"] == "rec-123"
    assert body["status"] == "PENDING"
    write_row.assert_called_once()


def test_start_recommendation_rejects_invalid_agent_type():
    ev = _admin_event("POST", "/optimization/recommendations", body={
        "scope": "__global__",
        "agentType": "bogus",
        "evaluatorArn": "arn:x",
        "logGroupArn": "arn:y",
        "startTime": "2026-05-07T00:00:00Z",
        "endTime": "2026-05-14T00:00:00Z",
    })
    resp = optimization.start_recommendation(ev)
    assert resp["statusCode"] == 400


def test_start_recommendation_rejects_non_aws_spans_log_group():
    ev = _admin_event("POST", "/optimization/recommendations", body={
        "scope": "__global__",
        "agentType": "text",
        "evaluatorArn": "arn:x",
        "logGroupArn": "arn:aws:logs:us-west-2:1:log-group:other-log-group:*",
        "startTime": "2026-05-07T00:00:00Z",
        "endTime": "2026-05-14T00:00:00Z",
    })
    resp = optimization.start_recommendation(ev)
    assert resp["statusCode"] == 400
    assert "aws/spans" in json.loads(resp["body"])["message"]


def test_list_recommendations_returns_ddb_rows_filtered_by_scope():
    rows = [
        {"userId": "__global__", "skillName": "__opt_rec_rec-1__",
         "recommendationArn": "arn:1", "agentType": "text", "status": "COMPLETED",
         "createdAt": "2026-05-14T00:00:00Z",
         "evaluatorArn": "arn:e"},
        {"userId": "__global__", "skillName": "__opt_rec_rec-2__",
         "recommendationArn": "arn:2", "agentType": "voice", "status": "IN_PROGRESS",
         "createdAt": "2026-05-13T00:00:00Z",
         "evaluatorArn": "arn:e"},
    ]
    with patch.object(optimization, "_query_opt_rows", return_value=rows):
        ev = _admin_event("GET", "/optimization/recommendations", qs={"scope": "__global__"})
        resp = optimization.list_recommendations(ev)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert len(body) == 2
    assert body[0]["recommendationId"] == "rec-1"
    assert body[1]["recommendationId"] == "rec-2"


def test_preview_unavailable_returns_501():
    with patch.object(optimization, "PREVIEW_UNAVAILABLE", True):
        resp = optimization.start_recommendation(_admin_event("POST", "/optimization/recommendations", body={}))
        assert resp["statusCode"] == 501


def test_get_recommendation_completed_returns_full_payload():
    stub = Stubber(optimization._agentcore_data())
    stub.add_response(
        "get_recommendation",
        {
            "recommendationId": "rec-1",
            "recommendationArn": "arn:1",
            "name": "n",
            "type": "SYSTEM_PROMPT_RECOMMENDATION",
            "recommendationConfig": {"systemPromptRecommendationConfig": {
                "agentTraces": {"cloudwatchLogs": {
                    "logGroupArns": ["arn:aws:logs:us-west-2:1:log-group:aws/spans"],
                    "serviceNames": ["text"],
                    "startTime": "2026-05-07T00:00:00Z",
                    "endTime": "2026-05-14T00:00:00Z",
                }},
                "evaluationConfig": {"evaluators": [{"evaluatorArn": "arn:e"}]},
                "systemPrompt": {"text": "current"},
            }},
            "status": "COMPLETED",
            "recommendationResult": {
                "systemPromptRecommendationResult": {
                    "recommendedSystemPrompt": "improved prompt",
                },
            },
            "createdAt": "2026-05-14T00:00:00Z",
            "updatedAt": "2026-05-14T00:01:00Z",
        },
        expected_params={"recommendationId": "rec-1"},
    )
    with stub, patch.object(optimization, "_update_rec_status") as upd, \
         patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        ev = _admin_event("GET", "/optimization/recommendations/{recId}", path_params={"recId": "rec-1"})
        resp = optimization.get_recommendation(ev)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == "COMPLETED"
    assert body["recommendedSystemPrompt"] == "improved prompt"
    upd.assert_called_with("rec-1", "COMPLETED")


def test_delete_recommendation_calls_agentcore_then_ddb():
    stub = Stubber(optimization._agentcore_data())
    stub.add_response("delete_recommendation",
                      {"recommendationId": "rec-1", "status": "DELETED"},
                      expected_params={"recommendationId": "rec-1"})
    with stub, patch.object(optimization, "_delete_rec_row") as drow, \
         patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        ev = _admin_event("DELETE", "/optimization/recommendations/{recId}", path_params={"recId": "rec-1"})
        resp = optimization.delete_recommendation(ev)
    assert resp["statusCode"] == 204
    drow.assert_called_once_with("rec-1")


def test_apply_recommendation_text_writes_prompt_row_only():
    """Target-based redesign: text apply just writes the __prompt_text__
    DDB row. No bundle is created — both runtime endpoints read the prompt
    row at request time."""
    fake_rec = {
        "recommendationId": "rec-1",
        "status": "COMPLETED",
        "recommendationResult": {
            "systemPromptRecommendationResult": {"recommendedSystemPrompt": "new prompt"},
        },
    }
    rec_row = {"agentType": "text", "userId": "__global__"}
    fake_client = optimization._agentcore_data()
    with patch.object(optimization, "_get_rec_row", return_value=rec_row), \
         patch.object(fake_client, "get_recommendation", return_value=fake_rec), \
         patch.object(optimization, "_write_prompt_row") as wprompt, \
         patch.object(optimization, "_create_bundle_version") as cb, \
         patch.object(optimization, "_write_bundle_row") as wb, \
         patch.object(optimization, "_mark_rec_applied") as mra, \
         patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        ev = _admin_event("POST", "/optimization/recommendations/{recId}/apply", path_params={"recId": "rec-1"})
        resp = optimization.apply_recommendation(ev)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["applied"] is True
    assert body["agentType"] == "text"
    wprompt.assert_called_once_with("__global__", "text", "new prompt", "admin@example.com")
    cb.assert_not_called()  # no bundle for text
    wb.assert_not_called()
    mra.assert_called_once()


def test_apply_recommendation_tool_desc_still_creates_bundle():
    """tool_desc apply still uses UpdateGatewayTarget + bundle snapshot for
    rollback (target-based A/B routing doesn't apply to MCP tool descriptions
    today; manual apply-and-observe is the only available workflow)."""
    fake_rec = {
        "recommendationId": "rec-2",
        "status": "COMPLETED",
        "recommendationResult": {
            "toolDescriptionRecommendationResult": {"tools": [
                {"toolName": "control_device", "recommendedToolDescription": "new desc"},
            ]},
        },
    }
    rec_row = {"agentType": "tool_desc", "userId": "__global__"}
    fake_data = optimization._agentcore_data()
    fake_ctrl = optimization._agentcore_control()
    with patch.object(optimization, "_get_rec_row", return_value=rec_row), \
         patch.object(fake_data, "get_recommendation", return_value=fake_rec), \
         patch.object(fake_ctrl, "update_gateway_target", return_value={}) as ugt, \
         patch.object(optimization, "_create_bundle_version", return_value=("arn:b", "v1")) as cb, \
         patch.object(optimization, "_write_bundle_row") as wb, \
         patch.object(optimization, "_mark_rec_applied"), \
         patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        ev = _admin_event("POST", "/optimization/recommendations/{recId}/apply", path_params={"recId": "rec-2"})
        resp = optimization.apply_recommendation(ev)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["appliedBundleArn"] == "arn:b"
    assert body["appliedBundleVersionId"] == "v1"
    ugt.assert_called_once()
    cb.assert_called_once()
    wb.assert_called_once()


def test_list_bundles_filters_to_tool_desc_only():
    """Target-based redesign: bundles are scoped to agentType=tool_desc only.
    Stale text/voice rows from the prior config-bundle design are hidden."""
    rows = [
        {  # text bundle from old config-bundle path → must be hidden
            "userId": "__global__", "skillName": "__opt_bundle_arn:bundle/old_text__",
            "bundleArn": "arn:bundle/old_text", "bundleName": "old_text",
            "latestVersionId": "v1", "agentType": "text",
            "createdAt": "2026-05-14T00:00:00Z",
        },
        {  # tool_desc bundle → must be returned
            "userId": "__global__", "skillName": "__opt_bundle_arn:bundle/tools__",
            "bundleArn": "arn:bundle/tools", "bundleName": "tools",
            "latestVersionId": "v3", "agentType": "tool_desc",
            "createdAt": "2026-05-15T00:00:00Z",
        },
    ]
    with patch.object(optimization, "_query_opt_rows", return_value=rows), \
         patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        ev = _admin_event("GET", "/optimization/bundles", qs={"scope": "__global__"})
        resp = optimization.list_bundles(ev)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert len(body) == 1
    assert body[0]["bundleArn"] == "arn:bundle/tools"
    assert body[0]["agentType"] == "tool_desc"


def test_create_bundle_rejects_text_agent_type():
    """create_bundle is admin-initiated and must reject text/voice — those
    use the runtime-endpoint A/B path, not bundles."""
    with patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        ev = _admin_event("POST", "/optimization/bundles", body={
            "scope": "__global__", "agentType": "text",
            "systemPromptOrTools": "some prompt",
        })
        resp = optimization.create_bundle(ev)
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert "tool_desc" in body["message"]


def test_get_bundle_versions_calls_control_api():
    stub = Stubber(optimization._agentcore_control())
    stub.add_response(
        "list_configuration_bundle_versions",
        {"versions": [
            {"bundleArn": "arn:bundle/foo", "bundleId": "b1", "versionId": "v3",
             "versionCreatedAt": "2026-05-14T00:00:00Z"},
            {"bundleArn": "arn:bundle/foo", "bundleId": "b1", "versionId": "v2",
             "versionCreatedAt": "2026-05-13T00:00:00Z"},
        ]},
        expected_params={"bundleId": "arn:bundle/foo"},
    )
    with stub, patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        ev = _admin_event("GET", "/optimization/bundles/{bundleArn}", path_params={"bundleArn": "arn:bundle/foo"})
        resp = optimization.get_bundle_versions(ev)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert len(body["versions"]) == 2
    assert body["versions"][0]["versionId"] == "v3"


def test_delete_bundle_calls_control_api_and_ddb():
    stub = Stubber(optimization._agentcore_control())
    stub.add_response(
        "delete_configuration_bundle",
        {"bundleId": "b1", "status": "DELETING"},
        expected_params={"bundleId": "arn:bundle/foo"},
    )
    with stub, patch.object(optimization, "_delete_bundle_row") as drow, \
         patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        ev = _admin_event("DELETE", "/optimization/bundles/{bundleArn}", path_params={"bundleArn": "arn:bundle/foo"})
        resp = optimization.delete_bundle(ev)
    assert resp["statusCode"] == 204
    drow.assert_called_once_with("arn:bundle/foo")


def test_start_ab_test_rejects_non_global_scope():
    ev = _admin_event("POST", "/optimization/ab-tests", body={
        "agentType": "text",
        "variantWeights": {"control": 50, "treatment": 50},
        "durationDays": 7,
        "scope": "user@example.com",
    })
    with patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        resp = optimization.start_ab_test(ev)
    assert resp["statusCode"] == 400


def test_start_ab_test_rejects_when_toggle_off():
    """When the global A/B routing toggle is OFF the API refuses to create
    a new A/B test — there's no gateway routing path to hit."""
    with patch.object(optimization, "_read_ab_toggle", return_value={"enabled": False}), \
         patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        ev = _admin_event("POST", "/optimization/ab-tests", body={
            "agentType": "text",
            "variantWeights": {"control": 50, "treatment": 50},
            "durationDays": 7,
        })
        resp = optimization.start_ab_test(ev)
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert body["error"] == "ToggleDisabled"


def test_start_ab_test_rejects_voice_and_tool_desc_agent_types():
    with patch.object(optimization, "_read_ab_toggle", return_value={"enabled": True}), \
         patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        for at in ("voice", "tool_desc"):
            ev = _admin_event("POST", "/optimization/ab-tests", body={
                "agentType": at,
                "variantWeights": {"control": 50, "treatment": 50},
                "durationDays": 7,
            })
            resp = optimization.start_ab_test(ev)
            assert resp["statusCode"] == 400, at
            body = json.loads(resp["body"])
            assert body["error"] == "UnsupportedAgentType"


def test_start_ab_test_rejects_when_running_test_exists():
    existing = [{
        "userId": "__global__", "skillName": "__opt_abtest_old__",
        "agentType": "text", "executionStatus": "RUNNING",
    }]
    with patch.object(optimization, "_query_opt_rows", return_value=existing), \
         patch.object(optimization, "_read_ab_toggle", return_value={"enabled": True}), \
         patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        ev = _admin_event("POST", "/optimization/ab-tests", body={
            "agentType": "text",
            "variantWeights": {"control": 50, "treatment": 50},
            "durationDays": 7,
        })
        resp = optimization.start_ab_test(ev)
    assert resp["statusCode"] == 409


def test_start_ab_test_target_based_payload():
    """create_ab_test must be invoked with target-based shape:
    gatewayArn=optimization-gateway, gatewayFilter.targetPaths,
    perVariantOnlineEvaluationConfig (two entries), variants reference
    target.name (not configurationBundle)."""
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return {
            "abTestId": "abt-1", "abTestArn": "arn:abt-1", "name": "rollout",
            "status": "CREATING", "executionStatus": "NOT_STARTED",
            "createdAt": "2026-05-14T00:00:00Z",
        }

    fake_data = optimization._agentcore_data()
    env = {
        "OPTIMIZATION_GATEWAY_ARN": "arn:gw-opt",
        "AB_TEST_ROLE_ARN": "arn:role",
        "CONTROL_ONLINE_EVAL_ARN": "arn:eval-c",
        "TREATMENT_ONLINE_EVAL_ARN": "arn:eval-t",
    }
    with patch.object(fake_data, "create_ab_test", side_effect=fake_create), \
         patch.object(optimization, "_query_opt_rows", return_value=[]), \
         patch.object(optimization, "_read_ab_toggle", return_value={"enabled": True}), \
         patch.object(optimization, "_write_abtest_row") as wrow, \
         patch.object(optimization, "PREVIEW_UNAVAILABLE", False), \
         patch.dict(os.environ, env):
        ev = _admin_event("POST", "/optimization/ab-tests", body={
            "agentType": "text",
            "controlEndpoint": "control",
            "treatmentEndpoint": "treatment",
            "variantWeights": {"control": 80, "treatment": 20},
            "durationDays": 1,
            "name": "rollout",
        })
        resp = optimization.start_ab_test(ev)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["testId"] == "abt-1"
    assert body["routingMode"] == "target-based"
    assert body["controlEndpoint"] == "control"
    # Verify CreateABTest payload
    assert captured["gatewayArn"] == "arn:gw-opt"
    assert captured["gatewayFilter"] == {"targetPaths": ["/smarthome-control/*"]}
    eval_cfg = captured["evaluationConfig"]
    assert "perVariantOnlineEvaluationConfig" in eval_cfg
    pv = {x["name"]: x["onlineEvaluationConfigArn"] for x in eval_cfg["perVariantOnlineEvaluationConfig"]}
    assert pv == {"C": "arn:eval-c", "T1": "arn:eval-t"}
    variants = captured["variants"]
    assert {v["name"] for v in variants} == {"C", "T1"}
    assert variants[0]["variantConfiguration"] == {"target": {"name": "smarthome-control"}}
    assert variants[1]["variantConfiguration"] == {"target": {"name": "smarthome-treatment"}}
    assert variants[0]["weight"] + variants[1]["weight"] == 100
    wrow.assert_called_once()


# --- AB toggle tests ---------------------------------------------------------

def test_get_ab_toggle_default_returns_false():
    """When the DDB row is missing the toggle defaults to disabled."""
    with patch.object(optimization, "_ddb_table") as t, \
         patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        t.return_value.get_item.return_value = {}  # no Item key
        ev = _admin_event("GET", "/optimization/ab-toggle")
        resp = optimization.get_ab_toggle(ev)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["enabled"] is False


def test_put_ab_toggle_on_writes_ddb_no_agentcore_call():
    with patch.object(optimization, "_write_ab_toggle") as w, \
         patch.object(optimization, "_query_opt_rows", return_value=[]) as q, \
         patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        ev = _admin_event("PUT", "/optimization/ab-toggle", body={"enabled": True})
        resp = optimization.put_ab_toggle(ev)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["enabled"] is True
    assert "stoppedTestId" not in body
    w.assert_called_once_with(True, "admin@example.com")
    # We don't query for active tests when enabling — only on disable.
    q.assert_not_called()


def test_put_ab_toggle_off_with_running_test_stops_it():
    """Toggle OFF with an active A/B test → calls UpdateABTest(STOPPED) +
    finalizes the DDB mirror row + returns stoppedTestId."""
    rows = [{
        "userId": "__global__", "skillName": "__opt_abtest_abt-running__",
        "agentType": "text", "executionStatus": "RUNNING",
    }]
    fake_data = optimization._agentcore_data()
    with patch.object(optimization, "_query_opt_rows", return_value=rows), \
         patch.object(fake_data, "update_ab_test", return_value={"executionStatus": "STOPPED"}) as ua, \
         patch.object(optimization, "_finalize_abtest_row") as fin, \
         patch.object(optimization, "_write_ab_toggle") as w, \
         patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        ev = _admin_event("PUT", "/optimization/ab-toggle", body={"enabled": False})
        resp = optimization.put_ab_toggle(ev)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["enabled"] is False
    assert body["stoppedTestId"] == "abt-running"
    ua.assert_called_once_with(abTestId="abt-running", executionStatus="STOPPED")
    fin.assert_called_once()
    w.assert_called_once_with(False, "admin@example.com")


def test_put_ab_toggle_off_without_active_test_skips_stop():
    rows = [{  # already stopped
        "userId": "__global__", "skillName": "__opt_abtest_old__",
        "agentType": "text", "executionStatus": "STOPPED",
    }]
    fake_data = optimization._agentcore_data()
    with patch.object(optimization, "_query_opt_rows", return_value=rows), \
         patch.object(fake_data, "update_ab_test") as ua, \
         patch.object(optimization, "_write_ab_toggle"), \
         patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        ev = _admin_event("PUT", "/optimization/ab-toggle", body={"enabled": False})
        resp = optimization.put_ab_toggle(ev)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert "stoppedTestId" not in body
    ua.assert_not_called()


def test_get_ab_test_includes_dashboard_url_and_writes_status():
    # Real GetABTest response. Required fields: abTestId, abTestArn, name, status,
    # executionStatus, gatewayArn, variants, evaluationConfig, createdAt, updatedAt.
    stub = Stubber(optimization._agentcore_data())
    stub.add_response(
        "get_ab_test",
        {
            "abTestId": "abt-1",
            "abTestArn": "arn:abt-1",
            "name": "rollout",
            "status": "ACTIVE",
            "executionStatus": "RUNNING",
            "gatewayArn": "arn:gw",
            "variants": [
                {"name": "control", "weight": 50,
                 "variantConfiguration": {"configurationBundle": {"bundleArn": "arn:b", "bundleVersion": "v1"}}},
                {"name": "treatment", "weight": 50,
                 "variantConfiguration": {"configurationBundle": {"bundleArn": "arn:b", "bundleVersion": "v2"}}},
            ],
            "evaluationConfig": {"onlineEvaluationConfigArn": "arn:eval"},
            "createdAt": "2026-05-14T00:00:00Z",
            "updatedAt": "2026-05-14T00:01:00Z",
            "results": {
                "evaluatorMetrics": [
                    {
                        "evaluatorArn": "arn:e1",
                        "controlStats": {"variantName": "control", "sampleSize": 100, "mean": 0.7},
                        "variantResults": [
                            {"variantName": "treatment", "sampleSize": 102, "mean": 0.85,
                             "pValue": 0.04, "isSignificant": True},
                        ],
                    }
                ]
            },
        },
        expected_params={"abTestId": "abt-1"},
    )
    with stub, patch.object(optimization, "_update_abtest_status") as upd, \
         patch.object(optimization, "_query_opt_rows", return_value=[
             {"userId": "__global__", "skillName": "__opt_abtest_abt-1__",
              "routingMode": "target-based",
              "controlEndpoint": "control", "treatmentEndpoint": "treatment"},
         ]), \
         patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        ev = _admin_event("GET", "/optimization/ab-tests/{testId}", path_params={"testId": "abt-1"})
        resp = optimization.get_ab_test(ev)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert body["pValue"] == 0.04
    assert body["winner"] == "treatment"
    assert "cloudwatchDashboardUrl" in body
    assert "abt-1" in body["cloudwatchDashboardUrl"]
    assert body["routingMode"] == "target-based"
    assert body["controlEndpoint"] == "control"
    upd.assert_called()


def test_stop_ab_test_writes_winner_to_ddb():
    # update_ab_test response required fields: abTestId, abTestArn, status, executionStatus, updatedAt
    stub = Stubber(optimization._agentcore_data())
    stub.add_response(
        "update_ab_test",
        {
            "abTestId": "abt-1",
            "abTestArn": "arn:abt-1",
            "status": "ACTIVE",
            "executionStatus": "STOPPED",
            "updatedAt": "2026-05-14T01:00:00Z",
        },
        expected_params={"abTestId": "abt-1", "executionStatus": "STOPPED"},
    )
    with stub, patch.object(optimization, "_finalize_abtest_row") as fin, \
         patch.object(optimization, "PREVIEW_UNAVAILABLE", False):
        ev = _admin_event("POST", "/optimization/ab-tests/{testId}/stop", path_params={"testId": "abt-1"})
        resp = optimization.stop_ab_test(ev)
    assert resp["statusCode"] == 200
    fin.assert_called()  # we don't enforce arg shape; handler decides winner from get_ab_test or stops without
