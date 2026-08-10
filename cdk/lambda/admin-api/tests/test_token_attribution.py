"""Per-session token totals must belong to this project, and to one agent.

Two defects in the same query, both silent:

  - `aws/spans` is an account-wide log group — about forty unrelated runtimes
    share it in this account. The query filtered on `scope.name` and on the
    presence of a token attribute, but not on `service.name`, so it summed every
    project's tokens into our sessions. It happened to report correctly only
    because no other project logged Strands token spans in the window; that is
    luck, not a filter.
  - A delegated turn spends tokens in the SUB-AGENT's runtime under the same
    session id. Summing without grouping folds the specialists' spend into the
    orchestrator's row, which makes "what did this agent cost" unanswerable from
    the page that exists to answer it.

Measured before the fix: 16,741,648 tokens under the orchestrator and 6,952
across three sub-agents, all reported as one number.
"""

import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDA_DIR = os.path.dirname(HERE)
sys.path.insert(0, LAMBDA_DIR)

import index  # noqa: E402


def _rows(*records):
    """Shape Logs Insights returns: a list of {field, value} lists."""
    return {"status": "Complete",
            "results": [[{"field": k, "value": v} for k, v in rec.items()]
                        for rec in records]}


def _fetch(rows):
    """Run _fetch_token_totals_7d against a canned Logs Insights result."""
    logs = MagicMock()
    logs.start_query.return_value = {"queryId": "q1"}
    logs.get_query_results.return_value = rows
    with patch.object(index, "logs_client", logs):
        return index._fetch_token_totals_7d(), logs


# ---------------------------------------------------------------------------
# The service filter
# ---------------------------------------------------------------------------

def test_the_query_restricts_spans_to_this_projects_runtimes():
    """Without this the page reports other tenants' token spend as ours."""
    _, logs = _fetch(_rows())
    query = logs.start_query.call_args.kwargs["queryString"]
    assert "resource.attributes.service.name in [" in query, (
        "the token query has no service filter; aws/spans is account-wide")


def test_the_filter_comes_from_the_fleet_rather_than_a_hardcoded_list():
    """A newly deployed sub-agent has to be covered by setting
    DASHBOARD_EXTRA_RUNTIME_ARNS, not by editing this query."""
    import inspect

    src = inspect.getsource(index._fetch_token_totals_7d)
    assert "dashboard._spans_service_filter()" in src


def test_the_query_groups_by_service_so_agents_can_be_told_apart():
    _, logs = _fetch(_rows())
    query = logs.start_query.call_args.kwargs["queryString"]
    assert "resource.attributes.service.name as serviceName" in query
    assert "by attributes.session.id as sessionId" in query


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

def test_a_session_total_sums_every_runtime_it_touched():
    """The total must still be the whole turn — a user asking "what did this
    session cost" means all of it, delegation included."""
    totals, _ = _fetch(_rows(
        {"sessionId": "s1", "serviceName": "smarthome_smarthome.DEFAULT",
         "totalTokens": "1000"},
        {"sessionId": "s1", "serviceName": "sha2aqa_sha2aqa.DEFAULT",
         "totalTokens": "250"},
    ))
    assert totals["s1"]["total"] == 1250


def test_the_split_names_each_agent_separately():
    """This is what the single sum destroyed: the specialists' spend was
    indistinguishable from the orchestrator's."""
    totals, _ = _fetch(_rows(
        {"sessionId": "s1", "serviceName": "smarthome_smarthome.DEFAULT",
         "totalTokens": "1000"},
        {"sessionId": "s1", "serviceName": "sha2aqa_sha2aqa.DEFAULT",
         "totalTokens": "250"},
        {"sessionId": "s1", "serviceName": "sha2alight_sha2alight.DEFAULT",
         "totalTokens": "40"},
    ))
    assert totals["s1"]["byAgent"] == {
        "smarthome": 1000, "sha2aqa": 250, "sha2alight": 40}


def test_two_sessions_do_not_mix():
    totals, _ = _fetch(_rows(
        {"sessionId": "s1", "serviceName": "smarthome_smarthome.DEFAULT",
         "totalTokens": "10"},
        {"sessionId": "s2", "serviceName": "smarthome_smarthome.DEFAULT",
         "totalTokens": "20"},
    ))
    assert totals["s1"]["total"] == 10
    assert totals["s2"]["total"] == 20


def test_repeated_rows_for_one_agent_accumulate():
    """Logs Insights returns one row per group; a re-query or a paged result must
    add rather than overwrite."""
    totals, _ = _fetch(_rows(
        {"sessionId": "s1", "serviceName": "sha2aqa_sha2aqa.DEFAULT",
         "totalTokens": "100"},
        {"sessionId": "s1", "serviceName": "sha2aqa_sha2aqa.DEFAULT",
         "totalTokens": "50"},
    ))
    assert totals["s1"]["byAgent"]["sha2aqa"] == 150


def test_a_row_with_no_session_is_skipped():
    totals, _ = _fetch(_rows(
        {"serviceName": "smarthome_smarthome.DEFAULT", "totalTokens": "999"}))
    assert totals == {}


