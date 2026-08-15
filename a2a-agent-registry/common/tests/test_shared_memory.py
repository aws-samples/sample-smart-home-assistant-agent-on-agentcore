"""Tests for the sub-agents' read-only view of the shared AgentCore Memory.

The point of S1 is that a specialist knows what the user told the orchestrator.
Three things make that fragile, and each is asserted here rather than assumed:

  - The actor id must MATCH the orchestrator's. It is a namespace path component,
    so a divergence does not error — the specialist reads its own empty namespace
    and "never remembers anything", which reads as a retrieval bug.
  - The records must be read as text. Strategies return different shapes
    (SEMANTIC gives plain text, USER_PREFERENCE gives a JSON document with the
    useful part under `context`), and getting it wrong puts a JSON blob in the
    prompt that the model copes with just well enough that nobody notices.
  - Retrieval must never be able to WRITE. Only the orchestrator holds the whole
    conversation; a specialist sees one self-contained instruction, so anything it
    wrote would come back as a half-sentence "memory" forever.
"""
import importlib
import json
import os

import pytest


class _Caller:
    """The fields common/memory.py reads off a CallerIdentity."""

    def __init__(self, email="", sub=""):
        self.email = email
        self.sub = sub


def _load(memory_id="smarthome_SmartHomeMemory-TEST"):
    os.environ["MEMORY_SMARTHOMEMEMORY_ID"] = memory_id
    from common import memory
    return importlib.reload(memory)


class _FakeMemoryClient:
    """Records every call, so a test can assert what was NOT called."""

    def __init__(self, by_namespace=None, fail=()):
        self.by_namespace = by_namespace or {}
        self.fail = set(fail)
        self.calls = []

    def retrieve_memory_records(self, **kwargs):
        self.calls.append(kwargs)
        ns = kwargs["namespace"]
        if ns in self.fail:
            raise RuntimeError("throttled")
        return {"memoryRecordSummaries": [
            {"content": {"text": t}} for t in self.by_namespace.get(ns, [])
        ]}


# ---------------------------------------------------------------------------
# Actor id: must agree with the orchestrator
# ---------------------------------------------------------------------------

def test_actor_is_the_sanitized_email():
    # The orchestrator has keyed memory by email since before the sub-agents
    # existed, and there are live records under those ids.
    assert _load().memory_actor_for(
        _Caller(email="admin@smarthome.local", sub="88c1a3e0-b041")
    ) == "admin_smarthome_local"


def test_sub_is_only_a_fallback():
    assert _load().memory_actor_for(_Caller(sub="88c1a3e0-b041")) == "88c1a3e0-b041"


def test_no_identity_yields_no_actor():
    # NOT a shared default: one would pool unrelated users' preferences into a
    # single namespace.
    assert _load().memory_actor_for(_Caller()) == ""


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------

def test_the_session_summary_is_keyed_on_the_memory_session_not_a_runtime_one():
    """The correction that measuring produced.

    This namespace was documented for months as unreadable from a sub-agent,
    because the A2A hop propagated no runtime session id. But the session component
    is not a runtime session id: the orchestrator writes Memory under
    `memory_session_id(actor)` = `mem-{actor}`, stable across logins. A sub-agent
    holds the actor, so it can derive it — no propagation required.

    Verified against the live Memory: `mem-{actor}` holds the running summary and
    the `user-session-...` variant is empty.
    """
    mod = _load()
    assert mod.namespaces_for("admin_smarthome_local") == [
        "/users/admin_smarthome_local/facts",
        "/users/admin_smarthome_local/preferences",
        "/summaries/admin_smarthome_local/mem-admin_smarthome_local",
    ]


def test_the_memory_session_matches_what_the_orchestrator_computes():
    """Both sides must produce the same string from what each of them holds.

    The orchestrator has the raw email; a sub-agent has the sanitised actor. The
    shared rule is idempotent so the two agree — if they ever stopped agreeing, the
    specialist would read an empty namespace and look like it had no memory.
    """
    import sys, os
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    sys.path.insert(0, os.path.join(repo, "shared"))
    import memory_actor

    actor = memory_actor.memory_actor_id(email="admin@smarthome.local")
    orchestrator_side = memory_actor.memory_session_id("admin@smarthome.local")
    assert f"/summaries/{actor}/{orchestrator_side}" in _load().namespaces_for(actor)


