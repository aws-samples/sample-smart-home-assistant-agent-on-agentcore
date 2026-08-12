"""Tests for the structured-output mode (spec 5 S6).

This product's end users are largely developers, and a prose reply is not
scriptable — a device state arrives as a sentence about brightness rather than a
number to assert on. `responseFormat: "json"` asks the agent for JSON.

Prompt-level rather than constrained decoding, deliberately: the reply comes from
the same turn with the same tools, so a JSON request routes and delegates exactly
as its prose equivalent does. One behaviour to reason about instead of two.

The invariants that matter are the boring ones — it must be off by default, it must
not disturb the cached prefix, and it must sit last so it wins on formatting
against an admin's governed prompt that may ask for prose.
"""
import importlib.util
import json
import os

import pytest

_AGENT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent.py")


@pytest.fixture(scope="module")
def agent_mod():
    spec = importlib.util.spec_from_file_location("agent_script", _AGENT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Ctx:
    session_id = "s" * 40
    request_headers: dict = {}


def _patched(agent_mod, monkeypatch):
    """Capture what handle_invocation passes to invoke_agent."""
    seen = {}

    def _fake(prompt, session_id=None, actor_id=None, auth_header=None,
              headers=None, on_event=None, json_output=False):
        seen["json_output"] = json_output
        return "{}"

    monkeypatch.setattr(agent_mod, "invoke_agent", _fake)
    monkeypatch.setattr(agent_mod, "_record_session", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "_extract_user_auth", lambda ctx: None)
    return seen


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------

def test_json_mode_is_off_unless_asked(agent_mod, monkeypatch):
    """The default must stay prose: every chatbot user goes through this path."""
    seen = _patched(agent_mod, monkeypatch)
    out = agent_mod.handle_invocation({"prompt": "hi", "userId": "u@e.com"}, _Ctx())
    assert seen["json_output"] is False
    assert "responseFormat" not in out


def test_response_format_json_turns_it_on(agent_mod, monkeypatch):
    seen = _patched(agent_mod, monkeypatch)
    out = agent_mod.handle_invocation(
        {"prompt": "hi", "userId": "u@e.com", "responseFormat": "json"}, _Ctx())
    assert seen["json_output"] is True
    # Echoed back so a script can tell a JSON reply from a prose one without
    # re-reading its own request.
    assert out["responseFormat"] == "json"


def test_the_flag_is_case_insensitive(agent_mod, monkeypatch):
    seen = _patched(agent_mod, monkeypatch)
    agent_mod.handle_invocation(
        {"prompt": "hi", "userId": "u@e.com", "responseFormat": "JSON"}, _Ctx())
    assert seen["json_output"] is True


def test_an_unknown_format_is_ignored_rather_than_rejected(agent_mod, monkeypatch):
    """A caller asking for "xml" gets prose, not a 400.

    The reply is still useful, which beats failing the turn over a formatting
    preference we do not implement.
    """
    seen = _patched(agent_mod, monkeypatch)
    agent_mod.handle_invocation(
        {"prompt": "hi", "userId": "u@e.com", "responseFormat": "xml"}, _Ctx())
    assert seen["json_output"] is False


def test_json_mode_composes_with_streaming(agent_mod, monkeypatch):
    """A script may want both: progress to watch, JSON to parse."""
    seen = {}

    def _fake(prompt, session_id=None, actor_id=None, auth_header=None,
              headers=None, on_event=None, json_output=False):
        seen["json_output"] = json_output
        return '{"ok": true}'

    monkeypatch.setattr(agent_mod, "invoke_agent", _fake)
    monkeypatch.setattr(agent_mod, "_record_session", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "_extract_user_auth", lambda ctx: None)

    frames = list(agent_mod.handle_invocation(
        {"prompt": "hi", "userId": "u@e.com", "stream": True,
         "responseFormat": "json"}, _Ctx()))
    assert seen["json_output"] is True
    assert json.loads(frames[-1])["response"] == '{"ok": true}'


# ---------------------------------------------------------------------------
# The rules themselves
# ---------------------------------------------------------------------------

def test_the_rules_forbid_the_common_failure_modes(agent_mod):
    """Fences and preambles break `JSON.parse` as thoroughly as malformed JSON.

    They are also what a chat-tuned model reaches for by default, so the rules
    name them explicitly rather than only asking for "JSON".
    """
    rules = agent_mod.JSON_OUTPUT_RULES
    assert "```" in rules, "the rules must name fenced blocks as forbidden"
    assert "Here is the JSON" in rules
    assert "first character" in rules


def test_the_rules_keep_errors_in_json_too(agent_mod):
    # A caller that can parse the happy path but gets prose on failure has to
    # handle two formats, which is the thing this mode exists to avoid.
    assert '{"error"' in agent_mod.JSON_OUTPUT_RULES


def test_the_rules_do_not_change_routing(agent_mod):
    """Format is orthogonal to delegation.

    Without this, a JSON request tends to be answered directly — the model treats
    "reply with JSON" as "reply now" and skips the specialist.
    """
    rules = agent_mod.JSON_OUTPUT_RULES
    assert "Route, delegate and call tools exactly" in rules


def test_the_rules_are_appended_last(agent_mod):
    """Ordering is load-bearing twice over.

    Formatting: the instruction nearest the end wins a direct conflict, and an
    admin's governed prompt may well ask for prose.

    Caching (S3): the stable prefix — system prompt, routing table, skills, tools —
    must stay byte-identical for a cache hit, so anything per-request goes after it.
    """
    src = open(_AGENT_PY, encoding="utf-8").read()
    a2a_at = src.index('+ "\\n\\n" + delegation')
    json_at = src.index('+= "\\n\\n" + JSON_OUTPUT_RULES')
    assert json_at > a2a_at, "JSON rules must be appended after the routing rules"


# ---------------------------------------------------------------------------
# The fence the prompt cannot reliably prevent
# ---------------------------------------------------------------------------

def test_a_fenced_reply_is_unwrapped(agent_mod):
    """Measured on the live runtime, on a DELEGATED turn:

        ```json
        {"source": "a2a_home_security_agent_risk_assessment", ...}
        ```

    Unsurprising — after summarising a specialist's prose the model is deep in
    chat-formatting mode, and a fence is what that does with JSON. Instructing
    harder is not the fix; the same lesson as the `⟦A2A:…⟧` marker. If a property
    must hold for every reply, the harness enforces it.
    """
    fenced = '```json\n{"deviceId": "bedroom-light-1", "power": false}\n```'
    out = agent_mod.unfence_json(fenced)
    assert json.loads(out)["deviceId"] == "bedroom-light-1"


def test_a_bare_fence_without_a_language_is_unwrapped_too(agent_mod):
    assert json.loads(agent_mod.unfence_json('```\n{"a": 1}\n```'))["a"] == 1


def test_unfenced_json_is_untouched(agent_mod):
    text = '{"a": 1}'
    assert agent_mod.unfence_json(text) == text


def test_prose_containing_a_fence_is_not_mangled(agent_mod):
    """Only a reply that is ENTIRELY one fenced block is unwrapped.

    A reply with a fence in the middle is not a JSON-mode reply at all, and
    reaching into it would corrupt content this cannot understand. Passing it
    through lets the caller reject it, which is the honest outcome.
    """
    text = 'Here is a snippet:\n```json\n{"a": 1}\n```\nand some more prose.'
    assert agent_mod.unfence_json(text) == text


def test_unfence_is_only_applied_in_json_mode(agent_mod):
    """A prose reply that happens to contain a fenced block must keep it.

    The chatbot renders markdown, so stripping fences from a normal reply would
    turn a code sample into unformatted text.
    """
    src = open(_AGENT_PY, encoding="utf-8").read()
    assert "_post = unfence_json if json_output else (lambda t: t)" in src


def test_unfence_handles_empty_and_none_safely(agent_mod):
    assert agent_mod.unfence_json("") == ""
    assert agent_mod.unfence_json(None) is None
