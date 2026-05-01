"""Tests for the vision-only branch in handle_invocation.

When the payload contains images, the text agent routes directly to
Claude Haiku (via vision.caption_images) and returns the description
to the chatbot WITHOUT invoking Kimi. The exchange is still persisted
to AgentCore Memory so follow-up text turns see it as context.
"""
import base64
import hashlib
import importlib.util
import os
from unittest.mock import patch, MagicMock

import pytest


_AGENT_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent.py")


@pytest.fixture(scope="module")
def agent_mod():
    spec = importlib.util.spec_from_file_location("agent_script", _AGENT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tiny_png_b64():
    raw = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
    )
    return base64.b64encode(raw).decode("ascii")


def _ctx(session_id="sess", headers=None):
    ctx = MagicMock()
    ctx.session_id = session_id
    ctx.request_headers = headers or {}
    return ctx


def test_images_bypass_kimi_and_return_description(agent_mod, tiny_png_b64):
    """Images → Haiku only; Kimi's invoke_agent must not be called."""
    with patch("vision.caption_images") as mcap, \
         patch.object(agent_mod, "invoke_agent") as minvoke, \
         patch.object(agent_mod, "_record_session"), \
         patch.object(agent_mod, "_persist_vision_turn") as mpers:
        mcap.return_value = ("Image 1: a smart-home dashboard.", "")

        out = agent_mod.handle_invocation(
            {"prompt": "describe this", "userId": "u@x",
             "images": [{"mediaType": "image/png", "data": tiny_png_b64}]},
            _ctx(),
        )

    assert out == {"response": "Image 1: a smart-home dashboard.", "status": "success"}
    mcap.assert_called_once()
    minvoke.assert_not_called()
    mpers.assert_called_once()


def test_images_with_warning_prefix_in_response(agent_mod, tiny_png_b64):
    """Partial-success warnings are surfaced to the chatbot response."""
    with patch("vision.caption_images") as mcap, \
         patch.object(agent_mod, "invoke_agent") as minvoke, \
         patch.object(agent_mod, "_record_session"), \
         patch.object(agent_mod, "_persist_vision_turn"):
        mcap.return_value = ("Image 1: ok.", "Note: rejected image 2 (bad format).")

        out = agent_mod.handle_invocation(
            {"prompt": "", "userId": "u@x",
             "images": [{"mediaType": "image/png", "data": tiny_png_b64}]},
            _ctx(),
        )

    assert "Image 1: ok." in out["response"]
    assert "Note: rejected image 2" in out["response"]
    minvoke.assert_not_called()


def test_memory_event_is_strands_compatible(agent_mod, tiny_png_b64):
    """The messages written by _persist_vision_turn must round-trip through
    AgentCoreMemoryConverter — otherwise Kimi's later list_messages call
    throws JSONDecodeError and the whole short-term history is dropped."""
    fake_memory = MagicMock()
    with patch("vision.caption_images") as mcap, \
         patch.object(agent_mod, "invoke_agent"), \
         patch.object(agent_mod, "_record_session"), \
         patch.dict(os.environ, {"MEMORY_SMARTHOMEMEMORY_ID": "mem-id"}, clear=False), \
         patch.object(agent_mod, "_memory_client", return_value=fake_memory):
        mcap.return_value = ("Image 1: a dot.", "")

        agent_mod.handle_invocation(
            {"prompt": "what is this", "userId": "u@x",
             "images": [{"mediaType": "image/png", "data": tiny_png_b64}]},
            _ctx(session_id="sess-roundtrip"),
        )

    kwargs = fake_memory.create_event.call_args.kwargs
    messages = kwargs["messages"]
    # Simulate a list_events response using those messages, then round-trip
    # through the Strands converter. This is what Kimi's session manager does
    # on every subsequent text turn.
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemoryConverter,
    )
    import datetime as _dt
    fake_events = [{
        "eventId": "0000001777000000000#abcdef00",
        "eventTimestamp": _dt.datetime.now(),
        "payload": [
            {"conversational": {"content": {"text": txt}, "role": role}}
            for (txt, role) in messages
        ],
    }]
    restored = AgentCoreMemoryConverter.events_to_messages(fake_events)
    # Must produce at least one message. Empty list means list_messages
    # would swallow a JSONDecodeError and Kimi would see nothing.
    assert len(restored) >= 1, "Strands converter could not parse vision event"
    texts = []
    for m in restored:
        for c in (m.message or {}).get("content", []):
            if "text" in c:
                texts.append(c["text"])
    joined = " ".join(texts)
    assert "what is this" in joined
    assert "Image 1: a dot." in joined


