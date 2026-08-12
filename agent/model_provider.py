"""Building the Strands model for a model id, and reporting which path it took.

One path today: ``BedrockModel`` over Converse on ``bedrock-runtime``.

**Bedrock Mantle is deliberately not integrated yet.** The `bedrock-mantle` endpoint
is the only way to reach the GPT-5.x family, and a working two-path version of this
module was built and verified against the live endpoint before being taken back out.
What that exercise established, so nobody has to rediscover it:

  - The listing is at ``https://bedrock-mantle.{region}.api.aws/v1/models``. The
    ``/openai/v1`` path that the Gemma blog and the GPT-5.6 Luna model card show
    returns 404 for the listing.
  - Mantle serves TWO OpenAI-compatible APIs on different paths and **no model
    accepts both**: `openai.gpt-5.6-luna` and `google.gemma-4-31b` are Responses-only
    (`/openai/v1`), `minimax.minimax-m2.5` is Chat-Completions-only (`/v1`). Calling
    the wrong one returns `400 The model '...' does not support the '...' API`.
  - Nothing reports which format a model accepts: `/v1/models` and `/v1/models/{id}`
    return status and data-retention only. It has to be probed.
  - Reaching it needs `bedrock-mantle:CreateInference|Get*|List*` on `project/*` plus
    `bedrock-mantle:CallWithBearerToken`, authenticated with a short-term bearer token
    (`aws-bedrock-token-generator`) rather than SigV4.
  - Prompt caching there is automatic prefix caching with no cache point to place, so
    the measured 98% prefix-token reduction in docs §8.7.1 does NOT describe it.

Re-adding it means restoring a per-model API-format probe (cached, and careful not to
cache a throttle as a verdict) and a second base URL. The design write-up is in
docs/superpowers/specs/2026-08-12-model-catalog-mantle-and-subagent-policy-design.md.

This module stays as the seam even with one path, so the caller has one place to
build a model and one line that says what it built.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

ENDPOINT_RUNTIME = "runtime"
# Kept as a recognised value so a settings row written while Mantle was integrated,
# or one written by a future re-integration, does not read as corrupt. It resolves to
# the runtime path today; see `resolve_endpoint`.
ENDPOINT_MANTLE = "mantle"


def resolve_endpoint(model_id: str, hint: str = "") -> str:
    """Which endpoint serves ``model_id``. Always ``runtime`` while Mantle is out.

    A stored ``mantle`` hint is answered with ``runtime`` rather than honoured: the
    Mantle path does not exist in this build, and pretending otherwise would fail on
    the first turn instead of at the point the model was chosen. A Converse call
    against a Mantle-only id fails loudly and names the model, which is the
    diagnosable direction.
    """
    if hint == ENDPOINT_MANTLE:
        logger.warning(
            "settings ask for the mantle endpoint for %s, but Bedrock Mantle is not "
            "integrated in this build; using %s", model_id, ENDPOINT_RUNTIME)
    return ENDPOINT_RUNTIME


def build_model(model_id: str, endpoint: str, *, cache_config, streaming=True):
    """A Strands model for ``model_id``.

    ``cache_config`` is passed through rather than constructed here so the caller
    keeps its comment about why ``strategy="auto"`` is the right choice.
    """
    from strands.models.bedrock import BedrockModel

    return BedrockModel(
        model_id=model_id,
        region_name=AWS_REGION,
        streaming=streaming,
        cache_config=cache_config,
    )


def describe(model, model_id: str, endpoint: str, strands_version: str) -> str:
    """One line saying what was built and whether caching engaged.

    This exists because caching failing is silent in BOTH directions: the answer is
    identical and the only signal is a CloudWatch metric that stays at zero.
    Diagnosing it from outside cost an hour once — the spans carry no cache fields,
    so a high `InputTokenCount` with an absent `CacheReadInputTokenCount` was the
    only clue, and it is indistinguishable from "the code was never deployed".

    `_cache_strategy` is Strands' own model-support check: "anthropic" for a Claude
    model id, None otherwise. So this line reports both which Strands is installed
    and whether it will place a cache point.
    """
    strategy = getattr(model, "_cache_strategy", "unavailable")
    return (f"model path: strands={strands_version} model={model_id} "
            f"endpoint={ENDPOINT_RUNTIME} caching=cache-point strategy={strategy}")
