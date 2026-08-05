import os
import pytest

# Set required env vars BEFORE import.
os.environ.setdefault("AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/text")
os.environ.setdefault("VOICE_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/voice")

from optimization_defaults import (
    component_arn_for,
    json_path_for,
    opt_sk,
    parse_opt_sk,
)


def test_component_arn_for_text_returns_text_runtime():
    assert component_arn_for("text") == "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test-text"


def test_component_arn_for_voice_returns_voice_runtime():
    assert component_arn_for("voice") == "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test-voice"


def test_component_arn_for_tool_desc_requires_target_arn_kwarg():
    with pytest.raises(ValueError):
        component_arn_for("tool_desc")
    assert component_arn_for("tool_desc", target_arn="arn:x") == "arn:x"


def test_json_path_for_text_and_voice_use_system_prompt():
    assert json_path_for("text") == "$.configuration.system_prompt"
    assert json_path_for("voice") == "$.configuration.system_prompt"


def test_json_path_for_tool_desc_includes_tool_name():
    assert json_path_for("tool_desc", tool_name="control_device") == \
        "$.configuration.tools.control_device.description"


def test_opt_sk_round_trip_for_each_kind():
    for kind in ("rec", "bundle", "abtest"):
        sk = opt_sk(kind, "abc-123")
        assert sk.startswith(f"__opt_{kind}_")
        assert parse_opt_sk(sk) == (kind, "abc-123")


def test_parse_opt_sk_returns_none_for_non_opt():
    assert parse_opt_sk("__prompt_text__") is None
    assert parse_opt_sk("regular-skill-name") is None


def test_unknown_agent_type_raises():
    with pytest.raises(ValueError):
        component_arn_for("foobar")
    with pytest.raises(ValueError):
        json_path_for("foobar")
