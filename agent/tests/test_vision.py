"""Tests for agent/vision.py — image captioning via Bedrock.

Validates caption_images(images, user_prompt) -> (caption_text, warnings):
  * happy path → Bedrock called once, text returned, no warnings
  * mixed validity → rejected images excluded, warning lists indices
  * all rejected → Bedrock NOT called, placeholder returned
  * Bedrock throttles then succeeds → one retry, success returned
  * Bedrock persistently fails → placeholder + service-unavailable warning
  * per-image byte cap enforced server-side (decoded > 20 MB)
"""
import base64
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tiny_png_b64():
    # 1x1 transparent PNG
    raw = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
    )
    return base64.b64encode(raw).decode("ascii")


def _bedrock_response(text: str):
    return {"output": {"message": {"content": [{"text": text}]}}}


def test_happy_path_single_image(tiny_png_b64):
    from vision import caption_images

    with patch("vision._client") as mclient:
        mclient.return_value.converse.return_value = _bedrock_response("Image 1: a small dot.")
        caption, warnings = caption_images(
            [{"mediaType": "image/png", "data": tiny_png_b64}],
            "What is this?",
        )

    assert "Image 1: a small dot." in caption
    assert warnings == ""
    mclient.return_value.converse.assert_called_once()
    args, kwargs = mclient.return_value.converse.call_args
    messages = kwargs["messages"]
    assert len(messages) == 1
    content = messages[0]["content"]
    # first block is text (prompt preface), then image blocks
    assert content[0]["text"]
    image_blocks = [b for b in content if "image" in b]
    assert len(image_blocks) == 1
    assert image_blocks[0]["image"]["format"] == "png"
    assert isinstance(image_blocks[0]["image"]["source"]["bytes"], (bytes, bytearray))


def test_mixed_validity_drops_bad_mime(tiny_png_b64):
    from vision import caption_images

    with patch("vision._client") as mclient:
        mclient.return_value.converse.return_value = _bedrock_response("Image 1: dot.")
        caption, warnings = caption_images(
            [
                {"mediaType": "image/bmp", "data": tiny_png_b64},
                {"mediaType": "image/png", "data": tiny_png_b64},
            ],
            "",
        )

    # Only one image should reach bedrock
    args, kwargs = mclient.return_value.converse.call_args
    image_blocks = [b for b in kwargs["messages"][0]["content"] if "image" in b]
    assert len(image_blocks) == 1
    assert "1" in warnings  # 1-based index of rejected image
    assert "Image 1: dot." in caption


def test_all_rejected_skips_bedrock(tiny_png_b64):
    from vision import caption_images

    with patch("vision._client") as mclient:
        caption, warnings = caption_images(
            [
                {"mediaType": "image/bmp", "data": tiny_png_b64},
                {"mediaType": "image/tiff", "data": tiny_png_b64},
            ],
            "",
        )

    mclient.return_value.converse.assert_not_called()
    assert "[No valid images" in caption
    assert "1" in warnings and "2" in warnings


def test_retry_on_throttle_then_success(tiny_png_b64):
    from vision import caption_images
    from botocore.exceptions import ClientError

    throttle = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "Converse",
    )

    with patch("vision._client") as mclient, patch("vision.time.sleep"):
        mclient.return_value.converse.side_effect = [
            throttle,
            _bedrock_response("Image 1: success after retry."),
        ]
        caption, warnings = caption_images(
            [{"mediaType": "image/png", "data": tiny_png_b64}], "x",
        )

    assert mclient.return_value.converse.call_count == 2
    assert "success after retry" in caption
    assert warnings == ""


def test_persistent_failure_returns_placeholder(tiny_png_b64):
    from vision import caption_images
    from botocore.exceptions import ClientError

    err = ClientError(
        {"Error": {"Code": "ServiceUnavailableException", "Message": "boom"}},
        "Converse",
    )

    with patch("vision._client") as mclient, patch("vision.time.sleep"):
        mclient.return_value.converse.side_effect = [err, err]
        caption, warnings = caption_images(
            [{"mediaType": "image/png", "data": tiny_png_b64}], "x",
        )

    assert "[Image(s) could not be analyzed" in caption
    assert "unavailable" in warnings.lower()


def test_oversize_image_rejected_server_side():
    from vision import caption_images

    # Build 21 MB of zeros, base64 it
    big = base64.b64encode(b"\x00" * (21 * 1024 * 1024)).decode("ascii")

    with patch("vision._client") as mclient:
        caption, warnings = caption_images(
            [{"mediaType": "image/png", "data": big}], "",
        )

    mclient.return_value.converse.assert_not_called()
    assert "[No valid images" in caption
    assert "1" in warnings


def test_cap_on_count_enforced(tiny_png_b64):
    """More than 3 images: caption_images drops the extras rather than calling Bedrock with them."""
    from vision import caption_images

    with patch("vision._client") as mclient:
        mclient.return_value.converse.return_value = _bedrock_response("ok")
        caption_images(
            [{"mediaType": "image/png", "data": tiny_png_b64}] * 4, "",
        )

    args, kwargs = mclient.return_value.converse.call_args
    image_blocks = [b for b in kwargs["messages"][0]["content"] if "image" in b]
    assert len(image_blocks) == 3
