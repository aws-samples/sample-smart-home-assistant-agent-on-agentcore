"""Tests for the voice path's per-user tool scoping.

The text path has always wrapped every gateway tool that reaches a per-user
backend so that `user_id` is injected from the runtime-validated idToken and
never appears in the schema the model sees (agent.py's scoped_suffixes). The
voice path handed the gateway's tool list to Nova Sonic verbatim, so once a tool
declared `user_id` the model could fill it in — a cross-user read on the query
tools and a cross-user write on control_device.

These tests pin the wrapper half of the fix: the sub is forced onto the call
regardless of what the model sends, and a schema that still carries `user_id`
gets it stripped before Nova Sonic sees it. (The other half is that the schemas
registered by scripts/setup-agentcore.py no longer declare the field at all.)
"""
import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest

_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_AGENT_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# voice_session does `from agent import ...`, which resolves to the agent PACKAGE
# rather than agent.py. Point the name at the script just long enough to import
# voice_session, then put the package back: test_load_system_prompt_modes.py does
# `import agent.agent`, which needs `agent` to be the package with a __path__.
# Leaving the override in place breaks every test file collected after this one.
_agent_mod = sys.modules.get("agent_script") or _load("agent_script", "agent.py")
_saved_agent_pkg = sys.modules.get("agent")
sys.modules["agent"] = _agent_mod

# voice_session eager-imports Strands' Nova Sonic bidi stack, which depends on
# the smithy-generated Bedrock bidirectional SDK. Those packages are installed
# only in the voice runtime container, not in this venv, and they are not what is
# under test here. Stub any module in those namespaces, and have each stub return
# a MagicMock for any attribute, so this stays working as that SDK's imports move
# around.
import types


class _AnyAttrModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = MagicMock(name=f"{self.__name__}.{name}")
        setattr(self, name, value)
        return value


class _StubBidiSdkFinder:
    """Import hook: fabricate the bidi SDK namespaces if they aren't installed."""

    PREFIXES = ("aws_sdk_bedrock_runtime", "smithy_aws_core", "smithy_core",
                "smithy_http", "smithy_json")

    def find_module(self, fullname, path=None):  # legacy API, kept harmless
        return self if self._owns(fullname) else None

    def _owns(self, fullname):
        root = fullname.split(".", 1)[0]
        return root in self.PREFIXES

    def find_spec(self, fullname, path=None, target=None):
        if not self._owns(fullname):
            return None
        return importlib.util.spec_from_loader(fullname, self)

    def create_module(self, spec):
        return _AnyAttrModule(spec.name)

    def exec_module(self, module):
        module.__path__ = []  # make it a package so submodules resolve


if not any(isinstance(f, _StubBidiSdkFinder) for f in sys.meta_path):
    sys.meta_path.append(_StubBidiSdkFinder())

vs = _load("voice_session_under_test", "voice_session.py")

# Restore the real `agent` package (or remove the alias) now that the import is
# done, so later test modules resolve `agent.agent` normally.
if _saved_agent_pkg is not None:
    sys.modules["agent"] = _saved_agent_pkg
else:
    sys.modules.pop("agent", None)


def _gateway_tool(name, props, required=None):
    """A stand-in for a Strands MCPAgentTool, with the real spec shape:
    {"inputSchema": {"json": {...}}}."""
    t = MagicMock()
    t.tool_name = name
    schema = {"type": "object", "properties": dict(props)}
    if required:
        schema["required"] = list(required)
    t.tool_spec = {
        "name": name,
        "description": f"desc for {name}",
        "inputSchema": {"json": schema},
    }
    return t


def _props(tool):
    return tool.tool_spec["inputSchema"]["json"]["properties"]


def test_user_id_is_removed_from_the_model_facing_schema():
    tool = _gateway_tool(
        "SmartHomeDeviceQuery___query_device_state",
        {"device_id": {"type": "string"}, "user_id": {"type": "string"}},
    )
    assert vs._strip_user_id_from_spec(tool) is True
    assert "user_id" not in _props(tool)
    assert "device_id" in _props(tool)


