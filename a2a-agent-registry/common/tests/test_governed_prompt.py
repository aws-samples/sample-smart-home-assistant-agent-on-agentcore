"""Tests for a sub-agent's admin-governed prompt resolution.

The risk here is asymmetric, so the tests are too:

  - Ignoring an override that IS readable is a governance hole — an admin saves a
    prompt, the console confirms it, and the agent keeps running the old one.
  - Refusing to answer because the governance table was throttled is worse than
    using the perfectly good prompt compiled into the image.

So a readable override must always win, and every failure must fall back rather
than raise. The two are tested separately because the tempting implementation
(one broad try/except around the whole function) satisfies the second while
quietly breaking the first.
"""

import sys
import types

import pytest

from common import governed_prompt as gp


SHIPPED = "You are the light effect specialist."
AGENT = "light-effect-agent"


class FakeTable:
    """A skills table holding whatever rows a test puts in it."""

    def __init__(self, rows=None, raises=None):
        # rows: {(userId, skillName): promptBody}
        self.rows = rows or {}
        self.raises = raises
        self.gets = []

    def get_item(self, Key):  # noqa: N803 - boto3's parameter name
        self.gets.append((Key["userId"], Key["skillName"]))
        if self.raises:
            raise self.raises
        body = self.rows.get((Key["userId"], Key["skillName"]))
        return {"Item": {"promptBody": body}} if body is not None else {}


@pytest.fixture(autouse=True)
def _no_real_table(monkeypatch):
    """Never let a test reach DynamoDB, and never let one leak its table into
    the next through the module-level cache."""
    monkeypatch.setattr(gp, "_table", None)
    yield
    gp._table = None


def _with_table(monkeypatch, table):
    monkeypatch.setattr(gp, "_get_table", lambda: table)
    return table


def _sk(name=AGENT):
    return f"__prompt_{name}__"


# ---------------------------------------------------------------------------
# The shipped prompt is the floor
# ---------------------------------------------------------------------------

def test_no_table_configured_uses_the_shipped_prompt():
    """Prompt governance not wired up for this agent yet — it must still run."""
    assert gp.resolve_system_prompt(AGENT, SHIPPED, user_id="u1") == SHIPPED


def test_no_override_anywhere_uses_the_shipped_prompt(monkeypatch):
    _with_table(monkeypatch, FakeTable())
    assert gp.resolve_system_prompt(AGENT, SHIPPED, user_id="u1") == SHIPPED


def test_a_dynamodb_failure_falls_back_rather_than_raising(monkeypatch):
    """A throttled governance read must not take the agent down."""
    _with_table(monkeypatch, FakeTable(raises=RuntimeError("throttled")))
    assert gp.resolve_system_prompt(AGENT, SHIPPED, user_id="u1") == SHIPPED


def test_a_whitespace_only_override_is_not_an_override(monkeypatch):
    """Otherwise an accidentally-blanked editor would leave the agent with no
    instructions at all."""
    _with_table(monkeypatch, FakeTable({("__global__", _sk()): "   \n  "}))
    assert gp.resolve_system_prompt(AGENT, SHIPPED, user_id="u1") == SHIPPED


def test_a_non_string_body_is_ignored(monkeypatch):
    _with_table(monkeypatch, FakeTable({("__global__", _sk()): 42}))
    assert gp.resolve_system_prompt(AGENT, SHIPPED, user_id="u1") == SHIPPED


# ---------------------------------------------------------------------------
# A readable override always wins
# ---------------------------------------------------------------------------

def test_a_global_override_replaces_the_shipped_prompt(monkeypatch):
    """Replaces, not appends: an admin who writes a global prompt for a
    specialist has rewritten it. Matches the orchestrator, whose built-in
    constant is used only when both scopes are empty."""
    _with_table(monkeypatch, FakeTable({("__global__", _sk()): "GLOBAL"}))
    assert gp.resolve_system_prompt(AGENT, SHIPPED, user_id="u1") == "GLOBAL"


def test_a_user_override_is_appended_to_the_global_one(monkeypatch):
    _with_table(monkeypatch, FakeTable({
        ("__global__", _sk()): "GLOBAL",
        ("u1", _sk()): "USER",
    }))
    assert gp.resolve_system_prompt(AGENT, SHIPPED, user_id="u1") == "GLOBAL\n\nUSER"


