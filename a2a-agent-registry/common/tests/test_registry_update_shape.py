"""Tests for the descriptor shape UpdateRegistryRecord actually accepts.

This exists because the same bug shipped twice.

`CreateRegistryRecord` takes descriptors bare; `UpdateRegistryRecord` wraps every
level in `optionalValue` — the union, each descriptor, and each field. Sending
Create's shape to Update fails validation, and `deploy.py` treats ANY update
failure as "delete the record and recreate it". That path succeeds, so a redeploy
reports success while minting a NEW recordId. Every user's `a2aGrants` map is
keyed by recordId, so the grants silently point at a record that no longer
exists: the orchestrator registers zero `a2a_*` tools for that agent and answers
from its own knowledge instead, with nothing in any log.

Fixed once by wrapping the OUTER level, which looked right and was not — the next
deploy recreated the record again because `a2aGrants` and `data` were still bare.
Hence a test that asserts the shape all the way down, and a second one that
validates it against the live botocore service model rather than against my
reading of it.
"""
import ast
import json
from pathlib import Path

import pytest

DEPLOY_PY = Path(__file__).resolve().parent.parent.parent / "deploy.py"


def _as_update_descriptors():
    """Load just the one function out of deploy.py.

    Extracted rather than imported: deploy.py imports boto3 and the roster at
    module scope, and this test is about a pure data transform.
    """
    tree = ast.parse(DEPLOY_PY.read_text())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_as_update_descriptors")
    module = ast.Module(
        body=[ast.ImportFrom(module="typing",
                             names=[ast.alias(name="Any")], level=0), fn],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace: dict = {}
    exec(compile(module, "<extracted>", "exec"), namespace)  # noqa: S102
    return namespace["_as_update_descriptors"]


CREATE_SHAPE = {"a2aAgentCard": {"data": '{"name":"light-effect-agent"}'}}


def test_every_level_is_wrapped():
    out = _as_update_descriptors()(CREATE_SHAPE)
    assert out == {
        "optionalValue": {
            "a2aAgentCard": {
                "optionalValue": {
                    "data": {"optionalValue": '{"name":"light-effect-agent"}'}
                }
            }
        }
    }


def test_wrapping_only_the_outer_level_is_not_enough():
    """The regression guard for the second occurrence of this bug.

    Asserted explicitly because `{"optionalValue": <create shape>}` is the
    plausible-looking wrong answer, and the way it fails — a successful-looking
    redeploy that voids every grant — gives no signal that it is wrong.
    """
    out = _as_update_descriptors()(CREATE_SHAPE)
    assert out != {"optionalValue": CREATE_SHAPE}
    card = out["optionalValue"]["a2aAgentCard"]
    assert "optionalValue" in card, "the descriptor itself must be wrapped"
    assert "optionalValue" in card["optionalValue"]["data"], \
        "the field value must be wrapped too"


def test_the_card_json_survives_unchanged():
    # The Registry validates the card against the full A2A schema, so the payload
    # has to arrive byte-identical — a transform that re-serialised it could drop
    # or reorder fields and be rejected as "does not match any supported version".
    card = json.dumps({"name": "x", "skills": [{"id": "a"}]})
    out = _as_update_descriptors()({"a2aAgentCard": {"data": card}})
    assert out["optionalValue"]["a2aAgentCard"]["optionalValue"]["data"][
        "optionalValue"] == card


def test_multiple_descriptors_are_each_wrapped():
    out = _as_update_descriptors()({
        "a2aAgentCard": {"data": "{}"},
        "agentSkillsDefinition": {"data": "{}"},
    })
    assert set(out["optionalValue"]) == {"a2aAgentCard", "agentSkillsDefinition"}
    for name in out["optionalValue"]:
        assert "optionalValue" in out["optionalValue"][name]


def test_validates_against_the_live_service_model():
    """The shape is measured, not remembered.

    botocore ships the service model, so this checks the real contract rather than
    my reading of the docs. Skipped if the Registry namespace is unavailable in the
    installed botocore, so the suite still runs offline or on an older SDK.
    """
    boto3 = pytest.importorskip("boto3")
    from botocore.exceptions import ParamValidationError, UnknownServiceError
    from botocore.validate import validate_parameters

    try:
        client = boto3.client("agent-registry-control", region_name="us-west-2")
    except UnknownServiceError:
        pytest.skip("agent-registry-control not in this botocore")

    shape = client.meta.service_model.operation_model(
        "UpdateRegistryRecord").input_shape
    payload = _as_update_descriptors()(CREATE_SHAPE)
    validate_parameters(
        {"registryId": "r", "recordId": "x", "descriptors": payload}, shape)

    # And the wrong shape must genuinely be rejected — otherwise the test above
    # proves nothing about which of the two is required.
    with pytest.raises(ParamValidationError):
        validate_parameters(
            {"registryId": "r", "recordId": "x",
             "descriptors": {"optionalValue": CREATE_SHAPE}}, shape)


def test_create_still_gets_the_bare_shape():
    """Create and Update must not be given the same payload.

    Reading deploy.py's source rather than calling it: the point is that the
    create call site passes `descriptor_payload` and the update call site passes
    the converted one, and swapping them would break in the opposite direction.
    """
    src = DEPLOY_PY.read_text()
    assert "descriptors=descriptor_payload" in src, \
        "create_registry_record must pass the BARE descriptor payload"
    assert "descriptors=update_descriptor_payload" in src, \
        "update_registry_record must pass the wrapped payload"