def test_no_actor_means_no_namespaces():
    assert _load().namespaces_for("") == []


# ---------------------------------------------------------------------------
# Record shapes
# ---------------------------------------------------------------------------

def test_plain_text_record_is_used_as_is():
    mod = _load()
    assert mod._record_text({"content": {"text": "The user prefers warm light"}}) == \
        "The user prefers warm light"


def test_user_preference_json_is_unwrapped_to_its_context():
    """USER_PREFERENCE records arrive as JSON, not prose.

    Measured against the live memory: the useful sentence is under `context`.
    Passing the raw document through would put `{"context": "...", ...}` into the
    system prompt.
    """
    mod = _load()
    raw = json.dumps({"context": "The user asked for ocean mode repeatedly",
                      "preferenceType": "explicit", "categories": ["lighting"]})
    assert mod._record_text({"content": {"text": raw}}) == \
        "The user asked for ocean mode repeatedly"


def test_unparseable_json_falls_back_to_the_raw_text():
    mod = _load()
    broken = '{"broken'
    assert mod._record_text({"content": {"text": broken}}) == broken


def test_json_without_a_known_key_is_still_returned():
    # Better a compact JSON string than nothing: an unknown shape is a new
    # strategy, not a reason to drop the user's context.
    mod = _load()
    out = mod._record_text({"content": {"text": '{"somethingNew": "warm light"}'}})
    assert "warm light" in out


def test_a_long_record_is_truncated():
    mod = _load()
    out = mod._record_text({"content": {"text": "x" * 5000}})
    assert len(out) == mod.MAX_RECORD_CHARS


def test_a_missing_or_odd_content_shape_is_empty_not_an_error():
    mod = _load()
    assert mod._record_text({}) == ""
    assert mod._record_text({"content": {}}) == ""
    assert mod._record_text({"content": {"text": None}}) == ""


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def test_retrieves_from_every_namespace_with_the_request_as_the_query():
    mod = _load()
    client = _FakeMemoryClient({
        "/users/admin_smarthome_local/facts": ["Has a 30-segment light strip"],
        "/users/admin_smarthome_local/preferences": ["Prefers warm light at night"],
    })
    out = mod.retrieve_memory(_Caller(email="admin@smarthome.local"),
                              "make the living room cosy", client=client)
    assert "Has a 30-segment light strip" in out
    assert "Prefers warm light at night" in out
    # The request text is the search query — that is what makes three records
    # relevant rather than arbitrary.
    assert all(c["searchCriteria"]["searchQuery"] == "make the living room cosy"
               for c in client.calls)
    # Facts, preferences and the running session summary.
    assert len(client.calls) == 3


def test_lines_keep_namespace_order_despite_concurrency():
    """Facts, then preferences, then the summary — regardless of which returns first.

    Retrieval is concurrent now (three serial calls would sit on the critical path
    of every delegated turn). Completion order must not reach the prompt: a prompt
    whose lines reorder between turns is a prompt-cache miss, and the intended
    reading is general-to-specific.
    """
    mod = _load()

    class _SlowFirstNamespace(_FakeMemoryClient):
        def retrieve_memory_records(self, **kwargs):
            # The first namespace answers last.
            if kwargs["namespace"].endswith("/facts"):
                import time
                time.sleep(0.05)
            return super().retrieve_memory_records(**kwargs)

    client = _SlowFirstNamespace({
        "/users/admin_smarthome_local/facts": ["FACT"],
        "/users/admin_smarthome_local/preferences": ["PREFERENCE"],
        "/summaries/admin_smarthome_local/mem-admin_smarthome_local": ["SUMMARY"],
    })
    out = mod.retrieve_memory(_Caller(email="admin@smarthome.local"), "q",
                              client=client)
    assert out.index("FACT") < out.index("PREFERENCE") < out.index("SUMMARY")


def test_a_missing_summary_namespace_is_not_an_error():
    """A user with no summary yet is the normal case on their first conversation."""
    mod = _load()
    client = _FakeMemoryClient(
        {"/users/admin_smarthome_local/facts": ["Has a light strip"]},
        fail={"/summaries/admin_smarthome_local/mem-admin_smarthome_local"},
    )
    out = mod.retrieve_memory(_Caller(email="admin@smarthome.local"), "q",
                              client=client)
    assert "Has a light strip" in out


