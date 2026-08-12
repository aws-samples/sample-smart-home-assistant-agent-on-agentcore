"""Tests for deriving A2A grants from the caller's token claim.

Replaces test_a2a_perms_load.py, which tested a DynamoDB read. Grants now come from
the `cognito:groups` claim on the user's own token — the same claim each sub-agent's
Runtime authorizer checks — so what the model is offered and what the platform will
allow cannot disagree.

What is pinned here is mostly the fail-closed direction: a token that is absent,
malformed, or carries no groups must yield NO tools, never all of them.
"""
import base64
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.dirname(HERE)
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)


def _token(claims: dict) -> str:
    """A JWT-shaped string. Unsigned on purpose — see the note below."""
    def seg(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


@pytest.fixture(scope="module")
def agent_mod():
    """`agent/a2a_grants.py`.

    Deliberately NOT `agent.py`: importing the orchestrator pulls in strands,
    playwright and browser-use, which cost enough memory to get the whole test
    session OOM-killed when it runs alongside the other suites. The claim parsing
    lives in its own module precisely so this test is cheap.
    """
    import a2a_grants
    return a2a_grants


def test_groups_become_grants_keyed_by_agent_name(agent_mod):
    tok = _token({"sub": "u1", "cognito:groups": [
        "a2a-knowledge-qa-agent.answer_from_docs",
        "a2a-knowledge-qa-agent.troubleshoot_from_docs",
        "a2a-light-effect-agent.compose_effect",
        "admin",
    ]})
    assert agent_mod.grants_from_user_token(f"Bearer {tok}") == {
        "knowledge-qa-agent": ["answer_from_docs", "troubleshoot_from_docs"],
        "light-effect-agent": ["compose_effect"],
    }


def test_no_token_is_no_grants(agent_mod):
    assert agent_mod.grants_from_user_token(None) == {}
    assert agent_mod.grants_from_user_token("") == {}


def test_a_token_with_no_groups_is_no_grants(agent_mod):
    """Not "all grants". This is the inversion that would silently open everything."""
    assert agent_mod.grants_from_user_token(f"Bearer {_token({'sub': 'u1'})}") == {}
    assert agent_mod.grants_from_user_token(
        f"Bearer {_token({'sub': 'u1', 'cognito:groups': []})}") == {}


def test_only_unrelated_groups_is_no_grants(agent_mod):
    tok = _token({"sub": "u1", "cognito:groups": ["admin", "some-team"]})
    assert agent_mod.grants_from_user_token(f"Bearer {tok}") == {}


def test_a_malformed_token_is_no_grants_rather_than_an_exception(agent_mod):
    """A turn must not die because a header was garbled; it should just offer no
    specialists, which is the same as an ungranted user."""
    for bad in ("not-a-jwt", "a.b", "Bearer ...", "Bearer x.y.z"):
        assert agent_mod.grants_from_user_token(bad) == {}


def test_bearer_prefix_is_optional(agent_mod):
    tok = _token({"sub": "u1", "cognito:groups": ["a2a-a-agent.s1"]})
    assert agent_mod.grants_from_user_token(tok) == {"a-agent": ["s1"]}
    assert agent_mod.grants_from_user_token(f"bearer {tok}") == {"a-agent": ["s1"]}


def test_a_single_group_claim_arriving_unwrapped_still_parses(agent_mod):
    """Some IdP configurations emit a lone group as a bare string, not a list."""
    tok = _token({"sub": "u1", "cognito:groups": "a2a-a-agent.s1"})
    assert agent_mod.grants_from_user_token(tok) == {"a-agent": ["s1"]}


def test_claims_are_read_without_verification_by_design(agent_mod):
    """The signature here is `none` and it still parses, which is intended.

    Nothing security-relevant is decided from these claims: they choose which tools
    to *offer*, and the sub-agent's authorizer independently validates the same token
    and checks the same claim. A forged claim buys a tool that is then refused at the
    door. This test exists so that the absence of verification is a recorded decision
    rather than something a later reader assumes is an oversight.
    """
    forged = _token({"sub": "attacker", "cognito:groups": ["a2a-device-control-agent.s1"]})
    assert agent_mod.grants_from_user_token(forged) == {"device-control-agent": ["s1"]}
