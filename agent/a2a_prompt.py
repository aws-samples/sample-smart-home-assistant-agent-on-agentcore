"""Generating the orchestrator's A2A routing table from the granted AgentCards.

The table used to be hand-written in `agent.py`, which was the reason granting a new
sub-agent was not enough to make it usable: its tools were registered, but the prompt
that tells the model *when* to delegate had never heard of it, so the model kept
answering from its own knowledge. Nothing failed; the capability just did not appear.

So the rows are generated here from the same AgentCard fields the tool descriptions
already use, and the surrounding guidance stays hand-written in `agent.py`. The split
is deliberate:

  - GENERATED: one row per granted skill, "what the user asks about" -> tool name.
    This is per-agent, mechanical, and must track grants exactly.
  - HAND-WRITTEN: that delegation costs seconds, that a match makes the call
    mandatory, routing on subject rather than phrasing, NOW versus LATER, the
    two-step choreography, asking independent specialists together. None of that is
    derivable from a skill description, and it is where the measured behaviour came
    from.

The cost of generating is that row quality now depends on what a sub-agent author
writes in their card. `agent/tests/test_delegation_rules.py` checks that every tool
name the prompt mentions actually exists; the delegation evals check that the model
still routes.

This module is import-light on purpose (no strands, no boto3) so it can be tested
without loading the orchestrator.
"""

from __future__ import annotations

import re

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Roughly the width the hand-written table used, so the generated one reads the same
# and the prompt's shape does not change under it.
_DESC_WIDTH = 42


def slug(raw: str) -> str:
    """`knowledge-qa-agent` -> `knowledge_qa_agent`. The tool-name form."""
    return _SLUG_RE.sub("_", (raw or "").lower()).strip("_") or "x"


def tool_name(agent_name: str, skill_id: str) -> str:
    """The Strands tool name for one granted skill.

    Single definition, imported by `tools/a2a.py` when it builds the tools. If the
    prompt and the tool builder derived this separately they could disagree, and the
    symptom would be a prompt confidently naming a tool that does not exist — which
    the model answers by falling back to its own knowledge, silently.
    """
    return f"a2a_{slug(agent_name)}_{slug(skill_id)}"


def _first_sentence(text: str) -> str:
    """One line of prose from a skill description.

    Card descriptions run to several sentences; a routing table needs the gist. Split
    on the first sentence end rather than truncating mid-word.
    """
    clean = " ".join((text or "").split())
    if not clean:
        return ""
    match = re.search(r"(?<=[.!?])\s", clean)
    return (clean[:match.start()] if match else clean).rstrip(".")


def _wrap(text: str, width: int, indent: str) -> list[str]:
    """Greedy wrap. Avoids importing textwrap for one call on a hot path."""
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return [(indent if i else "") + line for i, line in enumerate(lines)] or [""]


def _row(description: str, name: str) -> str:
    """One table row: wrapped description on the left, tool name on the right."""
    lines = _wrap(description, _DESC_WIDTH, "")
    out = [f"  {lines[0].ljust(_DESC_WIDTH)}  {name}"]
    for extra in lines[1:]:
        out.append(f"  {extra}")
    return "\n".join(out)


def build_routing_table(grants: dict[str, list[str]],
                        cards: dict[str, dict]) -> str:
    """The routing table for the tools this turn will actually register.

    `grants` is {agentCardName: [skill_id, ...]} as the `cognito:groups` claim gives
    it; `cards` is {agentCardName: AgentCard}. A granted agent with no card, or a
    granted skill the card does not publish, produces no row — the same filtering
    `build_a2a_tools` applies, so the prompt and the tool list stay in agreement.

    Returns "" when nothing is granted, which the caller uses to skip appending the
    delegation section at all: a user with no specialists should not be told about
    specialists.
    """
    rows: list[str] = []
    for agent_name in sorted(grants):
        card = cards.get(agent_name)
        if not card:
            continue
        published = {s.get("id"): s for s in (card.get("skills") or []) if s.get("id")}
        for skill_id in sorted(set(grants[agent_name])):
            skill = published.get(skill_id)
            if not skill:
                continue
            description = (_first_sentence(skill.get("description", ""))
                           or skill.get("name", "") or skill_id)
            rows.append(_row(description, tool_name(agent_name, skill_id)))

    if not rows:
        return ""

    header = (f"  {'the user asks about'.ljust(_DESC_WIDTH)}  call\n"
              f"  {'-' * _DESC_WIDTH}  {'-' * 32}")
    return "\n".join([header, *rows])
