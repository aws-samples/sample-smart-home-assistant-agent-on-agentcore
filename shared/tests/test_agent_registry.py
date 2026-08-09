"""Tests for the AWS Agent Registry GA helper.

Every constraint asserted here was found by exercising the live GA service, not by
reading the docs — several of them appear in neither the documentation nor the
botocore model, and each one produces an error message that points somewhere else:

  - CreateRegistryRecord returns `recordArn`; preview returned `recordId`. Reading
    the old key gives None and stores it, so the record looks created but
    unfindable.
  - `skillMd` must start with `---` frontmatter.
  - A DRAFT record cannot be set APPROVED directly; it has to be submitted for
    approval first. The error says "Invalid status transition", which reads like a
    bad target rather than a missing step.
"""

import json
import sys
import os
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_registry as ar


# ---------------------------------------------------------------------------
# Namespace
# ---------------------------------------------------------------------------

def test_the_client_is_the_ga_namespace():
    """The whole point of the migration: the old namespace stops serving Registry
    on 2026-09-17."""
    assert ar.REGISTRY_CLIENT == "agent-registry-control"
    assert ar.IAM_PREFIX == "agent-registry"
    assert ar.ARN_SERVICE == "agent-registry"


def test_registry_client_passes_no_explicit_endpoint():
    """The docs say `.api.aws`, boto3 resolves `.amazonaws.com`. Letting boto3
    decide avoids betting on which is right."""
    with patch("boto3.client") as client:
        ar.registry_client("us-west-2")
    kwargs = client.call_args.kwargs
    assert client.call_args.args[0] == "agent-registry-control"
    assert kwargs == {"region_name": "us-west-2"}
    assert "endpoint_url" not in kwargs


# ---------------------------------------------------------------------------
# Create response shape
# ---------------------------------------------------------------------------

def test_record_id_comes_from_the_arn():
    resp = {"recordArn": "arn:aws:agent-registry:us-west-2:1234:registry/REG1/record/REC9",
            "status": "CREATING"}
    assert ar.record_id_from_create(resp) == "REC9"


def test_record_id_prefers_an_explicit_field_when_present():
    """Keeps working if a future version restores recordId."""
    assert ar.record_id_from_create({"recordId": "direct", "recordArn": "a/b/c"}) == "direct"


def test_record_id_is_empty_when_the_response_has_neither():
    assert ar.record_id_from_create({}) == ""


# ---------------------------------------------------------------------------
# Descriptor shapes
# ---------------------------------------------------------------------------

def test_agent_descriptor_is_flat_with_data():
    """Preview: descriptors.a2a.agentCard.inlineContent (three levels).
    GA: descriptors.a2aAgentCard.data (two)."""
    d = ar.agent_record_descriptors({"name": "x"})
    assert set(d) == {"a2aAgentCard"}
    assert json.loads(d["a2aAgentCard"]["data"])["name"] == "x"
    assert "inlineContent" not in json.dumps(d)


def test_agent_descriptor_accepts_a_prerendered_json_string():
    d = ar.agent_record_descriptors('{"name":"y"}')
    assert d["a2aAgentCard"]["data"] == '{"name":"y"}'


def test_skill_descriptor_nests_markdown_under_additional_data():
    """The inversion: skillMd was a SIBLING of the definition in preview and is a
    CHILD of it in GA. Renaming preview's fields in place yields an illegal
    record."""
    d = ar.skill_record_descriptors({"name": "s"}, skill_md="---\nname: s\n---\nbody")
    assert set(d) == {"agentSkillsDefinition"}
    inner = d["agentSkillsDefinition"]
    assert "data" in inner and "dataSchemaVersion" in inner
    assert inner["additionalData"]["skillMd"]["data"].startswith("---")
    assert "skillMd" not in set(d)  # not a sibling


def test_skill_descriptor_omits_additional_data_without_markdown():
    d = ar.skill_record_descriptors({"name": "s"})
    assert "additionalData" not in d["agentSkillsDefinition"]


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

def test_frontmatter_is_added_when_missing():
    out = ar.ensure_frontmatter("Use http_request.", name="weather", description="Look up weather")
    assert out.startswith("---\n")
    assert "name: weather" in out
    assert "description: Look up weather" in out
    assert out.count("---") == 2
    assert "Use http_request." in out


def test_existing_frontmatter_is_left_alone():
    original = "---\nname: kept\n---\n\nbody"
    assert ar.ensure_frontmatter(original, name="ignored") == original


def test_a_multiline_description_is_flattened():
    """A newline inside the description would close the frontmatter block early."""
    out = ar.ensure_frontmatter("body", name="n", description="line one\nline two")
    head = out.split("---")[1]
    assert "line one line two" in head
    assert head.count("description:") == 1


def test_empty_markdown_still_gets_a_valid_block():
    out = ar.ensure_frontmatter("", name="n")
    assert out.startswith("---\n") and out.rstrip().endswith("---")


# ---------------------------------------------------------------------------
# Reading records back
# ---------------------------------------------------------------------------

def test_reads_a_ga_agent_card():
    rec = {"descriptors": {"a2aAgentCard": {"data": '{"name":"ga"}'}}}
    assert json.loads(ar.read_agent_card(rec))["name"] == "ga"


