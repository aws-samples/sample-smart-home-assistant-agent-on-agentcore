"""The Memory strategies provisioned and the namespaces retrieved must agree.

A Memory strategy and its reader are configured in two different files, by two
different mechanisms, and nothing connects them at runtime:

  - `scripts/setup-agentcore.py` passes `--strategies ...` to the agentcore CLI,
    which provisions extraction on the memory resource.
  - `agent/memory/session.py` lists the namespaces the session manager retrieves
    from.

Provision a strategy without adding its namespace here and it still runs: it reads
every conversation, extracts records, writes them, and bills for it. They are
simply never read. Nothing errors, no namespace is missing, and the agent answers
normally — it just answers without the memory someone paid to build. The reverse
(retrieve a namespace no strategy writes) is equally quiet: the call returns zero
records forever.

That is the same silent-success shape as the rest of this system's history, so the
correspondence is pinned here rather than left to whoever remembers both files.

EPISODIC was the case in point twice over. First it was in neither place, so
nothing was broken — the capability was simply absent, and absent things do not
show up in any test or dashboard. Then it was enabled with the namespace pattern
the other three use (`/users/{actor}/episodes`), which the API ACCEPTS and never
populates: episodes are documented to live under `/strategy/{memoryStrategyId}/...`.
A measured probe made the difference visible — from the same six events, SEMANTIC
and USER_PREFERENCE produced records in ~50s while the mis-namespaced EPISODIC
produced none in six minutes.

One caveat when reading a failure here: unlike the other strategies, episodic
records appear only once AgentCore judges an episode COMPLETE ("if an episode is
not complete, it will take longer to generate because the system waits to see if
the conversation is continued"). An empty episodic namespace mid-conversation is
therefore expected, and is NOT evidence of a wiring bug — which is exactly why the
wiring is asserted statically here instead of by querying the service.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SESSION = os.path.join(REPO, "agent", "memory", "session.py")
SETUP = os.path.join(REPO, "scripts", "setup-agentcore.py")

# Every built-in strategy, and the namespace this deployment gives it. The
# reflection-namespace constraint on EPISODIC is what forced the shape of that one:
# the service rejects an episodic namespace that is not at or under its reflection
# namespace, so both are set to the same value in the provisioning call.
STRATEGY_NAMESPACES = {
    "SEMANTIC": "/users/{actor}/facts",
    "SUMMARIZATION": "/summaries/{actor}/{session}",
    "USER_PREFERENCE": "/users/{actor}/preferences",
    # STRATEGY-scoped, per the AWS docs: episodes live under
    # `/strategy/{memoryStrategyId}/...`, and the actor-level variant keeps one
    # user's episodes out of another's. The first attempt used
    # `/users/{actor}/episodes`, which the API ACCEPTS and then never populates —
    # so the wrong answer here is silent, which is why it is asserted.
    "EPISODIC": "/strategy/{strategyId}/actor/{actor}/",
}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _provisioned_strategies():
    """The strategy list handed to `agentcore add memory`."""
    src = _read(SETUP)
    m = re.search(r"--strategies\s+([A-Z_,]+)", src)
    assert m, "could not find the --strategies flag in setup-agentcore.py"
    return [s for s in m.group(1).split(",") if s]


def _retrieved_namespaces():
    """The namespace keys in session.py's retrieval_config, actor/session
    placeholders normalised so they can be compared with the table above."""
    src = _read(SESSION)
    # From the dict literal to the end of the function. NOT sliced at the first `}`
    # — that one belongs to an f-string placeholder (`{actor_id}`), and cutting
    # there silently truncated this to two entries and reported a namespace missing
    # that was present. Nor sliced at the dict's close: the EPISODIC entry is added
    # after it, inside an `if EPISODIC_STRATEGY_ID:` guard, because its namespace
    # needs an id that only exists at deploy time.
    block = src[src.index("retrieval_config = {"):]
    block = block[:block.index("return AgentCoreMemorySessionManager")]
    out = []
    for raw in re.findall(r'f"([^"]+)"', block):
        out.append(raw.replace("{actor_id}", "{actor}")
                      .replace("{session_id}", "{session}")
                      .replace("{EPISODIC_STRATEGY_ID}", "{strategyId}"))
    return out


# ---------------------------------------------------------------------------
# The correspondence
# ---------------------------------------------------------------------------

def test_all_four_builtin_strategies_are_provisioned():
    """The ask was "turn them all on", so a missing one is a regression."""
    got = set(_provisioned_strategies())
    assert got == set(STRATEGY_NAMESPACES), (
        f"provisioned strategies drifted.\n"
        f"  missing: {sorted(set(STRATEGY_NAMESPACES) - got)}\n"
        f"  unexpected: {sorted(got - set(STRATEGY_NAMESPACES))}")


def test_every_provisioned_strategy_has_a_namespace_that_is_retrieved():
    """The load-bearing assertion: extraction without retrieval is paid-for silence."""
    retrieved = _retrieved_namespaces()
    for strategy in _provisioned_strategies():
        ns = STRATEGY_NAMESPACES[strategy]
        assert ns in retrieved, (
            f"{strategy} is provisioned but nothing retrieves from {ns}. Its "
            f"records would be extracted, stored and billed, and never read — "
            f"add it to retrieval_config in agent/memory/session.py")


def test_every_retrieved_namespace_belongs_to_a_provisioned_strategy():
    """The reverse: a namespace no strategy writes returns zero records forever."""
    provisioned = {STRATEGY_NAMESPACES[s] for s in _provisioned_strategies()}
    for ns in _retrieved_namespaces():
        assert ns in provisioned, (
            f"retrieval_config reads {ns}, which no provisioned strategy writes")


def test_episodic_is_actor_partitioned_not_session_partitioned():
    """Why it is readable at all.

    SUMMARIZATION's namespace carries `{sessionId}`, which is what makes it
    unreadable from an A2A sub-agent (AgentCore assigns a runtimeSessionId per
    runtime and the hop does not propagate the orchestrator's). If EPISODIC ever
    grows a session placeholder, that reasoning — recorded in
    a2a-agent-registry/common/memory.py — silently stops holding.
    """
    ns = STRATEGY_NAMESPACES["EPISODIC"]
    assert "{session}" not in ns, (
        "an episodic namespace keyed by session would be unreadable from a "
        "sub-agent, invalidating the note in common/memory.py:namespaces_for")
    assert "{actor}" in ns


def test_retrieval_uses_one_entry_per_strategy_not_a_wildcard():
    """A wildcard would hide a missing strategy from every test above."""
    for ns in _retrieved_namespaces():
        assert "*" not in ns, f"{ns} is a wildcard; namespaces must be explicit"


def test_session_scoped_namespace_is_the_only_one_carrying_a_session():
    """Guards a copy-paste that would scope facts or episodes per session and
    quietly lose everything learned in earlier conversations."""
    with_session = [ns for ns in _retrieved_namespaces() if "{session}" in ns]
    assert with_session == [STRATEGY_NAMESPACES["SUMMARIZATION"]], with_session


# ---------------------------------------------------------------------------
# The env-var naming hazard
# ---------------------------------------------------------------------------

def test_the_memory_id_scan_excludes_the_strategy_id():
    """`MEMORY_STRATEGY_EPISODIC_ID` matches the `MEMORY_*_ID` pattern too.

    setup-agentcore.py finds the memory id by scanning the runtime env for the
    first key starting with `MEMORY_` and ending in `_ID` — which the episodic
    strategy id also satisfies. Dict iteration is insertion order, so once this
    script has patched the strategy id in, a later run could pick it up and hand it
    to the admin Lambda as MEMORY_ID. The symptom would surface as "memory not
    found" from a component that never touched memory, which is a long way from the
    cause.

    Asserted rather than trusted because the scan and the new variable are ~500
    lines apart in the same file.
    """
    src = _read(SETUP)
    scans = [m for m in re.finditer(
        r'k\.startswith\("MEMORY_"\)\s*and\s*k\.endswith\("_ID"\)', src)]
    assert scans, "the MEMORY_*_ID scan moved; this guard needs updating"
    for m in scans:
        window = src[m.start():m.start() + 400]
        assert 'MEMORY_STRATEGY_EPISODIC_ID' in window, (
            "a MEMORY_*_ID scan does not exclude MEMORY_STRATEGY_EPISODIC_ID; it "
            "can select the strategy id and use it as a memory id")


def test_the_episodic_strategy_id_is_patched_into_the_runtime():
    """Retrieval is skipped when the var is absent, so forgetting to set it
    disables episodic recall silently — the namespace needs an id only the
    deployment knows."""
    src = _read(SETUP)
    assert 'existing_env["MEMORY_STRATEGY_EPISODIC_ID"]' in src, (
        "setup-agentcore.py must patch MEMORY_STRATEGY_EPISODIC_ID into the text "
        "runtime, or agent/memory/session.py cannot build the episodic namespace")


def test_session_py_skips_episodic_rather_than_guessing_a_namespace():
    """A guessed namespace retrieves nothing forever and is indistinguishable from
    a strategy that simply has not produced records yet."""
    src = _read(SESSION)
    assert "if EPISODIC_STRATEGY_ID:" in src, (
        "the episodic entry must be conditional on the strategy id being known")