def test_a_user_override_alone_is_appended_to_the_shipped_prompt(monkeypatch):
    """The per-user scope is additive at every level, so with no global override
    the shipped prompt is what it adds to — a user addendum must not silently
    discard the agent's own instructions."""
    _with_table(monkeypatch, FakeTable({("u1", _sk()): "USER"}))
    out = gp.resolve_system_prompt(AGENT, SHIPPED, user_id="u1")
    assert out == f"{SHIPPED}\n\nUSER"


def test_another_users_override_does_not_leak(monkeypatch):
    table = _with_table(monkeypatch, FakeTable({("u2", _sk()): "OTHER USER"}))
    assert gp.resolve_system_prompt(AGENT, SHIPPED, user_id="u1") == SHIPPED
    assert ("u2", _sk()) not in table.gets


def test_an_anonymous_caller_gets_only_the_global_override(monkeypatch):
    """A prompt-only agent runs without a verified user, so there is no user
    scope to read — and no user row should be read on its behalf."""
    table = _with_table(monkeypatch, FakeTable({
        ("__global__", _sk()): "GLOBAL",
        ("", _sk()): "SHOULD NEVER BE READ",
    }))
    assert gp.resolve_system_prompt(AGENT, SHIPPED, user_id=None) == "GLOBAL"
    assert [uid for uid, _ in table.gets] == ["__global__"]


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------

def test_the_sort_key_matches_what_the_console_writes(monkeypatch):
    """`__prompt_<agentName>__`, keyed by AgentCard name — the same shape
    agent/agent.py uses with "text"/"voice" in the slot. A mismatch here means
    every saved override is invisible, with no error on either side."""
    assert gp.prompt_sort_key(AGENT) == "__prompt_light-effect-agent__"
    table = _with_table(monkeypatch, FakeTable())
    gp.resolve_system_prompt(AGENT, SHIPPED, user_id="u1")
    assert {sk for _, sk in table.gets} == {"__prompt_light-effect-agent__"}


def test_each_agent_reads_its_own_key(monkeypatch):
    """Two specialists sharing one prompt row would make an edit to one change
    the other."""
    table = _with_table(monkeypatch, FakeTable({
        ("__global__", "__prompt_light-effect-agent__"): "LIGHT",
        ("__global__", "__prompt_knowledge-qa-agent__"): "QA",
    }))
    assert gp.resolve_system_prompt("light-effect-agent", SHIPPED) == "LIGHT"
    assert gp.resolve_system_prompt("knowledge-qa-agent", SHIPPED) == "QA"


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def test_the_prompt_is_re_read_every_call(monkeypatch):
    """No caching, by design: a cached prompt makes a saved edit look ignored for
    however long the TTL is, which is indistinguishable from the bug this whole
    feature exists to remove."""
    table = _with_table(monkeypatch, FakeTable({("__global__", _sk()): "V1"}))
    assert gp.resolve_system_prompt(AGENT, SHIPPED) == "V1"
    table.rows[("__global__", _sk())] = "V2"
    assert gp.resolve_system_prompt(AGENT, SHIPPED) == "V2"


def test_the_table_resource_is_built_once(monkeypatch):
    """The boto3 Table object is cached even though the read is not — building it
    per request would add a client construction to every call."""
    calls = []

    fake_table = object()

    def fake_resource(_svc, region_name=None):  # noqa: ARG001
        calls.append(region_name)
        return types.SimpleNamespace(Table=lambda _n: fake_table)

    monkeypatch.setitem(sys.modules, "boto3",
                        types.SimpleNamespace(resource=fake_resource))
    monkeypatch.setenv("SKILLS_TABLE_NAME", "smarthome-skills")
    monkeypatch.setattr(gp, "_table", None)

    assert gp._get_table() is fake_table
    assert gp._get_table() is fake_table
    assert len(calls) == 1


def test_an_unset_table_name_is_not_cached_as_a_table(monkeypatch):
    """Otherwise an agent deployed before the env var was added would keep
    returning None even after a restart with it set."""
    monkeypatch.delenv("SKILLS_TABLE_NAME", raising=False)
    monkeypatch.setattr(gp, "_table", None)
    assert gp._get_table() is None
    assert gp._table is None
