"""Vision captioning via Claude Haiku on Bedrock.

Called by agent.py when handle_invocation receives images in the payload.
Returns (caption_text, warnings) with partial-failure semantics so Kimi can
always reply — see docs/superpowers/specs/2026-04-30-text-agent-image-input-design.md.
"""
import base64
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
VISION_MODEL_ID = os.environ.get(
    "VISION_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

MAX_IMAGES = 3
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB raw

MIME_TO_FORMAT = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/webp": "webp",
    "image/gif": "gif",
}

PROMPT_PREFACE = (
    "The user uploaded the image(s) below along with this message: "
    "'{user_prompt}'. Describe each image in enough detail for a smart-home "
    "assistant to reason about it: subject, colors, any visible text, notable "
    "objects. If the message asks a specific question about the image(s), "
    "answer it as part of the description. Number the images (Image 1, "
    "Image 2, ...)."
)

_RETRYABLE_CODES = {
    "ThrottlingException",
    "ServiceUnavailableException",
    "ModelErrorException",
    "ModelStreamErrorException",
    "InternalServerException",
}

_bedrock_runtime = None


def _client():
    global _bedrock_runtime
    if _bedrock_runtime is None:
        _bedrock_runtime = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _bedrock_runtime


def _validate_and_decode(images):
    """Returns (valid_blocks, rejections) where valid_blocks are Bedrock image
    content blocks and rejections is a list of (1-based-index, reason) tuples."""
    valid = []
    rejections = []
    # Hard-cap count BEFORE validation so over-count images don't consume slots
    capped = images[:MAX_IMAGES]
    if len(images) > MAX_IMAGES:
        logger.info("caption_images: capped input from %d to %d", len(images), MAX_IMAGES)

    for idx, img in enumerate(capped, start=1):
        if not isinstance(img, dict):
            rejections.append((idx, "not an object"))
            continue
        mt = img.get("mediaType")
        data = img.get("data")
        fmt = MIME_TO_FORMAT.get(mt)
        if fmt is None:
            rejections.append((idx, f"unsupported format {mt!r}"))
            continue
        if not isinstance(data, str):
            rejections.append((idx, "missing base64 data"))
            continue
        try:
            raw = base64.b64decode(data, validate=False)
        except Exception:
            rejections.append((idx, "invalid base64"))
            continue
        if len(raw) > MAX_IMAGE_BYTES:
            rejections.append((idx, f"exceeds {MAX_IMAGE_BYTES // (1024*1024)} MB"))
            continue
        valid.append({"image": {"format": fmt, "source": {"bytes": raw}}})
    return valid, rejections


def _warning_from_rejections(rejections):
    if not rejections:
        return ""
    parts = [f"image {i} ({reason})" for i, reason in rejections]
    return "Note: rejected " + "; ".join(parts) + "."


def caption_images(images, user_prompt, model_id=None):
    """Return (caption_text, warnings) for the supplied images.

    - Empty caption is never returned; a placeholder is substituted so the
      caller can always construct a well-formed augmented prompt.
    - Bedrock failures after one retry also fall back to a placeholder.
    - `model_id` is an optional per-user override read from DynamoDB
      settings; falls back to the VISION_MODEL_ID env default.
    """
    valid_blocks, rejections = _validate_and_decode(images or [])
    warnings = _warning_from_rejections(rejections)

    if not valid_blocks:
        return (
            "[No valid images could be processed.]",
            warnings or "Note: all images were rejected.",
        )

    preface = PROMPT_PREFACE.format(user_prompt=(user_prompt or "(no text)"))
    messages = [{
        "role": "user",
        "content": [{"text": preface}, *valid_blocks],
    }]

    effective_model = model_id or VISION_MODEL_ID

    attempts = 0
    last_err = None
    while attempts < 2:
        try:
            resp = _client().converse(
                modelId=effective_model,
                messages=messages,
            )
            content = resp.get("output", {}).get("message", {}).get("content", [])
            text_parts = [c.get("text", "") for c in content if "text" in c]
            caption = "\n".join(p for p in text_parts if p).strip()
            if not caption:
                caption = "[Vision model returned no description.]"
            return caption, warnings
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            last_err = e
            if code in _RETRYABLE_CODES and attempts == 0:
                logger.warning("caption_images: retryable %s, backing off", code)
                time.sleep(0.5)
                attempts += 1
                continue
            break
        except Exception as e:
            last_err = e
            break

    logger.exception("caption_images: giving up after retry: %s", last_err)
    svc_warning = (
        "Note: vision service was unavailable; the assistant is replying "
        "without seeing the images."
    )
    combined = "; ".join(w for w in (warnings, svc_warning) if w)
    return "[Image(s) could not be analyzed at this time.]", combined
