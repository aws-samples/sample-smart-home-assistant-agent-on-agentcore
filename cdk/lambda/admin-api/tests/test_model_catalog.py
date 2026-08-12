"""Tests for the live model catalog (model_catalog.py).

The behaviour worth pinning here is not "does it call the API" but the three
judgement calls the module makes, each of which is silently wrong in a way an
admin would not notice:

  1. Where an inference profile exists, the profile id must win and the bare id
     must disappear, or the picker offers an id that fails at invoke time with
     "on-demand throughput isn't supported".
  2. A failed listing must produce `catalogError`, not a short list, and must not
     be cached.
"""
import sys
from unittest.mock import MagicMock, patch

import pytest

import model_catalog


@pytest.fixture(autouse=True)
def _clear_cache():
    model_catalog._CACHE = None
    model_catalog._CACHE_AT = 0.0
    yield
    model_catalog._CACHE = None
    model_catalog._CACHE_AT = 0.0


def _base(model_id, name, provider, *, on_demand=True, image=False,
          lifecycle="ACTIVE"):
    return {
        "modelArn": f"arn:aws:bedrock:us-west-2::foundation-model/{model_id}",
        "modelId": model_id,
        "modelName": name,
        "providerName": provider,
        "inferenceTypesSupported": ["ON_DEMAND"] if on_demand else ["PROVISIONED"],
        "inputModalities": ["TEXT", "IMAGE"] if image else ["TEXT"],
        "modelLifecycle": {"status": lifecycle},
    }


def _profile(profile_id, name, base_model_id, status="ACTIVE"):
    return {
        "inferenceProfileId": profile_id,
        "inferenceProfileName": name,
        "status": status,
        "models": [{
            "modelArn":
                f"arn:aws:bedrock:us-west-2::foundation-model/{base_model_id}",
        }],
    }


def _bedrock_stub(bases, profiles):
    client = MagicMock()
    client.list_foundation_models.return_value = {"modelSummaries": bases}
    client.list_inference_profiles.return_value = {
        "inferenceProfileSummaries": profiles}
    return client


def _run(bases, profiles):
    client = _bedrock_stub(bases, profiles)
    with patch.object(model_catalog, "_bedrock", return_value=client):
        return model_catalog.catalog(refresh=True)


def _ids(result):
    return [m["id"] for m in result["models"]]


def test_every_entry_is_on_the_runtime_endpoint():
    """Mantle is not integrated, so the picker must not imply another path exists."""
    result = _run(
        bases=[_base("moonshotai.kimi-k2.5", "Kimi K2.5", "Moonshot AI")],
        profiles=[],
    )
    assert [m["endpoint"] for m in result["models"]] == ["runtime"]
    assert _ids(result).count("moonshotai.kimi-k2.5") == 1


def test_inference_profile_replaces_the_bare_id():
    """A bare newer-Claude id fails at invoke time, so it must not be offered."""
    result = _run(
        bases=[_base("anthropic.claude-sonnet-4-6", "Claude Sonnet 4.6",
                     "Anthropic", on_demand=False, image=True)],
        profiles=[_profile("us.anthropic.claude-sonnet-4-6",
                           "US Claude Sonnet 4.6", "anthropic.claude-sonnet-4-6")],
    )
    assert _ids(result) == ["us.anthropic.claude-sonnet-4-6"]
    # Modalities come from the joined base model, not the profile.
    assert result["models"][0]["vision"] is True
    assert result["models"][0]["provider"] == "Anthropic"


def test_base_id_offered_when_no_profile_exists():
    result = _run(
        bases=[_base("deepseek.v3.2", "DeepSeek V3.2", "DeepSeek")],
        profiles=[],
    )
    assert _ids(result) == ["deepseek.v3.2"]


def test_provisioned_only_model_without_a_profile_is_dropped():
    """Naming it produces an error the admin cannot act on from this page."""
    result = _run(
        bases=[_base("some.provisioned-only", "Prov Only", "Other",
                     on_demand=False)],
        profiles=[],
    )
    assert _ids(result) == []