def test_an_unparseable_token_count_is_skipped_not_zeroed():
    """A zero would read as "this session was free"."""
    totals, _ = _fetch(_rows(
        {"sessionId": "s1", "serviceName": "smarthome_smarthome.DEFAULT",
         "totalTokens": "n/a"}))
    assert "s1" not in totals


def test_a_row_with_no_service_still_counts_toward_the_total():
    """An unattributable span is still real spend; dropping it would understate
    the session, which is worse than an unattributed figure."""
    totals, _ = _fetch(_rows(
        {"sessionId": "s1", "totalTokens": "77"}))
    assert totals["s1"]["total"] == 77
    assert totals["s1"]["byAgent"] == {}


# ---------------------------------------------------------------------------
# The agent id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("service,expected", [
    ("smarthome_smarthome.DEFAULT", "smarthome"),
    ("sha2aqa_sha2aqa.DEFAULT", "sha2aqa"),
    ("smarthomevoice_smarthomevoice.DEFAULT", "smarthomevoice"),
    # The A/B variant: both halves differ, so the full name is the id — otherwise
    # it collapses onto the orchestrator, exactly as it did on the fleet page.
    ("smarthome_bundles.DEFAULT", "smarthome_bundles"),
    ("", ""),
])
def test_the_agent_id_matches_the_one_the_fleet_page_uses(service, expected):
    """A token figure has to be joinable to a fleet row, so both sides must derive
    the id the same way."""
    assert index._agent_id_from_service_name(service) == expected


def test_the_derivation_agrees_with_the_fleet_read_model():
    """Two copies of this rule would drift; this pins them together."""
    import agents as fleet

    arn = "arn:aws:bedrock-agentcore:us-west-2:1:runtime/sha2alight_sha2alight-KAoL"
    from_fleet = fleet.build_fleet([arn], [], {})[0]["agentId"]
    from_spans = index._agent_id_from_service_name("sha2alight_sha2alight.DEFAULT")
    assert from_fleet == from_spans == "sha2alight"


# ---------------------------------------------------------------------------
# The A/B eval dimension is deliberately NOT parameterised
# ---------------------------------------------------------------------------

def test_the_ab_eval_metric_stays_pinned_to_the_orchestrator():
    """Not an oversight: the online-eval configs are attached to the text runtime
    and to no other, verified against Bedrock-AgentCore/Evaluations where only
    `smarthome_smarthome.DEFAULT` carries evaluation metrics. Parameterising this
    would query dimensions that do not exist and render an empty chart."""
    import inspect

    import dashboard

    src = inspect.getsource(dashboard)
    assert "ORCHESTRATOR_SERVICE_NAME" in src
    # Named for what it is. The old bare `SERVICE_NAME` read like a project-wide
    # default that had simply not been parameterised, which is why it was worth
    # renaming rather than only commenting: nothing outside this module used it.
    assert not re.search(r"^SERVICE_NAME\s*=", src, re.M), (
        "a bare SERVICE_NAME is back; per-agent callers should use "
        "service_name_for() and orchestrator-only ones the explicit name")


def test_service_name_for_answers_for_any_deployed_agent(monkeypatch):
    import dashboard

    monkeypatch.setattr(dashboard, "_all_runtime_arns", lambda: [
        "arn:aws:bedrock-agentcore:us-west-2:1:runtime/smarthome_smarthome-aa",
        "arn:aws:bedrock-agentcore:us-west-2:1:runtime/sha2aqa_sha2aqa-bb",
    ])
    assert dashboard.service_name_for("sha2aqa") == "sha2aqa_sha2aqa.DEFAULT"
    assert dashboard.service_name_for("smarthome") == "smarthome_smarthome.DEFAULT"


def test_an_unknown_agent_gets_no_service_name_rather_than_the_orchestrators():
    """Falling back would attribute one agent's metrics to another, which is worse
    than showing none."""
    import dashboard

    assert dashboard.service_name_for("no-such-agent") == ""
    assert dashboard.service_name_for("") == ""


def test_a_sub_agent_session_id_is_kept_separate_rather_than_guessed_at():
    """Measured: a sub-agent's runtime stamps its own session id (a bare UUID)
    instead of inheriting the orchestrator's `user-session-*`, because AgentCore
    assigns runtimeSessionId per runtime and the A2A hop does not propagate it.

    So a delegated turn's tokens land under a session id the runtime-sessions
    table has no row for. That is a real limit on per-TURN attribution, and the
    right behaviour is to keep the rows honest rather than to fold an unknown
    session into the nearest orchestrator one — which would invent a join.
    """
    totals, _ = _fetch(_rows(
        {"sessionId": "user-session-abc", "serviceName": "smarthome_smarthome.DEFAULT",
         "totalTokens": "1000"},
        {"sessionId": "ce6477ee-7021-45f5-b99d-317a5b", "totalTokens": "1256",
         "serviceName": "sha2aenergy_sha2aenergy.DEFAULT"},
    ))
    assert totals["user-session-abc"]["byAgent"] == {"smarthome": 1000}
    assert totals["ce6477ee-7021-45f5-b99d-317a5b"]["byAgent"] == {
        "sha2aenergy": 1256}, (
        "the sub-agent's spend must stay under its own session id, not be merged")
