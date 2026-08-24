"""The approval gate: a non-conformant agent cannot be published by clicking Approve.

Why a gate rather than the report it already was. Approving an AGENT record is what
makes it discoverable — console, orchestrator tools, delegation prompt. Whether it is
CALLABLE is decided by its own Runtime authorizer, which lives with the team that
deployed it. As a report, the correction loop ran through whoever clicked Approve; as a
gate, it runs through the only team that can fix it.

The boundary tested here is which severities block, and it is not "any finding":

  OPEN    blocks — authorization is not happening, anyone in the pool gets in
  CLOSED  blocks — nobody gets in; the model offers the tool and then apologises
  INFO    passes — "could not read the runtime" and "still enumerates skill groups"
                   are both real states an admin must be able to approve

A gate that blocked on INFO would refuse every agent in another account and every
runtime not yet redeployed for the stable-group migration. That is how a gate gets
turned off.
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POOL = "us-west-2_HwYYt6qLz"
CLIENT = "7031k7ctqes8mfe5jrf7kljttf"
DISCOVERY = (f"https://cognito-idp.us-west-2.amazonaws.com/{POOL}"
             "/.well-known/openid-configuration")
ARN = "arn:aws:bedrock-agentcore:us-west-2:123:runtime/sha2aair-ddd"
URL = ("https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
       + ARN.replace(":", "%3A").replace("/", "%2F") + "/invocations")

CARD = {"name": "third-party-agent", "url": URL,
        "skills": [{"id": "do_a_thing"}, {"id": "do_another"}]}
DOOR = ["a2a-third-party-agent"]
SKILL_GROUPS = ["a2a-third-party-agent.do_a_thing",
                "a2a-third-party-agent.do_another"]


def _authorizer(groups, discovery=DISCOVERY, audience=None, claims=True):
    jwt = {"discoveryUrl": discovery,
           "allowedAudience": [CLIENT] if audience is None else audience}
    if claims:
        jwt["customClaims"] = [{
            "inboundTokenClaimName": "cognito:groups",
            "inboundTokenClaimValueType": "STRING_ARRAY",
            "authorizingClaimMatchValue": {
                "claimMatchValue": {"matchValueStringList": groups},
                "claimMatchOperator": "CONTAINS_ANY"},
        }]
    return {"customJWTAuthorizer": jwt}


@pytest.fixture(scope="module")
def idx():
    """`index` reloaded ONCE for this file.

    Module-scoped deliberately: `index.py` is several thousand lines and pulls in
    boto3, and reloading it per test grew the interpreter until pytest was OOM-killed
    while writing its own summary — all tests passing, exit 137, no report. Every test
    here stubs its own seams with `patch.object`, so nothing leaks between them.
    """
    os.environ["COGNITO_USER_POOL_ID"] = POOL
    os.environ["COGNITO_APP_CLIENT_ID"] = CLIENT
    os.environ["REGISTRY_ID"] = "Zuy3YNKrPQ5uwE9t"
    import importlib

    import index
    importlib.reload(index)
    return index


UNREADABLE = object()   # sentinel: GetAgentRuntime itself fails


def _review(idx, authorizer, *, force=False, decision="approve", card=CARD):
    """Drive `review_registry_record` with the AWS seams stubbed.

    `authorizer=UNREADABLE` makes GetAgentRuntime raise, which is the real
    "runtime in another account" case. Passing `{}` would instead mean "read it, and it
    has no authorizer at all" — a different, OPEN, finding.
    """
    detail = {"status": "PENDING_APPROVAL", "_card": card}
    event = {
        "body": json.dumps({"recordId": "r1", "decision": decision,
                            "reason": "looks good"}),
        "queryStringParameters": {"force": "true"} if force else {},
    }
    control = MagicMock()
    if authorizer is UNREADABLE:
        control.get_agent_runtime.side_effect = RuntimeError("AccessDeniedException")
    else:
        control.get_agent_runtime.return_value = {
            "authorizerConfiguration": authorizer}

    # A record with no AgentCard descriptor returns "" from the real helper, which is
    # how a SKILL record is told apart from an AGENT one. `json.dumps({})` would be
    # the truthy string "{}" and every skill would be gated.
    def _card_of(d):
        c = d.get("_card") or {}
        return json.dumps(c) if c else ""

    # The inline revocation sweep after a reject/deprecate is `test_a2a_sweep`'s
    # subject, not this file's. Stubbed for isolation AND because leaving it live on a
    # MagicMock registry client does not terminate: `_fetch_a2a_records` pages
    # `while True` on `resp.get("nextToken")`, a MagicMock is always truthy, and the
    # loop grows `mock_calls` until the interpreter is OOM-killed — which presents as
    # every test passing and then exit 137 with no report at all.
    stub_sweep = patch.object(idx, "sweep_a2a_revocations",
                              return_value={"body": json.dumps({"swept": True})})
    with patch.object(idx, "registry_control") as reg, \
            patch.object(idx, "agentcore_control", control), \
            patch.object(idx.registry_ns, "read_agent_card", side_effect=_card_of), \
            patch.object(idx.registry_ns, "approve_record",
                         return_value="APPROVED") as approve, \
            patch.object(idx.registry_ns, "set_record_status"), \
            stub_sweep, \
            patch.object(idx, "_caller_identity", return_value="admin@example.com"):
        reg.get_registry_record.return_value = detail
        result = idx.review_registry_record(event)
    return result, json.loads(result["body"]), approve


# ---------------------------------------------------------------------------
# Passes
# ---------------------------------------------------------------------------

def test_a_conformant_agent_is_approved(idx):
    result, body, approve = _review(idx, _authorizer(DOOR))
    assert result["statusCode"] == 200
    assert body["status"] == "APPROVED"
    assert body["conformance"]["conformant"] is True
    approve.assert_called_once()


def test_a_pre_migration_authorizer_still_approves(idx):
    """It enumerates skill groups instead of the stable door group. That WORKS today —
    CONTAINS_ANY passes on any one of them — it is merely still coupled. Blocking here
    would refuse every runtime not yet redeployed for the migration."""
    result, body, approve = _review(idx, _authorizer(SKILL_GROUPS))
    assert result["statusCode"] == 200
    assert body["conformance"]["severity"] == "info"
    assert body["conformance"]["blocking"] is False
    assert {f["code"] for f in body["conformance"]["findings"]} == \
        {"claim-skill-groups-only"}
    approve.assert_called_once()


def test_an_unreadable_runtime_still_approves(idx):
    """Someone else's account. Not knowing is not evidence of a problem, and a gate
    that made the control plane's availability its own would be the outage."""
    result, body, approve = _review(idx, UNREADABLE)
    assert result["statusCode"] == 200
    assert body["conformance"]["severity"] == "info"
    assert "GetAgentRuntime failed" in body["conformance"]["resolvedVia"]
    approve.assert_called_once()


