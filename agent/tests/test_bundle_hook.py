import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_register_hook_replaces_prompt_when_baggage_resolves():
    """When baggage carries a valid bundle reference, the hook overrides
    the model call's system_prompt with the bundle's prompt."""
    # Mock the strands module before importing bundle_config
    fake_strands = MagicMock()
    with patch.dict("sys.modules", {"strands": fake_strands, "strands.hooks": fake_strands.hooks}):
        import bundle_config
        fake_agent = MagicMock()
        headers = {"baggage": "bundle-arn=arn:bundle:foo,bundle-version-id=v1"}

        with patch.object(bundle_config, "load_from_baggage", return_value="BUNDLE PROMPT"):
            bundle_config.register_before_model_call_hook(fake_agent, headers)

        # The Strands hook API is fake_agent.hooks.add_callback or fake_agent.hooks.register.
        # Capture the registered callback and invoke it with a fake event.
        assert fake_agent.hooks.add_callback.called or fake_agent.hooks.register.called
        register = fake_agent.hooks.add_callback if fake_agent.hooks.add_callback.called else fake_agent.hooks.register
        args, _ = register.call_args
        callback = args[1] if len(args) > 1 else args[0]

        fake_event = MagicMock()
        fake_event.kwargs = {"system_prompt": "ORIGINAL"}
        with patch.object(bundle_config, "load_from_baggage", return_value="BUNDLE PROMPT"):
            callback(fake_event)
        assert fake_event.kwargs["system_prompt"] == "BUNDLE PROMPT"


def test_register_hook_noop_when_baggage_missing():
    """When no baggage / bundle resolution returns None, the hook leaves
    system_prompt untouched."""
    # Mock the strands module before importing bundle_config
    fake_strands = MagicMock()
    with patch.dict("sys.modules", {"strands": fake_strands, "strands.hooks": fake_strands.hooks}):
        import bundle_config
        fake_agent = MagicMock()

        with patch.object(bundle_config, "load_from_baggage", return_value=None):
            bundle_config.register_before_model_call_hook(fake_agent, headers={})

        register = fake_agent.hooks.add_callback if fake_agent.hooks.add_callback.called else fake_agent.hooks.register
        args, _ = register.call_args
        callback = args[1] if len(args) > 1 else args[0]

        fake_event = MagicMock()
        fake_event.kwargs = {"system_prompt": "ORIGINAL"}
        with patch.object(bundle_config, "load_from_baggage", return_value=None):
            callback(fake_event)
        assert fake_event.kwargs["system_prompt"] == "ORIGINAL"
