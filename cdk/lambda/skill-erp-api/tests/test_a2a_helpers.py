"""Tests for A2A form <-> registry-descriptor rendering."""
import json

from a2a_helpers import (
    build_card_md,
    build_card_definition,
    parse_card_definition,
    validate_form,
    A2A_NAME_RE,
)


SAMPLE_FORM = {
    "name": "energy-optimization-agent",
    "description": "Recommends schedules to reduce smart-home energy use.",
    "endpoint": "https://example.com/a2a/energy-optimization-agent",
    "version": "1.0.0",
    "provider": "SmartHome Demo",
    "capabilities": {
        "streaming": True,
        "pushNotifications": True,
        "stateTransitionHistory": False,
    },
    "auth": "none",
    "tags": ["energy", "demo"],
    "skills": [
        {
            "id": "analyze-usage",
            "name": "Analyze Usage",
            "description": "Summarises device runtime and energy draw.",
            "examples": ["Summarise last week's fan usage"],
            "tags": [],
        }
    ],
}


def test_name_regex_accepts_valid():
    assert A2A_NAME_RE.match("energy-optimization-agent")
    assert A2A_NAME_RE.match("a1")


def test_name_regex_rejects_invalid():
    for bad in ["", "-leading", "Trailing-", "UPPER", "has_underscore", "a" * 70]:
        assert not A2A_NAME_RE.match(bad), f"should reject {bad!r}"


def test_validate_form_passes_for_sample():
    ok, err = validate_form(SAMPLE_FORM)
    assert ok, err


def test_validate_form_rejects_bad_name():
    bad = dict(SAMPLE_FORM, name="BAD_NAME")
    ok, err = validate_form(bad)
    assert not ok
    assert "name" in err.lower()


def test_validate_form_requires_skill():
    bad = dict(SAMPLE_FORM, skills=[])
    ok, err = validate_form(bad)
    assert not ok
    assert "skill" in err.lower()


def test_build_card_md_frontmatter_fields():
    md = build_card_md(SAMPLE_FORM)
    assert md.startswith("---\n")
    assert 'endpoint: "https://example.com/a2a/energy-optimization-agent"' in md
    assert 'version: "1.0.0"' in md
    assert 'auth: "none"' in md
    assert 'x-capabilities: "streaming,pushNotifications"' in md
    assert 'x-tags: "energy,demo"' in md
    assert "## Description" in md
    assert "## Skills" in md
    assert "analyze-usage" in md


def test_build_card_definition_is_canonical_agentcard():
    raw = build_card_definition(SAMPLE_FORM)
    card = json.loads(raw)
    # AgentCore Registry requires protocolVersion on A2A cards.
    assert card["protocolVersion"], "protocolVersion missing"
    assert card["name"] == "energy-optimization-agent"
    assert card["url"] == "https://example.com/a2a/energy-optimization-agent"
    assert card["version"] == "1.0.0"
    assert card["provider"]["organization"] == "SmartHome Demo"
    assert card["provider"]["url"]  # required by A2A spec
    assert card["capabilities"] == {
        "streaming": True,
        "pushNotifications": True,
        "stateTransitionHistory": False,
    }
    assert card["authentication"] == {"schemes": ["none"]}
    assert card["defaultInputModes"] == ["text"]
    assert card["defaultOutputModes"] == ["text"]
    assert card["tags"] == ["energy", "demo"]
    assert card["skills"][0]["id"] == "analyze-usage"
    assert card["skills"][0]["examples"] == ["Summarise last week's fan usage"]


def test_parse_card_definition_round_trips():
    raw = build_card_definition(SAMPLE_FORM)
    parsed = parse_card_definition(raw)
    # Parsed form mirrors the input shape for all scalar + list fields.
    assert parsed["name"] == SAMPLE_FORM["name"]
    assert parsed["description"] == SAMPLE_FORM["description"]
    assert parsed["endpoint"] == SAMPLE_FORM["endpoint"]
    assert parsed["version"] == SAMPLE_FORM["version"]
    assert parsed["provider"] == SAMPLE_FORM["provider"]
    assert parsed["capabilities"] == SAMPLE_FORM["capabilities"]
    assert parsed["auth"] == SAMPLE_FORM["auth"]
    assert parsed["tags"] == SAMPLE_FORM["tags"]
    assert parsed["skills"] == SAMPLE_FORM["skills"]


def test_parse_card_definition_handles_empty_string():
    parsed = parse_card_definition("")
    assert parsed["name"] == ""
    assert parsed["skills"] == []


def test_parse_card_definition_handles_invalid_json():
    parsed = parse_card_definition("{not json")
    assert parsed["name"] == ""
