"""Tests for which log groups the spans queries read.

Covers the fix for a silent six-day outage. AgentCore Runtime used to export OTel
spans to the account-wide `aws/spans` group and moved them, on 2026-08-05, into
each runtime's own `/aws/bedrock-agentcore/runtimes/{id}-DEFAULT` group. Nothing
raised: `StartQuery` kept succeeding against `aws/spans` and kept matching zero
records, so the Overview page's TTFT and token-split cards read "no data" as
though the system were idle, and the Sessions tab showed no tokens.

The tests that matter most here are the two failure modes, both of which were
verified against the live account rather than assumed:

  - `StartQuery` rejects the ENTIRE request with ResourceNotFoundException if any
    one named group is missing. A single torn-down specialist runtime left in
    DASHBOARD_EXTRA_RUNTIME_ARNS would therefore break every spans card, which is
    why the group list is filtered through DescribeLogGroups first.
  - The legacy group must stay in the list, because a 30d dashboard range still
    reaches back past the cutover.
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

TEXT_GROUP = "/aws/bedrock-agentcore/runtimes/smarthome_smarthome-ee97ToCthI-DEFAULT"
VOICE_GROUP = ("/aws/bedrock-agentcore/runtimes/"
               "smarthomevoice_smarthomevoice-HAi7bi8jxA-DEFAULT")
A2A_GROUP = "/aws/bedrock-agentcore/runtimes/sha2aenergy_sha2aenergy-2LtP3f5f4a-DEFAULT"
LEGACY = "aws/spans"


def _load(text=TEXT, voice="", extras=""):
    os.environ["AGENT_RUNTIME_ARN"] = text
    os.environ["VOICE_AGENT_RUNTIME_ARN"] = voice
    os.environ["DASHBOARD_EXTRA_RUNTIME_ARNS"] = extras
    import dashboard
    return importlib.reload(dashboard)


class _FakeLogs:
    """A logs client whose DescribeLogGroups knows only `existing`."""

    def __init__(self, existing, fail=False):
        self.existing = set(existing)
        self.fail = fail
        self.described = []

    def describe_log_groups(self, logGroupNamePrefix, limit=50):
        if self.fail:
            raise RuntimeError("throttled")
        self.described.append(logGroupNamePrefix)
        return {
            "logGroups": [{"logGroupName": n} for n in sorted(self.existing)
                          if n.startswith(logGroupNamePrefix)]
        }


@pytest.fixture
def patched(monkeypatch):
    """Install a fake logs client into a freshly loaded dashboard module."""
    def _install(mod, existing, fail=False):
        fake = _FakeLogs(existing, fail)
        monkeypatch.setattr(mod, "_client", lambda svc: fake)
        return fake
    return _install


# ---------------------------------------------------------------------------
# ARN -> per-runtime span log group
# ---------------------------------------------------------------------------

def test_runtime_group_derived_from_each_configured_arn():
    mod = _load(voice=VOICE, extras=A2A)
    assert mod._runtime_span_log_groups() == [TEXT_GROUP, VOICE_GROUP, A2A_GROUP]


def test_runtime_groups_empty_when_no_arn_configured():
    # Not an error: the caller falls back to the legacy group alone, which is
    # better than querying nothing.
    assert _load(text="")._runtime_span_log_groups() == []


def test_runtime_groups_dedup():
    mod = _load(extras=f"{TEXT},{TEXT}")
    assert mod._runtime_span_log_groups() == [TEXT_GROUP]


# ---------------------------------------------------------------------------
# The group list actually queried
# ---------------------------------------------------------------------------

def test_per_runtime_groups_come_first_then_legacy(patched):
    mod = _load(voice=VOICE)
    patched(mod, [TEXT_GROUP, VOICE_GROUP, LEGACY])
    assert mod._spans_log_groups() == [TEXT_GROUP, VOICE_GROUP, LEGACY]


def test_legacy_group_is_still_queried(patched):
    """A 30d range straddles the 2026-08-05 cutover.

    Dropping the legacy group would erase five weeks of history from a page whose
    whole purpose is the trend line.
    """
    mod = _load()
    patched(mod, [TEXT_GROUP, LEGACY])
    assert LEGACY in mod._spans_log_groups()


def test_a_missing_group_is_dropped_not_passed_through(patched):
    """The core regression guard.

    StartQuery fails the WHOLE query if one named group is absent, so a
    specialist runtime torn down but still listed in DASHBOARD_EXTRA_RUNTIME_ARNS
    would take down every spans card on the page.
    """
    mod = _load(extras=A2A)
    patched(mod, [TEXT_GROUP, LEGACY])  # the A2A runtime is gone
    groups = mod._spans_log_groups()
    assert A2A_GROUP not in groups
    assert groups == [TEXT_GROUP, LEGACY]


def test_describe_failure_leaves_the_list_intact(patched):
    """Losing the ability to check is not evidence that nothing exists.

    On a throttled DescribeLogGroups the query still runs; its own error handling
    is the backstop. Returning [] here would turn one throttle into an empty
    dashboard.
    """
    mod = _load()
    patched(mod, [], fail=True)
    assert mod._spans_log_groups() == [TEXT_GROUP, LEGACY]


def test_all_groups_missing_yields_empty_list(patched):
    mod = _load()
    patched(mod, [])
    assert mod._spans_log_groups() == []


# ---------------------------------------------------------------------------
# The query call itself
# ---------------------------------------------------------------------------

def test_query_uses_log_group_names_plural(monkeypatch, patched):
    """`logGroupName` (singular) cannot express a union, and was the bug.

    Asserted on the actual kwargs rather than on behaviour because the singular
    form would still return a valid empty result — which is exactly how the
    original outage stayed invisible.
    """
    mod = _load(voice=VOICE)
    fake = patched(mod, [TEXT_GROUP, VOICE_GROUP, LEGACY])
    captured = {}

    class _Q(_FakeLogs):
        exceptions = type("E", (), {"ResourceNotFoundException": type(
            "RNF", (Exception,), {})})

        def start_query(self, **kwargs):
            captured.update(kwargs)
            return {"queryId": "q1"}

        def get_query_results(self, queryId):
            return {"status": "Complete", "results": []}

    q = _Q([TEXT_GROUP, VOICE_GROUP, LEGACY])
    monkeypatch.setattr(mod, "_client", lambda svc: q)
    mod._run_logs_insights("filter @message like /x/", days=7)

    assert "logGroupName" not in captured
    assert captured["logGroupNames"] == [TEXT_GROUP, VOICE_GROUP, LEGACY]


def test_query_returns_none_when_no_group_exists(monkeypatch, patched):
    mod = _load()
    patched(mod, [])
    # No start_query at all: nothing to query, and passing [] would be a 400.
    assert mod._run_logs_insights("filter @message like /x/", days=7) is None