def test_inactive_profile_is_skipped():
    result = _run(
        bases=[_base("anthropic.claude-x", "Claude X", "Anthropic",
                     on_demand=False)],
        profiles=[_profile("us.anthropic.claude-x", "US Claude X",
                           "anthropic.claude-x", status="INACTIVE")],
    )
    # The profile is unusable and the base id is provisioned-only, so neither
    # is offered — better than offering one that cannot be invoked.
    assert _ids(result) == []


def test_deprecated_lifecycle_is_flagged_not_hidden():
    result = _run(
        bases=[_base("anthropic.claude-old", "Claude Old", "Anthropic",
                     lifecycle="LEGACY")],
        profiles=[],
    )
    assert result["models"][0]["deprecated"] is True


def test_a_failed_listing_reports_the_reason_rather_than_an_empty_list():
    """An empty picker and a denied permission must not look the same."""
    client = MagicMock()
    client.list_foundation_models.side_effect = RuntimeError("AccessDeniedException")
    with patch.object(model_catalog, "_bedrock", return_value=client):
        result = model_catalog.catalog(refresh=True)
    assert "AccessDeniedException" in result["catalogError"]
    assert result["models"] == []


def test_a_failed_catalog_is_not_cached():
    """Otherwise one throttle blinds the picker for a full TTL."""
    client = MagicMock()
    client.list_foundation_models.side_effect = RuntimeError("Throttling")
    with patch.object(model_catalog, "_bedrock", return_value=client):
        model_catalog.catalog(refresh=True)
    assert model_catalog._CACHE is None

    ok = _run(bases=[_base("deepseek.v3.2", "DeepSeek V3.2", "DeepSeek")],
              profiles=[])
    assert not ok["catalogError"]
    assert model_catalog._CACHE is not None


def test_missing_list_inference_profiles_permission_costs_only_profiles():
    """A denied ListInferenceProfiles must not empty the whole catalog."""
    client = _bedrock_stub([_base("deepseek.v3.2", "DeepSeek V3.2", "DeepSeek")], [])
    client.list_inference_profiles.side_effect = RuntimeError("AccessDenied")

    with patch.object(model_catalog, "_bedrock", return_value=client):
        result = model_catalog.catalog(refresh=True)

    assert _ids(result) == ["deepseek.v3.2"]
    # Degraded, not failed: the runtime listing worked, so nothing is reported.
    assert result["catalogError"] == ""


def test_endpoint_for_unknown_model_is_empty_not_a_guess():
    """The agent's fallback depends on telling "unknown" from "runtime"."""
    client = _bedrock_stub([_base("deepseek.v3.2", "D", "DeepSeek")], [])
    with patch.object(model_catalog, "_bedrock", return_value=client):
        assert model_catalog.endpoint_for("deepseek.v3.2") == "runtime"
        assert model_catalog.endpoint_for("who.knows") == ""
        assert model_catalog.endpoint_for("") == ""


def test_provider_ignores_the_geography_prefix():
    assert model_catalog._provider_of("us.anthropic.claude-x") == "Anthropic"
    assert model_catalog._provider_of("global.anthropic.claude-x") == "Anthropic"
    assert model_catalog._provider_of("openai.gpt-5.6-luna") == "OpenAI"
    # An unknown prefix is kept rather than dropped.
    assert model_catalog._provider_of("newvendor.model-1") == "newvendor"


def test_label_from_id_is_readable():
    """Recognisable, not typographically perfect — the UI shows the id too."""
    assert model_catalog._label_from_id("qwen.qwen3-32b") == "QWEN3 32B"
    assert model_catalog._label_from_id("google.gemma-4-e2b") == "Gemma 4 E2B"
    # The case that killed the cleverer version of this function: no fragment
    # gets welded to another, so a long id degrades gracefully instead of oddly.
    assert model_catalog._label_from_id("qwen.qwen3-235b-a22b-2507") == \
        "QWEN3 235B A22B 2507"
