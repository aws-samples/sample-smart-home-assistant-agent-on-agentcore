"""The tool docstring and the system prompt must agree about `discover_devices`.

This exists because of a bug that cost two deploys to find.

S2 attaches a "Devices already identified for this request" list to every
delegated message so the specialist can skip its opening `discover_devices` call —
measured at ~1.2s of an LLM turn whose only output was that one call. The system
prompts were updated to say so. It made no difference: the specialists kept
calling `discover_devices` on every request.

The reason was in the TOOL's own docstring, which still said "Call this FIRST,
every time". A tool description is attached to the very tool the model is deciding
whether to call, so when the two disagree the description wins — and nothing about
that is visible in a reply, which looked completely correct throughout. The
optimisation simply did not happen.

So: a source-level check that neither half contradicts the other. It cannot verify
the model's behaviour, but it can stop the specific contradiction that hid this for
two deploys.
"""
import re
from pathlib import Path

import pytest

REGISTRY_DIR = Path(__file__).resolve().parent.parent.parent

# Agents whose tools reach devices, and which therefore receive a brief.
BRIEF_AWARE = ("light-effect", "scene-sync", "device-control")

# The heading the orchestrator emits. Every consumer has to name it the same way:
# the specialist is matching on text in its context, so a paraphrase in one prompt
# is an instruction about a section that never appears.
BRIEF_HEADING = "Devices already identified"

# Phrases that tell the model to discover unconditionally. Each one negates the
# brief on its own.
UNCONDITIONAL = (
    "call this first, every time",
    "call `discover_devices` before anything else",
    "call `discover_devices` before composing",
    "call this before anything else",
)


def _flat(text: str) -> str:
    """Collapse runs of whitespace to single spaces.

    Necessary, not cosmetic: the heading is line-wrapped in every docstring and
    prompt that mentions it, so an exact substring match on the raw text fails on
    the newline inside "Devices already\\nidentified". The model sees the wrapped
    form and reads it fine; the test has to compare on the same basis.
    """
    return re.sub(r"\s+", " ", text)


def _tools_source(agent: str) -> str:
    return (REGISTRY_DIR / agent / "tools.py").read_text(encoding="utf-8")


def _prompt_source(agent: str) -> str:
    return (REGISTRY_DIR / agent / "system_prompt.md").read_text(encoding="utf-8")


def _discover_docstring(agent: str) -> str:
    """The docstring of `discover_devices` in this agent's tools.py.

    Parsed rather than string-searched so the assertion is about the text the model
    actually receives as the tool description, not about the file as a whole.
    """
    import ast

    tree = ast.parse(_tools_source(agent))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "discover_devices":
            return ast.get_docstring(node) or ""
    return ""


@pytest.mark.parametrize("agent", BRIEF_AWARE)
def test_the_agent_defines_discover_devices(agent):
    # If this fails the rest of the file is vacuous rather than failing.
    assert _discover_docstring(agent), f"{agent}: no discover_devices docstring"


@pytest.mark.parametrize("agent", BRIEF_AWARE)
def test_the_tool_docstring_does_not_demand_an_unconditional_call(agent):
    """The exact bug: "Call this FIRST, every time" in the tool description.

    It overrode a system prompt that said the opposite, and the only symptom was an
    optimisation that silently did nothing.
    """
    doc = _flat(_discover_docstring(agent)).lower()
    for phrase in UNCONDITIONAL:
        assert phrase not in doc, (
            f"{agent}: discover_devices' docstring says {phrase!r}, which "
            f"contradicts the delegation brief. The tool description wins over the "
            f"system prompt, so the brief is ignored and nothing reports it.")


@pytest.mark.parametrize("agent", BRIEF_AWARE)
def test_the_tool_docstring_names_the_brief(agent):
    """The model has to be told what to look for, by its actual heading."""
    doc = _flat(_discover_docstring(agent))
    assert BRIEF_HEADING in doc, (
        f"{agent}: discover_devices' docstring never mentions "
        f"{BRIEF_HEADING!r}, so the model has no reason to check for it")


@pytest.mark.parametrize("agent", BRIEF_AWARE)
def test_the_system_prompt_names_the_brief(agent):
    prompt = _flat(_prompt_source(agent))
    assert BRIEF_HEADING in prompt, (
        f"{agent}: system_prompt.md never mentions {BRIEF_HEADING!r}")


@pytest.mark.parametrize("agent", BRIEF_AWARE)
def test_the_system_prompt_does_not_demand_an_unconditional_call(agent):
    prompt = _flat(_prompt_source(agent)).lower()
    for phrase in UNCONDITIONAL:
        assert phrase not in prompt, (
            f"{agent}: system_prompt.md says {phrase!r}, which negates the brief")


@pytest.mark.parametrize("agent", BRIEF_AWARE)
def test_both_halves_still_keep_discover_as_the_fallback(agent):
    """The brief is a hint, not an inventory.

    Room and category matching is heuristic, so a specialist must be able to look
    for itself when the guess was wrong. A prompt that forbade discovery outright
    would trade a slow correct answer for a fast wrong one.
    """
    both = _flat(_discover_docstring(agent) + " " + _prompt_source(agent))
    assert re.search(r"missing from it|not in it|no such list|contains no list|"
                     r"nothing is listed|contradicts", both, re.IGNORECASE), (
        f"{agent}: neither the tool doc nor the prompt says when to call "
        f"discover_devices anyway — the brief has become an authority")


@pytest.mark.parametrize("agent", BRIEF_AWARE)
def test_both_halves_say_the_brief_carries_no_live_state(agent):
    """Otherwise the specialist reports a brightness nobody told it.

    The brief comes from the static catalog: it says what exists and what it
    accepts, never what is currently on.
    """
    both = _flat(_discover_docstring(agent) + " " + _prompt_source(agent))
    assert "query_device_state" in both, (
        f"{agent}: nothing points at query_device_state for live state")


def test_the_orchestrator_emits_exactly_this_heading():
    """The producer and every consumer must agree on one string.

    Checked against shared/device_brief.py rather than duplicated here: if the
    heading is reworded there, this test fails instead of six prompts quietly
    describing a section that no longer appears.
    """
    brief_src = _flat((REGISTRY_DIR.parent / "shared" / "device_brief.py").read_text())
    assert BRIEF_HEADING in brief_src, (
        f"shared/device_brief.py no longer emits {BRIEF_HEADING!r}; the prompts "
        f"and tool docs that reference it are now describing nothing")
