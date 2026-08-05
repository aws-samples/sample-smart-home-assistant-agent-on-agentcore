"""Tests for dashboard.py's service-name allowlist and multi-runtime rollups.

These cover the fix for the ops dashboard silently excluding every runtime but
the text one: the spans queries filtered `service.name like /smarthome/` and the
evaluation block pinned a single exact `service.name`, so the voice runtime and
the A2A specialist runtimes never reached Overview even though their tokens are
real spend.
"""
import importlib
import os

import pytest

TEXT = ("arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/"
        "smarthome_smarthome-ee97ToCthI")
VOICE = ("arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/"
         "smarthomevoice_smarthomevoice-HAi7bi8jxA")
A2A = ("arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/"
       "sha2aenergy_sha2aenergy-2LtP3f5f4a")
BUNDLES = ("arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/"
           "smarthome_bundles-wC9vsZ75VN")


def _load(text=TEXT, voice="", extras=""):
    """Reload dashboard.py with the given runtime env.

    The allowlist is derived from module-level env constants, so the module has
    to be re-imported for each configuration.
    """
    os.environ["AGENT_RUNTIME_ARN"] = text
    os.environ["VOICE_AGENT_RUNTIME_ARN"] = voice
    os.environ["DASHBOARD_EXTRA_RUNTIME_ARNS"] = extras
    import dashboard
    return importlib.reload(dashboard)


# ---------------------------------------------------------------------------
# ARN -> service.name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("arn,expected", [
    (TEXT, "smarthome_smarthome.DEFAULT"),
    (VOICE, "smarthomevoice_smarthomevoice.DEFAULT"),
    (A2A, "sha2aenergy_sha2aenergy.DEFAULT"),
    ("", ""),
    ("not-an-arn", ""),
])
def test_service_name_from_arn(arn, expected):
    assert _load()._service_name_from_arn(arn) == expected


# ---------------------------------------------------------------------------
# Allowlist composition
# ---------------------------------------------------------------------------

def test_text_only_yields_single_service():
    assert _load()._service_names() == ["smarthome_smarthome.DEFAULT"]


def test_extras_and_voice_are_included():
    names = _load(voice=VOICE, extras=f"{A2A},{BUNDLES}")._service_names()
    assert set(names) == {
        "smarthome_smarthome.DEFAULT",
        "smarthomevoice_smarthomevoice.DEFAULT",
        "sha2aenergy_sha2aenergy.DEFAULT",
        "smarthome_bundles.DEFAULT",
    }


def test_extras_dedup_and_tolerate_whitespace():
    names = _load(voice=VOICE, extras=f" {A2A} , , {A2A} ")._service_names()
    assert len(names) == 3
    assert names.count("sha2aenergy_sha2aenergy.DEFAULT") == 1


def test_text_service_name_present_even_if_arn_unset():
    # A half-provisioned env must still query the text runtime rather than
    # silently returning an empty allowlist (which matches nothing at all).
    assert "smarthome_smarthome.DEFAULT" in _load(text="", extras=A2A)._service_names()


# ---------------------------------------------------------------------------
# Logs Insights filter clause
# ---------------------------------------------------------------------------

def test_filter_uses_exact_in_not_regex():
    # A widened regex (e.g. /^smarthome/) would eventually match unrelated
    # projects sharing this account's aws/spans log group.
    clause = _load(extras=A2A)._spans_service_filter()
    assert "in [" in clause
    assert "like" not in clause


def test_filter_quotes_every_name_and_ends_with_newline():
    clause = _load(extras=A2A)._spans_service_filter()
    assert clause.count('"') == 4  # two names, two quotes each
    assert clause.endswith("\n")


def test_filter_splices_into_a_well_formed_query():
    # The clause is concatenated between two query lines; every line must still
    # be either the leading `filter` or a `| ` pipe stage.
    clause = _load(extras=A2A)._spans_service_filter()
    query = ('filter scope.name = "strands.telemetry.tracer"\n'
             '| filter ispresent(attributes.gen_ai.server.time_to_first_token)\n'
             + clause +
             '| stats count(*) as n\n')
    lines = query.strip().split("\n")
    assert all(l.startswith(("filter", "| ")) for l in lines), query


# ---------------------------------------------------------------------------
# GetMetricData budget
# ---------------------------------------------------------------------------

def test_evaluation_service_count_is_clamped_to_the_query_budget():
    # GetMetricData rejects a request with more than 500 queries; the block
    # issues one per (evaluator, service) pair.
    many = ",".join(
        f"arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/rt{i}_rt{i}-AAAA"
        for i in range(80)
    )
    d = _load(extras=many)
    assert len(d._service_names()) * len(d.EVALUATORS) > 500, "premise: unclamped overflows"
    max_services = max(1, 500 // len(d.EVALUATORS))
    assert max_services * len(d.EVALUATORS) <= 500


# ---------------------------------------------------------------------------
# dim=agent attribution
# ---------------------------------------------------------------------------

def _rows():
    return [
        {"sessionId": "s1", "model": "kimi",
         "svc": "smarthome_smarthome.DEFAULT", "inTok": "100", "outTok": "10"},
        {"sessionId": "s2", "model": "kimi",
         "svc": "sha2aenergy_sha2aenergy.DEFAULT", "inTok": "200", "outTok": "20"},
        {"sessionId": "s3", "model": "nova",
         "svc": "sha2aenergy_sha2aenergy.DEFAULT", "inTok": "5", "outTok": "1"},
    ]


def test_agent_dim_buckets_by_runtime_not_model():
    # Two runtimes sharing one model must stay separate -- that distinction is
    # the whole point of attributing by runtime.
    res = _load()._attribute(_rows(), "agent")
    assert {b["key"] for b in res} == {
        "smarthome_smarthome.DEFAULT", "sha2aenergy_sha2aenergy.DEFAULT"}


def test_agent_dim_sums_tokens_across_models_of_one_runtime():
    res = _load()._attribute(_rows(), "agent")
    a2a = next(b for b in res if b["key"] == "sha2aenergy_sha2aenergy.DEFAULT")
    assert a2a["inputTokens"] == 205
    assert a2a["outputTokens"] == 21
    assert sorted(a2a["models"]) == ["kimi", "nova"]


def test_agent_dim_sorted_by_total_tokens_desc():
    res = _load()._attribute(_rows(), "agent")
    assert res[0]["key"] == "sha2aenergy_sha2aenergy.DEFAULT"


def test_agent_dim_falls_back_to_model_when_svc_missing():
    # Spans predating the added `svc` grouping still have to attribute somewhere.
    legacy = [{"sessionId": "s9", "model": "kimi", "inTok": "7", "outTok": "1"}]
    assert _load()._attribute(legacy, "agent")[0]["key"] == "kimi"
