"""`UpdateRegistryRecord` wraps every level in `optionalValue`. Twice bitten.

The failure is not a clean one. Any update error is treated by the deploy path as
"delete the record and recreate it"; that path succeeds, so a redeploy reports
success while minting a NEW recordId — and `a2aGrants` in every
`__a2a_permissions__` row is keyed by recordId. The user-visible symptom is a
specialist whose skills were granted yesterday having no `a2a_*` tools today, with
nothing in any log. It happened once before the outer wrap was added, and again on
the next deploy because the inner two levels were still bare.

The logic now exists in two places that cannot import each other:

  shared/agent_registry.as_update_descriptors   used by scripts/ and the Lambdas
  a2a-agent-registry/deploy.py                 runs from its own directory

so this file holds them identical and checks both against the live botocore
service model rather than against anyone's reading of the docs.
"""
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from shared import agent_registry  # noqa: E402

DEPLOY_PY = os.path.join(REPO, "a2a-agent-registry", "deploy.py")

# A SKILL create-shaped tree, three levels deep — the shape that actually broke
# `scripts/publish-builtin-skills.py` on its first idempotent re-run.
SKILL_SHAPE = {
    "agentSkillsDefinition": {
        "data": '{"name": "led-control"}',
        "dataSchemaVersion": "0.1.0",
        "additionalData": {
            "skillMd": {"data": "# led-control", "dataSchemaVersion": "0.1.0"},
        },
    },
}

AGENT_SHAPE = {"a2aAgentCard": {"data": '{"name": "home-security-agent"}'}}


def _deploy_impl():
    """`_as_update_descriptors` from deploy.py, without importing deploy.py.

    deploy.py imports boto3 and the A2A roster at module scope; exec'ing just the
    one function keeps this test from needing either.
    """
    src = open(DEPLOY_PY, encoding="utf-8").read()
    match = re.search(
        r"\ndef _as_update_descriptors\(.*?\n(?=\ndef |\n# ---)", src, re.S)
    assert match, "‑_as_update_descriptors not found in deploy.py"
    ns: dict = {"Any": object}
    exec(compile(match.group(0), DEPLOY_PY, "exec"), ns)  # noqa: S102
    return ns["_as_update_descriptors"]


def test_every_level_is_wrapped_not_just_the_outer_one():
    """Wrapping only the outer level is the fix that looks right."""
    out = agent_registry.as_update_descriptors(AGENT_SHAPE)
    assert out == {"optionalValue": {
        "a2aAgentCard": {"optionalValue": {
            "data": {"optionalValue": '{"name": "home-security-agent"}'}}}}}


def test_nested_additional_data_is_wrapped_all_the_way_down():
    """The SKILL shape nests one level deeper than the AGENT shape, which is how a
    wrap that was correct for agents was still wrong for skills."""
    out = agent_registry.as_update_descriptors(SKILL_SHAPE)
    skill_md = (out["optionalValue"]["agentSkillsDefinition"]["optionalValue"]
                ["additionalData"]["optionalValue"]["skillMd"]["optionalValue"])
    assert skill_md["data"] == {"optionalValue": "# led-control"}


def test_the_two_implementations_agree():
    """deploy.py cannot import shared/, so the logic exists twice. If they drift,
    one of the two callers silently falls back to delete-and-recreate."""
    deploy_fn = _deploy_impl()
    for shape in (AGENT_SHAPE, SKILL_SHAPE):
        assert deploy_fn(shape) == agent_registry.as_update_descriptors(shape)


def test_validates_against_the_live_service_model():
    """The shape is measured, not remembered."""
    boto3 = pytest.importorskip("boto3")
    from botocore.exceptions import ParamValidationError, UnknownServiceError
    from botocore.validate import validate_parameters

    try:
        client = boto3.client(agent_registry.REGISTRY_CLIENT, region_name="us-west-2")
    except UnknownServiceError:
        pytest.skip(f"{agent_registry.REGISTRY_CLIENT} not in this botocore")

    shape = client.meta.service_model.operation_model(
        "UpdateRegistryRecord").input_shape
    for tree in (AGENT_SHAPE, SKILL_SHAPE):
        validate_parameters(
            {"registryId": "r", "recordId": "x",
             "descriptors": agent_registry.as_update_descriptors(tree)}, shape)

        # And the outer-only wrap must genuinely be rejected, or the check above
        # proves nothing about which of the two shapes is required.
        with pytest.raises(ParamValidationError):
            validate_parameters(
                {"registryId": "r", "recordId": "x",
                 "descriptors": {"optionalValue": tree}}, shape)


def test_create_is_not_given_the_update_shape():
    """The two calls take different payloads, and swapping them breaks in the
    opposite direction — silently, because create would then store a descriptor
    tree full of `optionalValue` keys as though they were data."""
    src = open(os.path.join(REPO, "scripts", "publish-builtin-skills.py"),
               encoding="utf-8").read()
    assert "descriptors=descriptors," in src, \
        "create_registry_record must pass the BARE descriptors"
    assert "descriptors=registry_ns.as_update_descriptors(descriptors)" in src, \
        "update_registry_record must pass the wrapped descriptors"