def test_falls_back_to_the_preview_agent_card_path():
    """A record created before the migration must still read — otherwise the A2A
    tool builder silently drops it."""
    rec = {"descriptors": {"a2a": {"agentCard": {"inlineContent": '{"name":"old"}'}}}}
    assert json.loads(ar.read_agent_card(rec))["name"] == "old"


def test_missing_card_reads_as_empty_not_an_exception():
    assert ar.read_agent_card({}) == ""
    assert ar.read_agent_card({"descriptors": {}}) == ""


def test_reads_a_ga_skill_record():
    rec = {"descriptors": {"agentSkillsDefinition": {
        "data": '{"name":"s"}',
        "additionalData": {"skillMd": {"data": "---\nname: s\n---\nbody"}}}}}
    definition, md = ar.read_skill_definition(rec)
    assert json.loads(definition)["name"] == "s"
    assert md.startswith("---")


def test_falls_back_to_the_preview_skill_paths():
    rec = {"descriptors": {"agentSkills": {
        "skillDefinition": {"inlineContent": '{"name":"old"}'},
        "skillMd": {"inlineContent": "# old"}}}}
    definition, md = ar.read_skill_definition(rec)
    assert json.loads(definition)["name"] == "old"
    assert md == "# old"


# ---------------------------------------------------------------------------
# Listing and status
# ---------------------------------------------------------------------------

def test_list_uses_the_structured_filters_shape():
    """GA replaced ad-hoc parameters with [{"name": field, "values": [...]}], and
    dropped descriptorType entirely."""
    client = MagicMock()
    client.list_registry_records.return_value = {"registryRecords": [{"recordId": "a"}]}
    ar.list_records(client, "REG", record_type="AGENT", status="APPROVED")
    filters = client.list_registry_records.call_args.kwargs["filters"]
    assert {"name": "recordType", "values": ["AGENT"]} in filters
    assert {"name": "status", "values": ["APPROVED"]} in filters
    assert "descriptorType" not in json.dumps(filters)


def test_list_sends_no_filters_when_none_are_asked_for():
    client = MagicMock()
    client.list_registry_records.return_value = {"registryRecords": []}
    ar.list_records(client, "REG")
    assert "filters" not in client.list_registry_records.call_args.kwargs


def test_list_paginates():
    client = MagicMock()
    client.list_registry_records.side_effect = [
        {"registryRecords": [{"recordId": "a"}], "nextToken": "t1"},
        {"registryRecords": [{"recordId": "b"}]},
    ]
    assert [r["recordId"] for r in ar.list_records(client, "REG")] == ["a", "b"]


def test_status_update_always_sends_a_reason():
    """statusReason is required — omitting it is a ParamValidationError, not a
    service-side default."""
    client = MagicMock()
    ar.set_record_status(client, "REG", "REC", "APPROVED")
    assert client.update_registry_record_status.call_args.kwargs["statusReason"]


# ---------------------------------------------------------------------------
# Approval flow
# ---------------------------------------------------------------------------

def test_approval_submits_first_because_draft_cannot_go_straight_to_approved():
    """Measured: the only UpdateRegistryRecordStatus transition out of DRAFT is
    DEPRECATED. Setting APPROVED on a DRAFT record fails with "Invalid status
    transition", which reads like a bad target rather than a missing step."""
    client = MagicMock()
    client.get_registry_record.side_effect = [
        {"status": "DRAFT"},              # initial
        {"status": "PENDING_APPROVAL"},   # after submit
        {"status": "APPROVED"},           # after approve
    ]
    with patch("time.sleep"):
        final = ar.approve_record(client, "REG", "REC")
    client.submit_registry_record_for_approval.assert_called_once()
    client.update_registry_record_status.assert_called_once()
    assert final == "APPROVED"


def test_an_already_approved_record_is_left_alone():
    client = MagicMock()
    client.get_registry_record.return_value = {"status": "APPROVED"}
    with patch("time.sleep"):
        assert ar.approve_record(client, "REG", "REC") == "APPROVED"
    client.submit_registry_record_for_approval.assert_not_called()
    client.update_registry_record_status.assert_not_called()


def test_a_pending_record_skips_the_submit_step():
    client = MagicMock()
    client.get_registry_record.side_effect = [
        {"status": "PENDING_APPROVAL"},
        {"status": "APPROVED"},
    ]
    with patch("time.sleep"):
        assert ar.approve_record(client, "REG", "REC") == "APPROVED"
    client.submit_registry_record_for_approval.assert_not_called()
    client.update_registry_record_status.assert_called_once()


def test_record_types_match_the_service_enum():
    assert ar.RECORD_TYPE_AGENT == "AGENT"
    assert ar.RECORD_TYPE_SKILL == "SKILL"
    # descriptorType and its values are gone in GA.
    assert not hasattr(ar, "DESCRIPTOR_TYPE_A2A")


def test_dedup_name_is_stable_and_skips_blanks():
    assert ar.dedup_name("a", "b") == "a-b"
    assert ar.dedup_name("a", "", None or "") == "a"
    assert ar.dedup_name() == "record"
