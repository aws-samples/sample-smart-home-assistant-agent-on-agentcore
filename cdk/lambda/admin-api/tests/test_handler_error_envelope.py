"""An unhandled error must come back as a CORS-bearing 500, not escape.

When a handler raises, API Gateway substitutes its own 502 — and that response has
no `Access-Control-Allow-Origin` header, because the header is set by this
Lambda's own `response()` helper. The browser then reports "blocked by CORS
policy" and never shows the real exception.

That happened: `GetGateway` stopped returning `protocolType`, the Cedar policy
rebuild raised KeyError, and the visible symptom was a CORS error on the
permissions save while the Admin Console showed the checkboxes as saved. The
misdirection is the whole point of these tests — the failure named the wrong
system, and the UI claimed success.
"""

import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index  # noqa: E402


def _event(method="PUT", resource="/users/{userId}/permissions"):
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": {"userId": "u-1"},
        "body": "{}",
        "requestContext": {"authorizer": {"claims": {
            "cognito:groups": "admin", "sub": "probe"}}},
    }


def test_an_unhandled_exception_becomes_a_500_with_cors_headers():
    with patch.object(index, "_dispatch", side_effect=KeyError("protocolType")):
        out = index.handler(_event(), None)
    assert out["statusCode"] == 500
    # The header that API Gateway's own 502 would omit.
    assert out["headers"]["Access-Control-Allow-Origin"] == "*"
    body = json.loads(out["body"])
    assert body["error"] == "KeyError"
    assert "protocolType" in body["message"]


def test_the_exception_type_and_message_survive_to_the_browser():
    """A generic "internal error" would leave the next person with the same
    misdirection this exists to remove."""
    with patch.object(index, "_dispatch",
                      side_effect=ValueError("gateway is in UPDATING")):
        out = index.handler(_event(), None)
    body = json.loads(out["body"])
    assert body["error"] == "ValueError"
    assert body["message"] == "gateway is in UPDATING"


def test_a_successful_dispatch_is_passed_through_untouched():
    sentinel = {"statusCode": 200, "headers": {}, "body": "{}"}
    with patch.object(index, "_dispatch", return_value=sentinel):
        assert index.handler(_event(), None) is sentinel


def test_the_router_is_still_reachable_as_dispatch():
    """`handler` is now a wrapper; if the router were renamed without updating it,
    every request would 500 with an AttributeError instead of routing."""
    assert callable(index._dispatch)
    out = index.handler(_event(method="GET", resource="/nope"), None)
    # Routed, not crashed: the unknown-route branch answered.
    assert out["statusCode"] == 400
    assert "Unknown route" in json.loads(out["body"])["error"]


# ---------------------------------------------------------------------------
# The specific regression
# ---------------------------------------------------------------------------

def test_protocol_type_is_only_sent_when_the_api_returns_it():
    """`protocolType` is optional on UpdateGateway and GetGateway stopped
    returning it. Reading it unconditionally is what raised KeyError."""
    import inspect

    src = inspect.getsource(index)
    assert 'protocolType=gw["protocolType"]' not in src, (
        "protocolType is read unconditionally again — GetGateway does not return "
        "it, so this raises KeyError and the browser reports a CORS error")
    assert 'if gw.get("protocolType"):' in src


def test_update_gateway_still_sends_every_required_member():
    """The fix removes an OPTIONAL member. Dropping a required one would trade a
    KeyError for a ValidationException — also a 500, also opaque."""
    src = __import__("inspect").getsource(index)
    start = src.index("update_kwargs = dict(")
    block = src[start:start + 600]
    for required in ("gatewayIdentifier", "name=", "roleArn=", "authorizerType="):
        assert required in block, f"UpdateGateway call lost {required}"
