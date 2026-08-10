"""The admin Lambda's copies of the shipped prompts must match the real ones.

The Lambda is packaged from `cdk/lambda/admin-api/` alone, so it cannot read the
agent image's `SYSTEM_PROMPT` or a sub-agent's `system_prompt.md` at runtime. It
keeps mirrors, and the Prompt editor renders them as "Default" — the text an
admin is told they are overriding, and the text "Revert to Default" writes back.

A stale mirror is therefore worse than a missing one: reverting would install a
prompt the agent has never run, and the editor would misreport what an override
changes. Nothing else catches it — the mirror is a valid Python string whatever it
says, and the agent never reads it.

The text/voice mirror was maintained by a comment asking future editors to keep it
in sync. This is that comment, enforced.
"""

import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDA_DIR = os.path.dirname(HERE)
# .../cdk/lambda/admin-api/tests -> repo root is four levels up. Asserted rather
# than trusted: an off-by-one here yields a path that simply does not exist, and
# the parametrized checks would then silently cover nothing.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(LAMBDA_DIR)))
A2A = os.path.join(ROOT, "a2a-agent-registry")
assert os.path.isdir(A2A), f"expected the A2A tree at {A2A}"

sys.path.insert(0, LAMBDA_DIR)
# `common` is a package under a2a-agent-registry/, imported the way the deployed
# container does. Inserted at module scope, not inside a helper: the roster is
# read by a parametrize decorator, which runs at collection time.
sys.path.insert(0, A2A)

import a2a_prompt_defaults  # noqa: E402
import agent_prompt_defaults  # noqa: E402
from common.agents import AGENTS  # noqa: E402


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _roster():
    return AGENTS


# ---------------------------------------------------------------------------
# The A2A sub-agents — generated mirror
# ---------------------------------------------------------------------------

def test_every_approved_sub_agent_has_a_default():
    """A missing entry makes the editor claim the agent ships no prompt, when in
    fact it ships one this Lambda simply cannot see."""
    missing = [long for long, _slug in _roster().values()
               if long not in a2a_prompt_defaults.A2A_DEFAULTS]
    assert not missing, (
        f"no default mirrored for {missing} — run "
        f"./venv/bin/python scripts/gen-prompt-defaults.py")


def test_no_default_is_mirrored_for_an_agent_that_no_longer_exists():
    """A removed agent leaving its prompt behind means the generator was not
    re-run, so the remaining entries are suspect too."""
    known = {long for long, _slug in _roster().values()}
    extra = set(a2a_prompt_defaults.A2A_DEFAULTS) - known
    assert not extra, f"mirrored default for unknown agent(s): {sorted(extra)}"


@pytest.mark.parametrize("directory", sorted(_roster()))
def test_the_mirror_matches_the_file_byte_for_byte(directory):
    long_name, _slug = _roster()[directory]
    on_disk = _read(os.path.join(A2A, directory, "system_prompt.md"))
    mirrored = a2a_prompt_defaults.A2A_DEFAULTS[long_name]
    assert mirrored == on_disk, (
        f"{directory}/system_prompt.md changed without regenerating the mirror — "
        f"run ./venv/bin/python scripts/gen-prompt-defaults.py")


def test_the_generated_file_says_it_is_generated():
    """Otherwise the next person edits it by hand and the generator overwrites
    them."""
    body = _read(os.path.join(LAMBDA_DIR, "a2a_prompt_defaults.py"))
    assert "GENERATED" in body
    assert "gen-prompt-defaults.py" in body


# ---------------------------------------------------------------------------
# The text and voice runtimes — hand-maintained mirror
# ---------------------------------------------------------------------------

def _module_constant(path, name):
    """Read a module-level string constant without importing the module.

    `agent/agent.py` imports strands, boto3 and the AgentCore SDK at module
    scope; this test only needs one literal out of it.
    """
    import ast

    tree = ast.parse(_read(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path}")


def test_the_text_prompt_mirror_matches_the_agent():
    live = _module_constant(os.path.join(ROOT, "agent", "agent.py"), "SYSTEM_PROMPT")
    assert agent_prompt_defaults.DEFAULT_TEXT_PROMPT == live, (
        "agent/agent.py SYSTEM_PROMPT changed without updating "
        "cdk/lambda/admin-api/agent_prompt_defaults.py — the Prompt editor would "
        "show, and 'Revert to Default' would install, a prompt the agent never ran")


def test_the_voice_prompt_mirror_matches_the_voice_session():
    live = _module_constant(
        os.path.join(ROOT, "agent", "voice_session.py"), "VOICE_SYSTEM_PROMPT")
    assert agent_prompt_defaults.DEFAULT_VOICE_PROMPT == live, (
        "agent/voice_session.py VOICE_SYSTEM_PROMPT changed without updating "
        "cdk/lambda/admin-api/agent_prompt_defaults.py")


def test_the_two_mirrors_do_not_overlap():
    """`builtinDefault` resolves as `PROMPT_DEFAULTS.get(t) or A2A_DEFAULTS.get(t)`,
    so a name in both would silently take the built-in one."""
    both = set(agent_prompt_defaults.DEFAULTS) & set(a2a_prompt_defaults.A2A_DEFAULTS)
    assert not both, f"agent type in both mirrors: {sorted(both)}"


def test_no_mirrored_default_is_empty():
    """An empty default reads in the UI as "this agent ships no prompt"."""
    everything = dict(agent_prompt_defaults.DEFAULTS)
    everything.update(a2a_prompt_defaults.A2A_DEFAULTS)
    blank = [k for k, v in everything.items() if not (v or "").strip()]
    assert not blank, f"empty default(s): {blank}"