def test_one_failing_namespace_does_not_cost_the_other():
    mod = _load()
    client = _FakeMemoryClient(
        {"/users/admin_smarthome_local/preferences": ["Prefers warm light"]},
        fail={"/users/admin_smarthome_local/facts"},
    )
    out = mod.retrieve_memory(_Caller(email="admin@smarthome.local"), "cosy",
                              client=client)
    assert "Prefers warm light" in out


def test_duplicate_records_are_not_repeated():
    mod = _load()
    same = "Prefers warm light"
    client = _FakeMemoryClient({
        "/users/admin_smarthome_local/facts": [same],
        "/users/admin_smarthome_local/preferences": [same],
    })
    out = mod.retrieve_memory(_Caller(email="admin@smarthome.local"), "cosy",
                              client=client)
    assert out.count(same) == 1


def test_no_memory_id_configured_means_no_call_at_all():
    """A deployment without the shared memory env behaves exactly as before.

    Asserted on the client having been left untouched, because returning "" while
    still paying for two API calls per delegation would be a silent regression in
    the thing S1 is measured on.
    """
    mod = _load(memory_id="")
    client = _FakeMemoryClient({"/users/admin_smarthome_local/facts": ["x"]})
    assert mod.retrieve_memory(_Caller(email="admin@smarthome.local"), "cosy",
                               client=client) == ""
    assert client.calls == []


def test_an_empty_query_retrieves_nothing():
    mod = _load()
    client = _FakeMemoryClient({"/users/admin_smarthome_local/facts": ["x"]})
    assert mod.retrieve_memory(_Caller(email="admin@smarthome.local"), "  ",
                               client=client) == ""
    assert client.calls == []


def test_an_unidentified_caller_retrieves_nothing():
    mod = _load()
    client = _FakeMemoryClient({"/users//facts": ["x"]})
    assert mod.retrieve_memory(_Caller(), "cosy", client=client) == ""
    assert client.calls == []


# ---------------------------------------------------------------------------
# Read-only, by construction
# ---------------------------------------------------------------------------

def test_the_module_never_writes_an_event():
    """No write API is reachable from this module.

    Checked on the parsed AST rather than the file text, so the module's own
    docstring explaining why it does not write does not itself trip the check —
    which is exactly what the first version of this test did.

    Source-level because the guarantee is an absence, and an absence cannot be
    demonstrated by calling something. The IAM grant in deploy.py is the other
    half: it grants RetrieveMemoryRecords alone, so a future edit that added a
    write would fail rather than quietly poison the user's memory.
    """
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent.parent / "memory.py").read_text())
    called = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    for forbidden in ("create_event", "put_memory", "delete_memory_record",
                      "delete_memory", "batch_create_memory_records"):
        assert forbidden not in called, f"memory.py must not call {forbidden}"
    assert "AgentCoreMemorySessionManager" not in names, (
        "a session manager writes events on every turn; sub-agents must not hold one")


# ---------------------------------------------------------------------------
# The prompt section
# ---------------------------------------------------------------------------

def test_the_section_labels_memory_as_context_not_instruction():
    """Framing, not decoration.

    Unlabelled, these lines read as part of the current request: a specialist
    asked to dim the bedroom would apply a remembered ocean effect because the
    prompt appeared to ask for it. The model is told what this is and which side
    wins on conflict.
    """
    mod = _load()
    client = _FakeMemoryClient(
        {"/users/admin_smarthome_local/preferences": ["Likes ocean mode"]})
    section = mod.memory_prompt_section(
        _Caller(email="admin@smarthome.local"), "dim the bedroom", client=client)
    assert "Likes ocean mode" in section
    assert "NOT part of the current request" in section
    assert "the current request wins" in section


def test_nothing_remembered_yields_an_empty_section():
    # "" so the caller appends nothing and the prompt is byte-for-byte unchanged.
    mod = _load()
    assert mod.memory_prompt_section(
        _Caller(email="admin@smarthome.local"), "dim the bedroom",
        client=_FakeMemoryClient({})) == ""
