"""Tests for the A2A session-id propagation convention.

Two failure modes are asserted rather than assumed, because neither raises:

  - Sending an id AgentCore rejects (<33 chars) turns a healthy delegation into a
    400. A warmup invocation's session id is the literal "default", so this is a
    real input, not a hypothetical one.
  - Reading an id the client never sent — the sub-agent's own session id — would
    address an empty memory namespace on every delegated turn and cost a call for
    it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import a2a_session  # noqa: E402


ORCHESTRATOR_ID = "user-session-88c1a3e0-b041-4c2a-9f31-1755000000000"


def test_the_orchestrators_own_shape_is_usable():
    # `user-session-{sub}-{epoch_ms}`, which is what the chatbot generates.
    assert a2a_session.usable_session_id(ORCHESTRATOR_ID) == ORCHESTRATOR_ID


def test_a_bare_uuid_pair_is_usable():
    # scripts/measure-baseline.py concatenates two hex UUIDs to clear the minimum.
    sid = "0123456789abcdef0123456789abcdef0123456789abcdef"
    assert a2a_session.usable_session_id(sid) == sid


def test_the_warmup_session_id_is_refused():
    """AgentCore: "Member must have length greater than or equal to 33"."""
    assert a2a_session.usable_session_id("default") == ""


def test_something_of_legal_length_but_unsafe_shape_is_refused():
    # Long enough, but it would reshape the memory namespace it is interpolated
    # into. The actor segment comes from the verified token, so this cannot reach
    # another user — it is refused because a namespace nobody intended is still
    # nobody's namespace.
    assert a2a_session.usable_session_id("../../summaries/someone-else/aaaaaaaaaa") == ""
    assert a2a_session.usable_session_id("has spaces " + "a" * 40) == ""


def test_too_long_is_refused():
    assert a2a_session.usable_session_id("a" * 129) == ""


def test_non_strings_and_absent_values_are_refused():
    for value in (None, 12345, [ORCHESTRATOR_ID], {}, ""):
        assert a2a_session.usable_session_id(value) == ""


def test_whitespace_is_stripped_rather_than_rejected():
    assert a2a_session.usable_session_id(f"  {ORCHESTRATOR_ID}\n") == ORCHESTRATOR_ID


# ---------------------------------------------------------------------------
# Inbound: which channel wins
# ---------------------------------------------------------------------------

HEADER = a2a_session.RUNTIME_SESSION_ID_HEADER.lower()
META = a2a_session.SESSION_ID_METADATA_KEY


def test_the_header_wins_when_both_arrive():
    """Prefer the channel the platform itself adopted.

    It does not arrive on the direct runtime path, so this is about a future hop
    that surfaces it under a name the allowlist admits: that value is by definition
    the one the spans were stamped with, and the log line should agree with the
    spans rather than with the body.
    """
    other = "user-session-ffffffff-0000-0000-0000-000000000000"
    assert a2a_session.session_id_from(
        {HEADER: ORCHESTRATOR_ID}, {META: other}) == ORCHESTRATOR_ID


def test_metadata_is_used_when_no_header_arrives():
    # The normal case, not an edge case: AgentCore's header allowlist cannot admit
    # an `x-amzn-` header, so a container never receives the session header.
    assert a2a_session.session_id_from({}, {META: ORCHESTRATOR_ID}) == ORCHESTRATOR_ID


def test_an_unusable_header_falls_through_to_metadata():
    assert a2a_session.session_id_from(
        {HEADER: "default"}, {META: ORCHESTRATOR_ID}) == ORCHESTRATOR_ID


def test_neither_channel_means_no_session():
    assert a2a_session.session_id_from({}, {}) == ""
    assert a2a_session.session_id_from(None, None) == ""


def test_a_malformed_source_is_treated_as_absent():
    """No propagation must degrade, never raise: every sub-agent worked without it."""
    assert a2a_session.session_id_from("not-a-mapping", None) == ""
    assert a2a_session.session_id_from(None, [("tuples", "not a dict")]) == ""