def test_user_id_is_dropped_from_required_too():
    tool = _gateway_tool(
        "SmartHomeKnowledgeBase___query_knowledge_base",
        {"query": {"type": "string"}, "user_id": {"type": "string"}},
        required=["query", "user_id"],
    )
    vs._strip_user_id_from_spec(tool)
    assert tool.tool_spec["inputSchema"]["json"]["required"] == ["query"]


def test_a_tool_without_user_id_is_left_alone():
    tool = _gateway_tool("SmartHomeNavigation___navigate_to_page",
                         {"page": {"type": "string"}})
    assert vs._strip_user_id_from_spec(tool) is False
    assert _props(tool) == {"page": {"type": "string"}}


def test_wrapper_injects_the_sub():
    client = MagicMock()
    tool = _gateway_tool("SmartHomeDeviceQuery___query_device_state",
                         {"device_id": {"type": "string"}})
    wrapped = vs._wrap_scoped_voice_tool(client, tool, "sub-alice")

    wrapped(device_id="living-sensor-1")

    args = client.call_tool_sync.call_args.kwargs
    assert args["name"] == "SmartHomeDeviceQuery___query_device_state"
    assert args["arguments"] == {"device_id": "living-sensor-1",
                                 "user_id": "sub-alice"}


def test_wrapper_overrides_a_model_supplied_user_id():
    """The whole point: a forged identity must not survive."""
    client = MagicMock()
    tool = _gateway_tool("SmartHomeDeviceQuery___query_device_state",
                         {"device_id": {"type": "string"}})
    wrapped = vs._wrap_scoped_voice_tool(client, tool, "sub-alice")

    wrapped(device_id="living-sensor-1", user_id="sub-bob")

    assert client.call_tool_sync.call_args.kwargs["arguments"]["user_id"] == "sub-alice"


def test_wrapper_sends_no_identity_when_there_is_no_sub():
    """Better that the Lambda rejects the call than that it runs unscoped."""
    client = MagicMock()
    tool = _gateway_tool("SmartHomeDeviceControl___control_device",
                         {"device_type": {"type": "string"}})
    wrapped = vs._wrap_scoped_voice_tool(client, tool, None)

    wrapped(device_type="fan", user_id="sub-bob")

    assert "user_id" not in client.call_tool_sync.call_args.kwargs["arguments"]


def test_wrapper_keeps_the_gateway_name_so_nova_sonic_can_emit_it():
    client = MagicMock()
    name = "SmartHomeDeviceControl___control_device"
    tool = _gateway_tool(name, {"device_type": {"type": "string"}})
    wrapped = vs._wrap_scoped_voice_tool(client, tool, "sub-alice")
    assert wrapped.tool_name == name


def test_a_scoped_tool_is_wrapped_even_when_its_schema_is_already_clean():
    """The registered schemas no longer declare `user_id`, but the Lambda still
    needs one — so wrapping must not be conditional on finding the field."""
    client = MagicMock()
    tool = _gateway_tool("SmartHomeDeviceQuery___query_device_state",
                         {"device_id": {"type": "string"}})
    assert vs._strip_user_id_from_spec(tool) is False  # nothing to strip
    wrapped = vs._wrap_scoped_voice_tool(client, tool, "sub-alice")
    wrapped(device_id="living-sensor-1")
    assert client.call_tool_sync.call_args.kwargs["arguments"]["user_id"] == "sub-alice"


def test_every_scoped_suffix_matches_the_text_path():
    """A tool scoped in one path and not the other is the actual bug class."""
    text_scoped = set(("query_knowledge_base", "control_device",
                       "discover_devices", "query_device_state",
                       "query_sensor_history"))
    # The voice path omits query_knowledge_base because the KB is keyed by email
    # (actor_id), not by sub — voice has no wrapper for that identifier.
    assert set(vs._VOICE_SCOPED_SUFFIXES) == text_scoped - {"query_knowledge_base"}
    # navigate_to_page must be in neither: its result is user-independent.
    assert "navigate_to_page" not in vs._VOICE_SCOPED_SUFFIXES
    src = open(os.path.join(_AGENT_DIR, "agent.py")).read()
    scoped_block = src.split("scoped_suffixes = (", 1)[1].split(")", 1)[0]
    for suffix in vs._VOICE_SCOPED_SUFFIXES:
        assert f'"{suffix}"' in scoped_block, suffix
    assert "navigate_to_page" not in scoped_block
