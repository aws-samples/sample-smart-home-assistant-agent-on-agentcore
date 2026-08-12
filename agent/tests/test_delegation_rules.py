"""The delegation prompt must name tools that exist, and not contradict itself.

The ROUTING TABLE is now generated from the granted AgentCards
(`a2a_prompt.build_routing_table`), so a renamed skill can no longer leave the table
pointing at a tool that does not exist — the table and the tools come from one
source. What remains checkable, and is checked here, is the HAND-WRITTEN half:
`A2A_DELEGATION_PREAMBLE` and `A2A_DELEGATION_EPILOGUE` still name specific tools
and agents in their guidance, and those mentions can go stale exactly the way the
whole table used to. A stale mention reads fine and makes the model fall back to its
own knowledge, which is why it needs a test rather than a review.

Tool names come from `a2a_prompt.tool_name`, which both the prompt and the tool
builder use, so they are derivable from the cards — which is what these tests do
rather than restating them.
"""

import json
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
AGENT_PY = os.path.join(ROOT, "agent", "agent.py")
A2A_DIR = os.path.join(ROOT, "a2a-agent-registry")
assert os.path.isfile(AGENT_PY), AGENT_PY


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _constant(name):
    """Read a module-level string constant without importing agent.py.

    agent.py imports strands, boto3 and the AgentCore SDK at module scope; these
    tests need one literal out of it.
    """
    import ast

    for node in ast.parse(_read(AGENT_PY)).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in agent/agent.py")


def _slug(value):
    """Mirror of tools/a2a.py `_slug` — kept in sync by the test below.

    The character class keeps `_`, so `inspect_devices` survives intact rather
    than collapsing; getting that wrong makes every derived name subtly different
    from the real one.
    """
    return re.sub(r"[^a-z0-9_]+", "_", (value or "").lower()).strip("_") or "x"


def _expected_tool_names():
    """Every a2a_* tool name the deployed cards can produce."""
    names = set()
    for entry in sorted(os.listdir(A2A_DIR)):
        card = os.path.join(A2A_DIR, entry, "card.json")
        if not os.path.isfile(card):
            continue
        data = json.loads(_read(card))
        agent_name = data.get("name", "")
        for skill in data.get("skills") or []:
            names.add(f"a2a_{_slug(agent_name)}_{_slug(skill.get('id', ''))}")
    return names


RULES = _constant("A2A_DELEGATION_PREAMBLE") + "\n" + _constant("A2A_DELEGATION_EPILOGUE")
PROMPT = _constant("SYSTEM_PROMPT")
# Tool names the rules mention. A trailing `*` is a deliberate wildcard for a
# family of skills, so it is matched as a prefix.
MENTIONED = set(re.findall(r"\ba2a_[a-z0-9_]+\*?", RULES))


def test_the_prompt_and_the_tool_builder_share_one_name_function():
    """Two derivations of the tool name could disagree, and the symptom would be a
    prompt naming a tool that does not exist with nothing logged."""
    src = _read(os.path.join(ROOT, "agent", "tools", "a2a.py"))
    assert "a2a_prompt.tool_name(agent_name" in src, (
        "tools/a2a.py no longer builds tool names via a2a_prompt.tool_name; the "
        "generated routing table would then be able to disagree with the tools")

    import sys
    sys.path.insert(0, os.path.join(ROOT, "agent"))
    import a2a_prompt

    assert a2a_prompt.tool_name("knowledge-qa-agent", "answer_from_docs") == \
        "a2a_knowledge_qa_agent_answer_from_docs"
    # The `_` must survive slugging or multi-word skill ids collapse together.
    assert a2a_prompt.slug("answer_from_docs") == "answer_from_docs"


def test_every_tool_the_rules_name_actually_exists():
    """A renamed skill leaves the prompt routing to a tool that is not there, and
    the model falls back to answering from general knowledge."""
    expected = _expected_tool_names()
    unknown = []
    for mention in MENTIONED:
        if mention.endswith("*"):
            prefix = mention[:-1]
            if not any(n.startswith(prefix) for n in expected):
                unknown.append(mention)
        elif mention not in expected:
            unknown.append(mention)
    assert not unknown, (
        f"the delegation rules route to tool(s) no AgentCard produces: "
        f"{sorted(unknown)}. Known: {sorted(expected)}")