def test_memory_event_written_with_fingerprint(agent_mod, tiny_png_b64):
    """The vision turn is persisted: (user prompt, USER) + (description, ASSISTANT),
    with metadata carrying a small fingerprint of the images (count, MIME, size, sha256).
    Raw base64 bytes must NOT appear in metadata."""
    fake_memory = MagicMock()
    mem_id_env = {"MEMORY_SMARTHOMEMEMORY_ID": "mem-id-123"}
    with patch("vision.caption_images") as mcap, \
         patch.object(agent_mod, "invoke_agent"), \
         patch.object(agent_mod, "_record_session"), \
         patch.dict(os.environ, mem_id_env, clear=False), \
         patch.object(agent_mod, "_memory_client", return_value=fake_memory):
        mcap.return_value = ("Image 1: ok.", "")

        raw = base64.b64decode(tiny_png_b64)
        expected_sha = hashlib.sha256(raw).hexdigest()

        agent_mod.handle_invocation(
            {"prompt": "what is this", "userId": "u@x",
             "images": [{"mediaType": "image/png", "data": tiny_png_b64}]},
            _ctx(session_id="sess-1"),
        )

    fake_memory.create_event.assert_called_once()
    kwargs = fake_memory.create_event.call_args.kwargs
    assert kwargs["memory_id"] == "mem-id-123"
    assert kwargs["session_id"] == "sess-1"
    # actor_id is sanitized (@/. replaced with _ — see memory/session.py)
    assert "u_x" in kwargs["actor_id"] or kwargs["actor_id"] == "u@x"
    messages = kwargs["messages"]
    assert any("what is this" in m[0] and m[1] == "USER" for m in messages)
    assert any("Image 1: ok." in m[0] and m[1] == "ASSISTANT" for m in messages)

    # Metadata fingerprint, not raw bytes
    meta = kwargs.get("metadata") or {}
    serialized = repr(meta)
    assert tiny_png_b64 not in serialized
    assert "image/png" in serialized
    assert expected_sha[:16] in serialized  # sha prefix is enough to prove provenance


def test_memory_write_failure_does_not_break_response(agent_mod, tiny_png_b64):
    """If memory write fails, the user still gets Haiku's description."""
    fake_memory = MagicMock()
    fake_memory.create_event.side_effect = RuntimeError("memory down")

    with patch("vision.caption_images") as mcap, \
         patch.object(agent_mod, "invoke_agent"), \
         patch.object(agent_mod, "_record_session"), \
         patch.dict(os.environ, {"MEMORY_SMARTHOMEMEMORY_ID": "mem-id"}, clear=False), \
         patch.object(agent_mod, "_memory_client", return_value=fake_memory):
        mcap.return_value = ("Image 1: resilient.", "")

        out = agent_mod.handle_invocation(
            {"prompt": "x", "userId": "u@x",
             "images": [{"mediaType": "image/png", "data": tiny_png_b64}]},
            _ctx(),
        )

    assert out["status"] == "success"
    assert "Image 1: resilient." in out["response"]


def test_too_many_images_rejected(agent_mod, tiny_png_b64):
    with patch("vision.caption_images") as mcap, \
         patch.object(agent_mod, "invoke_agent") as minvoke:
        out = agent_mod.handle_invocation(
            {"prompt": "hi", "userId": "u@x",
             "images": [{"mediaType": "image/png", "data": tiny_png_b64}] * 4},
            _ctx(),
        )

    assert "error" in out
    mcap.assert_not_called()
    minvoke.assert_not_called()


def test_images_not_a_list_rejected(agent_mod):
    with patch("vision.caption_images") as mcap, \
         patch.object(agent_mod, "invoke_agent") as minvoke:
        out = agent_mod.handle_invocation(
            {"prompt": "hi", "userId": "u@x", "images": "oops"},
            _ctx(),
        )

    assert "error" in out
    mcap.assert_not_called()
    minvoke.assert_not_called()


def test_no_images_still_routes_to_kimi(agent_mod):
    """Regression guard: plain-text turns go through Kimi as before."""
    with patch("vision.caption_images") as mcap, \
         patch.object(agent_mod, "invoke_agent") as minvoke, \
         patch.object(agent_mod, "_record_session"):
        minvoke.return_value = "OK"

        out = agent_mod.handle_invocation(
            {"prompt": "turn on the LED", "userId": "u@x"},
            _ctx(),
        )

    mcap.assert_not_called()
    minvoke.assert_called_once_with(
        "turn on the LED",
        session_id="sess", actor_id="u@x", auth_header=None,
    )
    assert out == {"response": "OK", "status": "success"}