def test_a_runtime_that_reads_back_with_NO_authorizer_is_refused(idx):
    """The distinction the sentinel above exists for: an empty authorizer config is
    not "could not check", it is "there is no door"."""
    result, body, approve = _review(idx, {})
    assert result["statusCode"] == 409
    assert body["conformance"]["severity"] == "open"
    assert {f["code"] for f in body["conformance"]["findings"]} == \
        {"no-jwt-authorizer"}
    approve.assert_not_called()


def test_a_skill_record_is_not_gated(idx):
    """A SKILL record has no runtime, so the gate would report `runtime-unreadable`
    on every skill approval — noise that teaches people to ignore the field."""
    result, body, approve = _review(idx, None, card={})
    assert result["statusCode"] == 200
    assert body["conformance"] is None
    approve.assert_called_once()


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

def test_an_agent_with_no_claim_check_is_refused(idx):
    """The dangerous one: every authenticated user of the pool reaches every skill,
    with no error anywhere. Approving it would publish an unprotected agent."""
    result, body, approve = _review(idx, _authorizer([], claims=False))
    assert result["statusCode"] == 409
    assert body["conformance"]["severity"] == "open"
    assert {f["code"] for f in body["conformance"]["findings"]} == {"no-claim-check"}
    approve.assert_not_called()


def test_an_agent_pointed_at_the_wrong_pool_is_refused(idx):
    """Granted users get a 401 while the orchestrator keeps offering the tool."""
    result, body, approve = _review(
        idx, _authorizer(DOOR, discovery="https://example.invalid/oidc"))
    assert result["statusCode"] == 409
    assert body["conformance"]["severity"] == "closed"
    approve.assert_not_called()


def test_a_foreign_agents_group_in_the_authorizer_is_refused(idx):
    result, body, approve = _review(
        idx, _authorizer(DOOR + ["a2a-home-security-agent.arm_system"]))
    assert result["statusCode"] == 409
    assert body["conformance"]["severity"] == "open"
    approve.assert_not_called()


def test_the_refusal_names_who_can_fix_it(idx):
    """The whole point of the gate is that the loop runs through the agent's own
    team, so the message has to say so and name their command."""
    _result, body, _approve = _review(idx, _authorizer([], claims=False))
    assert "a2a-authorizer-contract.py" in body["hint"]
    assert "force=true" in body["hint"]


# ---------------------------------------------------------------------------
# The override
# ---------------------------------------------------------------------------

def test_force_approves_and_records_the_override_in_the_status_reason(idx):
    """A platform admin must be able to approve something the check cannot resolve.
    The override goes in the RECORD's statusReason, not only in a Lambda log that
    ages out."""
    result, body, approve = _review(idx, _authorizer([], claims=False), force=True)
    assert result["statusCode"] == 200
    assert body["status"] == "APPROVED"
    stamped = approve.call_args.kwargs["reason"]
    assert "conformance override: open" in stamped
    assert "admin@example.com" in stamped


def test_force_is_a_no_op_for_a_conformant_agent(idx):
    """It must not become a habit that silences a real finding later."""
    _result, body, approve = _review(idx, _authorizer(DOOR), force=True)
    assert "override" not in approve.call_args.kwargs["reason"]
    assert body["conformance"]["conformant"] is True


# ---------------------------------------------------------------------------
# The gate is only on approval
# ---------------------------------------------------------------------------

def test_rejecting_a_non_conformant_agent_is_not_gated(idx):
    """Refusing to let someone reject a broken agent would be absurd."""
    result, _body, _approve = _review(
        idx, _authorizer([], claims=False), decision="reject")
    assert result["statusCode"] == 200
