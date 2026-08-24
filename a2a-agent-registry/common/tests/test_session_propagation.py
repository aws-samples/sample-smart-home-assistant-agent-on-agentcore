"""Tests for reading the orchestrator's session id off an inbound A2A request.

Why this is worth its own file: the value decides which memory namespace the
specialist reads and which session id its logs name, and every failure mode here
is silent. A dropped id looks like "the specialist never remembers the
conversation"; an id read from the wrong slot looks the same.

The two channels and their precedence live in `common/a2a_session.py` (a copy of
shared/a2a_session.py). This file tests the plumbing that pulls them out of an
a2a RequestContext, which is the part that depends on the SDK's shape.
"""
from unittest.mock import MagicMock

from common import a2a_session, server

SESSION = "user-session-88c1a3e0-b041-4c2a-9f31-1755000000000"
HEADER = a2a_session.RUNTIME_SESSION_ID_HEADER


def _context(headers: dict | None = None, message_metadata=None,
             params_metadata=None):
    """A stand-in for an a2a RequestContext.

    Mirrors the real shape: headers arrive via call_context.state['headers'], the
    orchestrator's metadata rides on `context.message.metadata`, and
    `context.metadata` is the separate *params* slot.
    """
    ctx = MagicMock()
    ctx.call_context = MagicMock()
    ctx.call_context.state = {"headers": dict(headers or {})}
    ctx.message = MagicMock()
    ctx.message.metadata = message_metadata
    ctx.metadata = params_metadata
    return ctx


def test_the_platform_header_is_read():
    # Kept working for a hop that might surface it under an allowlistable name;
    # never exercised on the direct runtime path. See common/a2a_session.py.
    ctx = _context(headers={HEADER: SESSION})
    assert server._orchestrator_session_id(ctx, server._headers_from_context(ctx)) \
        == SESSION


def test_the_header_is_read_case_insensitively():
    """HTTP headers are case-insensitive and proxies do re-case them."""
    ctx = _context(headers={HEADER.lower(): SESSION})
    assert server._orchestrator_session_id(ctx, server._headers_from_context(ctx)) \
        == SESSION


def test_message_metadata_is_used_when_the_header_is_absent():
    """This is the production path, not a fallback.

    AgentCore's `requestHeaderAllowlist` cannot admit an `x-amzn-` header, so the
    platform's session header never reaches a container. Metadata is the only
    channel the sub-agent's own code can read.
    """
    ctx = _context(message_metadata={a2a_session.SESSION_ID_METADATA_KEY: SESSION})
    assert server._orchestrator_session_id(ctx, server._headers_from_context(ctx)) \
        == SESSION


def test_params_metadata_is_a_secondary_source():
    ctx = _context(params_metadata={a2a_session.SESSION_ID_METADATA_KEY: SESSION})
    assert server._orchestrator_session_id(ctx, server._headers_from_context(ctx)) \
        == SESSION


def test_no_propagation_is_a_supported_state():
    """Every sub-agent answered before this existed and must still answer."""
    ctx = _context()
    assert server._orchestrator_session_id(ctx, server._headers_from_context(ctx)) \
        == ""


def test_an_unusable_value_is_refused_rather_than_interpolated():
    """The id is caller-controlled input that lands in a memory namespace path.

    Cross-user reads are impossible regardless — the namespace's actor segment
    comes from the verified token — but a value that reshapes the path addresses a
    namespace nobody intended.
    """
    ctx = _context(headers={HEADER: "../../summaries/someone-else/x"})
    assert server._orchestrator_session_id(ctx, server._headers_from_context(ctx)) \
        == ""


def test_a_context_without_a_message_does_not_raise():
    ctx = MagicMock()
    ctx.call_context = None
    ctx.message = None
    ctx.metadata = None
    assert server._orchestrator_session_id(ctx, server._headers_from_context(ctx)) \
        == ""
