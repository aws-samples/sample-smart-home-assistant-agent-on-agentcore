"""The two `UpdateRegistryRecord` calls in this Lambda, checked against the model.

Both edit routes (`PUT /my-skills/{recordId}` and `PUT /my-a2a-agents/{recordId}`)
were written to pass `description` as a plain string and `descriptors` as the
Create-shaped tree, with a comment asserting that GA has "no `optionalValue`
wrapper". The service model says otherwise: on **Update** (and only on update)
`description`, `displayName` and every level of `descriptors` are wrapped, while
`recordVersion` and `name` stay bare. So both routes raised ParamValidationError
before the request ever left the Lambda — every edit was a 500.

It survived because the existing route tests mock boto3 with a MagicMock, and a
MagicMock accepts any keyword shape at all. That is the hole this file closes: it
drives the real handlers, captures the kwargs they actually built, and validates
them against the live botocore shape rather than against a hand-written
expectation. A future edit that reverts to plain values fails here.

`test_create_still_takes_the_bare_shape` is the other half. The two calls take
DIFFERENT payloads, and "wrap everything" is as wrong as "wrap nothing" — create
would then store `optionalValue` keys as though they were data, which no exception
would tell you about.
"""
import importlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_mock_table = MagicMock()
_mock_ac = MagicMock()

CARD_JSON = json.dumps({"name": "my-agent", "skills": [{"id": "do-thing"}]})
SKILL_MD = ("---\nname: my-skill\ndescription: \"a skill\"\n"
            "allowed_tools: [control_device]\n---\n\nbody text\n")


@pytest.fixture(scope="module", autouse=True)
def _install_mocks():
    # Both Lambda dirs define index.py; force this one to win.
    lambda_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    if sys.path[0] != lambda_dir:
        sys.path.insert(0, lambda_dir)
    sys.modules.pop("index", None)
    with patch("boto3.resource") as mres, patch("boto3.client") as mclient:
        mres.return_value.Table.return_value = _mock_table
        mclient.return_value = _mock_ac
        import index  # noqa: F401
        importlib.reload(index)
        yield
        sys.modules.pop("index", None)


@pytest.fixture(autouse=True)
def _reset_mocks():
    _mock_table.reset_mock()
    _mock_ac.reset_mock()
    _mock_table.get_item.return_value = {
        "Item": {"ownerSub": "alice-sub", "recordType": "a2a"}}
    yield


def _event(method, resource, record_id, body):
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": {"recordId": record_id},
        "body": json.dumps(body),
        "requestContext": {"authorizer": {"claims": {
            "sub": "alice-sub", "email": "alice@example.com"}}},
    }


def _input_shape(operation):
    """A real input shape for `operation`, or a skip.

    Loaded through botocore's own session rather than `boto3.client`, because the
    module fixture above patches `boto3.client` to a MagicMock — asking it for a
    service model returns a mock whose `type_name` is a mock, and the validator
    fails with an AttributeError that looks nothing like a shape problem.
    """
    botocore = pytest.importorskip("botocore")
    import botocore.session
    from botocore.exceptions import UnknownServiceError

    import agent_registry as registry_ns
    try:
        model = botocore.session.get_session().get_service_model(
            registry_ns.REGISTRY_CLIENT)
    except UnknownServiceError:
        pytest.skip(f"{registry_ns.REGISTRY_CLIENT} not in this botocore "
                    f"(needs botocore >= 1.43.67)")
    return model.operation_model(operation).input_shape


def _validate(kwargs):
    from botocore.validate import validate_parameters
    validate_parameters(kwargs, _input_shape("UpdateRegistryRecord"))


def test_skill_edit_builds_a_payload_the_service_accepts():
    import index
    _mock_ac.get_registry_record.return_value = {
        "recordId": "rec-1",
        "name": "my-skill",
        "description": "a skill",
        "status": "APPROVED",
        "descriptors": {"agentSkillsDefinition": {
            "data": json.dumps({"_meta": {"license": "MIT-0"}}),
            "additionalData": {"skillMd": {"data": SKILL_MD}},
        }},
    }

    resp = index.handler(_event("PUT", "/my-skills/{recordId}", "rec-1",
                                {"description": "an edited skill"}), None)
    assert resp["statusCode"] == 200, resp["body"]

    kwargs = _mock_ac.update_registry_record.call_args.kwargs
    _validate(kwargs)
    # And spell out the two shapes, so a failure says which one moved.
    assert kwargs["description"] == {"optionalValue": "an edited skill"}
    assert set(kwargs["descriptors"]) == {"optionalValue"}
    skill = kwargs["descriptors"]["optionalValue"]["agentSkillsDefinition"]
    assert set(skill) == {"optionalValue"}
    assert set(skill["optionalValue"]["data"]) == {"optionalValue"}


def test_a2a_edit_builds_a_payload_the_service_accepts():
    import index
    _mock_ac.get_registry_record.return_value = {
        "recordId": "rec-2", "name": "my-agent", "status": "APPROVED",
        "descriptors": {"a2aAgentCard": {"data": CARD_JSON}},
    }

    form = {
        "name": "my-agent",
        "description": "an edited agent",
        "endpoint": "https://example.com/a2a",
        "version": "1.0.0",
        "provider": "Test",
        "capabilities": {"streaming": True, "pushNotifications": False,
                         "stateTransitionHistory": False},
        "auth": "none",
        "tags": ["demo"],
        "skills": [{"id": "do-thing", "name": "Do thing",
                    "description": "does the thing", "examples": ["e"]}],
    }
    resp = index.handler(
        _event("PUT", "/my-a2a-agents/{recordId}", "rec-2", form), None)
    assert resp["statusCode"] == 200, resp["body"]

    kwargs = _mock_ac.update_registry_record.call_args.kwargs
    _validate(kwargs)
    assert kwargs["description"] == {"optionalValue": "an edited agent"}
    card = kwargs["descriptors"]["optionalValue"]["a2aAgentCard"]
    assert set(card["optionalValue"]["data"]) == {"optionalValue"}


def test_the_plain_shape_really_is_rejected():
    """Without this, the two tests above prove only "some shape validates"."""
    from botocore.exceptions import ParamValidationError

    with pytest.raises(ParamValidationError):
        _validate({"registryId": "r", "recordId": "x", "description": "plain"})
    with pytest.raises(ParamValidationError):
        _validate({"registryId": "r", "recordId": "x",
                   "descriptors": {"a2aAgentCard": {"data": CARD_JSON}}})


def test_create_still_takes_the_bare_shape():
    """Create and Update are not interchangeable in either direction."""
    import index
    from botocore.validate import validate_parameters

    create_shape = _input_shape("CreateRegistryRecord")

    _mock_ac.create_registry_record.return_value = {
        "recordArn": "arn:aws:agent-registry:us-west-2:111:registry/r/record/rec-9"}
    _mock_ac.get_registry_record.return_value = {"status": "DRAFT"}
    resp = index.handler({
        "httpMethod": "POST", "resource": "/my-skills",
        "body": json.dumps({"skillName": "my-skill", "description": "a skill",
                            "instructions": "body text"}),
        "requestContext": {"authorizer": {"claims": {
            "sub": "alice-sub", "email": "alice@example.com"}}},
    }, None)
    assert resp["statusCode"] == 201, resp["body"]

    kwargs = _mock_ac.create_registry_record.call_args.kwargs
    validate_parameters(kwargs, create_shape)
    assert "optionalValue" not in json.dumps(kwargs["descriptors"])
    assert isinstance(kwargs["description"], str)