def test_every_granted_specialist_gets_a_generated_row():
    """The property the whole generation change exists for.

    Granting a sub-agent must be sufficient to make the model route to it. Before,
    the table was hand-written, so a newly granted specialist had its tools
    registered and was never mentioned in the prompt — the model kept answering
    from its own knowledge and nothing reported it. Here every deployed card is
    treated as granted, and every one of them must produce a row naming its real
    tool.
    """
    import sys
    sys.path.insert(0, os.path.join(ROOT, "agent"))
    import a2a_prompt

    cards, grants = {}, {}
    for entry in sorted(os.listdir(A2A_DIR)):
        card_path = os.path.join(A2A_DIR, entry, "card.json")
        if not os.path.isfile(card_path):
            continue
        data = json.loads(_read(card_path))
        name = data.get("name", "")
        skills = [s["id"] for s in (data.get("skills") or []) if s.get("id")]
        if not name or not skills:
            continue
        cards[name] = data
        grants[name] = skills

    assert cards, "no deployed AgentCards found to check against"
    table = a2a_prompt.build_routing_table(grants, cards)

    for name, skills in grants.items():
        for skill in skills:
            tool = a2a_prompt.tool_name(name, skill)
            assert tool in table, (
                f"{tool} is deployed and granted but got no routing row, so the "
                f"model is never told to call it")


def test_an_ungranted_specialist_gets_no_row():
    """The other half: a user must not be told about specialists they cannot reach,
    or the model calls one and the platform refuses it mid-turn."""
    import sys
    sys.path.insert(0, os.path.join(ROOT, "agent"))
    import a2a_prompt

    cards = {
        "knowledge-qa-agent": {"skills": [
            {"id": "answer_from_docs", "description": "Answer from the manual."}]},
        "home-security-agent": {"skills": [
            {"id": "risk_assessment", "description": "Assess risk."}]},
    }
    table = a2a_prompt.build_routing_table(
        {"knowledge-qa-agent": ["answer_from_docs"]}, cards)
    assert "a2a_knowledge_qa_agent_answer_from_docs" in table
    assert "home_security" not in table


def test_no_grants_produces_no_section_at_all():
    import sys
    sys.path.insert(0, os.path.join(ROOT, "agent"))
    import a2a_prompt

    assert a2a_prompt.build_routing_table({}, {}) == ""
    # A grant for an agent with no card must not produce an empty header either.
    assert a2a_prompt.build_routing_table({"ghost-agent": ["s"]}, {}) == ""


def test_the_rules_override_the_capability_list_explicitly():
    """The capability list and the routing table disagreed about the knowledge
    base: the list claimed query_knowledge_base for documentation questions, which
    is exactly what knowledge-QA exists for. Whichever the model read last won."""
    assert "OVERRIDE" in RULES
    assert "query_knowledge_base" in RULES, (
        "the rules no longer say which of the two paths wins for documentation")


def test_the_fast_path_still_keeps_single_device_actions_local():
    """Delegating a light switch adds seconds for nothing. Measured: the fast
    paths call control_device / query_device_state / query_sensor_history /
    navigate_to_page directly, and this is the instruction that keeps them there."""
    assert "DO IT YOURSELF" in RULES
    for tool in ("control_device", "query_device_state", "query_sensor_history",
                 "discover_devices", "navigate_to_page"):
        assert tool in RULES, f"{tool} is no longer listed as a direct call"


def test_the_rules_tell_the_model_to_refuse_rather_than_improvise():
    """The refusal is the honest answer when a domain has no registered tool, and
    it is what makes an ungranted specialist visible instead of silently
    substituted for."""
    assert "IF NO TOOL MATCHES" in RULES
    assert "general knowledge" in RULES


def test_pending_actions_are_explained_to_the_orchestrator():
    """A saved scene hands back actions for the ORCHESTRATOR to apply, which is
    what keeps a scene-driven command under the same per-user authorisation as a
    typed one. If the prompt does not say so, the model reports the scene as
    already applied and nothing happens."""
    assert "pendingActions" in RULES
    assert "control_device" in RULES


def test_the_capability_list_points_at_the_routing_rules():
    """The list is read first and is where a model looks for what it can do; it
    has to hand off to the routing table rather than answering the question
    itself."""
    assert "a2a_<agent>_<skill>" in PROMPT
    assert "routing rules" in PROMPT.lower()


def test_the_prompt_mirror_is_regenerated_when_the_rules_change():
    """agent_prompt_defaults mirrors SYSTEM_PROMPT for the Admin Console's
    "Revert to Default". test_prompt_defaults_mirror.py enforces equality; this
    just points at it, so a failure there is not mistaken for an unrelated bug."""
    mirror = os.path.join(ROOT, "cdk", "lambda", "admin-api",
                          "agent_prompt_defaults.py")
    assert PROMPT in _read(mirror), (
        "cdk/lambda/admin-api/agent_prompt_defaults.py no longer matches "
        "SYSTEM_PROMPT — the console would offer a prompt the agent never ran")