def test_raw_bytes_persisted_to_session_storage_before_haiku(agent_mod, tiny_png_b64, tmp_path, monkeypatch):
    """Raw image bytes must be saved to /mnt/workspace/<session>/uploads/images
    BEFORE vision.caption_images is invoked (persist-first semantics)."""
    monkeypatch.setenv("AGENT_SESSION_ROOT", str(tmp_path))
    import sys
    sys.modules.pop("session_storage", None)

    call_order = []

    def fake_save(*args, **kwargs):
        call_order.append("save")
        # Return a realistic entry
        return {"path": "uploads/images/x.png", "sha256": "abcdef" * 10 + "abcd",
                "mime": kwargs.get("mime", "image/png"), "bytes": 100, "ts": "t"}

    def fake_caption(images, prompt):
        call_order.append("caption")
        return ("Image 1: ok.", "")

    with patch("session_storage.save_image", side_effect=fake_save) as msave, \
         patch("vision.caption_images", side_effect=fake_caption), \
         patch.object(agent_mod, "invoke_agent"), \
         patch.object(agent_mod, "_record_session"), \
         patch.object(agent_mod, "_persist_vision_turn"):
        agent_mod.handle_invocation(
            {"prompt": "q", "userId": "u@x",
             "images": [{"mediaType": "image/png", "data": tiny_png_b64}]},
            _ctx(session_id="sess-persist"),
        )

    assert msave.called
    assert call_order == ["save", "caption"], f"persist-first violated: {call_order}"


def test_memory_metadata_includes_storage_path(agent_mod, tiny_png_b64, tmp_path, monkeypatch):
    """After save_image returns a path, that path is attached to the memory event
    metadata (image_N_path) so future agent features can retrieve the raw bytes."""
    monkeypatch.setenv("AGENT_SESSION_ROOT", str(tmp_path))
    import sys
    sys.modules.pop("session_storage", None)

    def fake_save(session_id, mime, raw, **kw):
        return {"path": "uploads/images/my-image.png", "sha256": "f" * 64,
                "mime": mime, "bytes": len(raw), "ts": "t"}

    fake_memory = MagicMock()
    with patch("session_storage.save_image", side_effect=fake_save), \
         patch("vision.caption_images", return_value=("Image 1.", "")), \
         patch.object(agent_mod, "invoke_agent"), \
         patch.object(agent_mod, "_record_session"), \
         patch.dict(os.environ, {"MEMORY_SMARTHOMEMEMORY_ID": "mem-id"}, clear=False), \
         patch.object(agent_mod, "_memory_client", return_value=fake_memory):
        agent_mod.handle_invocation(
            {"prompt": "x", "userId": "u@x",
             "images": [{"mediaType": "image/png", "data": tiny_png_b64}]},
            _ctx(session_id="sess-meta"),
        )

    kwargs = fake_memory.create_event.call_args.kwargs
    meta_repr = repr(kwargs.get("metadata") or {})
    assert "image_1_path" in meta_repr
    assert "uploads/images/my-image.png" in meta_repr


def test_persist_failure_does_not_break_response(agent_mod, tiny_png_b64, tmp_path, monkeypatch):
    """If session storage write fails, Haiku still runs and the user still gets an answer."""
    monkeypatch.setenv("AGENT_SESSION_ROOT", str(tmp_path))
    import sys
    sys.modules.pop("session_storage", None)

    with patch("session_storage.save_image", side_effect=OSError("disk full")), \
         patch("vision.caption_images", return_value=("Image 1: resilient.", "")), \
         patch.object(agent_mod, "invoke_agent"), \
         patch.object(agent_mod, "_record_session"), \
         patch.object(agent_mod, "_persist_vision_turn"):
        out = agent_mod.handle_invocation(
            {"prompt": "x", "userId": "u@x",
             "images": [{"mediaType": "image/png", "data": tiny_png_b64}]},
            _ctx(),
        )

    assert out["status"] == "success"
    assert "Image 1: resilient." in out["response"]


def test_warmup_unchanged(agent_mod):
    with patch("vision.caption_images") as mcap, \
         patch.object(agent_mod, "invoke_agent") as minvoke:
        out = agent_mod.handle_invocation(
            {"prompt": "__warmup__", "userId": "u@x"},
            _ctx(),
        )

    assert out == {"status": "warmup_ok"}
    mcap.assert_not_called()
    minvoke.assert_not_called()
