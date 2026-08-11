"""Tests for prompt caching on the orchestrator's model (spec 5 S3).

Every turn sends the same ~10.5k-token prefix: the system prompt (~1.6k), the A2A
routing table (~1.7k), eleven governed skills (~4.3k) and ~20 tool schemas. It was
being reprocessed on each call. With a cache point it is read from cache instead —
measured through a real Strands Agent: 12,019 tokens `cacheReadInputTokens` on the
second call, `inputTokens` down from 12,022 to 6.

What these tests protect is mostly the REASONING, because the failure modes are
silent in both directions:

  - Drop `cache_config` and nothing breaks; the bill quietly triples.
  - Hardcode `cache_prompt="default"` instead of `strategy="auto"` and it works
    until a user picks a model that does not support caching, at which point every
    one of their turns errors — and the model is per-user configurable here.
  - Put anything varying in FRONT of the stable prefix and cache hits stop, with
    no error and no log line: writes at 1.25x, reads at zero.
"""
import ast
import re
from pathlib import Path

import pytest

AGENT_PY = Path(__file__).resolve().parent.parent / "agent.py"
SERVER_PY = (Path(__file__).resolve().parent.parent.parent
             / "a2a-agent-registry" / "common" / "server.py")


def _create_agent_source() -> str:
    """The source of `create_agent`, where the model is built."""
    tree = ast.parse(AGENT_PY.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "create_agent")
    return ast.get_source_segment(AGENT_PY.read_text(encoding="utf-8"), fn) or ""


def test_the_orchestrator_model_enables_caching():
    assert "cache_config" in _create_agent_source(), (
        "create_agent no longer passes cache_config; every turn will reprocess "
        "the ~10.5k-token prefix and nothing will report it")


def test_caching_uses_auto_not_a_hardcoded_cache_point():
    """`strategy="auto"` because the model is per-user configurable.

    Admin Console -> Models lets a user pick their own model. `auto` asks Strands
    to check support and place the points, logging a warning and proceeding
    uncached when the model has none. A hardcoded `cache_prompt="default"` would
    instead fail every turn for that user.
    """
    src = _create_agent_source()
    assert 'CacheConfig(strategy="auto")' in src, (
        "expected CacheConfig(strategy=\"auto\") so an unsupported per-user model "
        "degrades instead of erroring")
    assert "cache_prompt" not in src, (
        "cache_prompt is the deprecated hardcoded form and does not check model "
        "support")


def test_cache_config_is_imported_from_strands():
    src = AGENT_PY.read_text(encoding="utf-8")
    assert re.search(r"from strands\.models\.bedrock import .*CacheConfig", src), (
        "CacheConfig must come from strands.models.bedrock")


def test_the_sub_agents_do_not_enable_caching():
    """The opposite decision, for measured reasons.

    A specialist's prefix runs 362-4,211 mean input tokens (measured across four
    deployed agents, minima as low as 71) — under the model-specific checkpoint
    minimum, where a cache point is silently ignored. And `execute` appends the
    governed prompt override and the user's retrieved memory per request, so the
    prefix is not stable anyway. Cache WRITES bill at 1.25x, so enabling it there
    would cost 25% more per delegation for zero hits.
    """
    src = SERVER_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_build_strands_agent")
    body = ast.get_source_segment(src, fn) or ""
    # The word appears in the explanatory comment; what must not appear is the
    # keyword argument actually being passed.
    assert not re.search(r"cache_config\s*=", body), (
        "sub-agent models must not enable caching: prefixes are below the "
        "checkpoint minimum and vary per request, so writes at 1.25x buy nothing")


def test_the_reason_for_each_choice_is_recorded():
    """Both files must say WHY, since both defaults are invisible when wrong.

    Not documentation for its own sake: the next person to touch either file sees
    two agents in one repo configured oppositely, and without the numbers the
    obvious "fix" is to make them consistent.
    """
    for path in (AGENT_PY, SERVER_PY):
        text = path.read_text(encoding="utf-8")
        assert "cach" in text.lower(), f"{path.name}: no mention of caching"
    agent_text = AGENT_PY.read_text(encoding="utf-8")
    # The honest framing: this is a cost win, not the latency win the spec hoped
    # for. Measured 2% on latency, which is inside the noise.
    assert "COST optimisation" in agent_text or "cost optimisation" in agent_text, (
        "agent.py should state that caching here is a cost saving, not a latency "
        "one — measured at 2% latency and 98% tokens")


def test_the_static_prefix_still_precedes_the_dynamic_parts():
    """Cache hits need an exact prefix match, so ordering is load-bearing.

    In `invoke_agent` the system prompt and the A2A rules are assembled first and
    the user's message arrives last, which is what makes the prefix reusable. This
    checks the A2A rules are still APPENDED to the prompt rather than the prompt
    being rebuilt around per-request data.
    """
    src = AGENT_PY.read_text(encoding="utf-8")
    assert re.search(r'\+\s*"\\n\\n"\s*\+\s*A2A_DELEGATION_RULES', src), (
        "the A2A rules should be appended to the end of the system prompt; "
        "inserting per-request content before them would break every cache hit")
