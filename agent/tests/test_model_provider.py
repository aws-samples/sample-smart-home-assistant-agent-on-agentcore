"""Tests for the model build seam (agent/model_provider.py).

Bedrock Mantle is not integrated in this build, so there is one path. What is still
worth pinning is that a stored `mantle` hint from the period when it WAS integrated
does not silently route somewhere that does not exist, and that the diagnostic line
still reports whether caching engaged — the failure that is silent in both
directions.
"""
from unittest.mock import MagicMock, patch

import model_provider as mp


def test_endpoint_always_resolves_to_runtime():
    assert mp.resolve_endpoint("us.anthropic.claude-sonnet-4-6") == "runtime"
    assert mp.resolve_endpoint("") == "runtime"


def test_a_stored_mantle_hint_resolves_to_runtime_rather_than_a_missing_path():
    """A settings row written while Mantle was integrated must not break a turn.

    Answering `runtime` means a Mantle-only id fails loudly at Converse and names the
    model, which is diagnosable. Honouring the hint would reach for a code path that
    is not in this build.
    """
    assert mp.resolve_endpoint("openai.gpt-5.6-luna", "mantle") == "runtime"


def test_build_model_passes_the_cache_config_through():
    sentinel = object()
    with patch("strands.models.bedrock.BedrockModel") as bm:
        mp.build_model("us.anthropic.claude-sonnet-4-6", "runtime",
                       cache_config=sentinel)
    kwargs = bm.call_args.kwargs
    assert kwargs["model_id"] == "us.anthropic.claude-sonnet-4-6"
    assert kwargs["cache_config"] is sentinel, (
        "dropping cache_config triples the bill and reports nothing")
    assert kwargs["streaming"] is True


def test_describe_reports_the_cache_strategy():
    """"Caching quietly stopped working" and "caching works" look identical from
    outside, so this line is the only signal."""
    model = MagicMock()
    model._cache_strategy = "anthropic"
    line = mp.describe(model, "us.anthropic.claude-sonnet-4-6", "runtime", "1.51.0")
    assert "strategy=anthropic" in line
    assert "endpoint=runtime" in line
    assert "strands=1.51.0" in line


def test_describe_says_unavailable_rather_than_implying_caching():
    line = mp.describe(object(), "some.model", "runtime", "1.51.0")
    assert "strategy=unavailable" in line
