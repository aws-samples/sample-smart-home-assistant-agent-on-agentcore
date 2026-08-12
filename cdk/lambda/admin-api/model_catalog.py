"""The model picker's catalog, built live from Bedrock rather than hardcoded.

Two calls, because the id you can actually invoke is not always the base model id:
``ListFoundationModels`` for the models and ``ListInferenceProfiles`` for the
cross-region ids that newer Claude models require. Both are ``bedrock-runtime``.

This replaced a hand-maintained list of 33 entries in the admin console that had to
be edited every time Bedrock shipped a model, and that could not say which endpoint
served an entry.

**Bedrock Mantle is deliberately not listed.** A working version that merged the
`bedrock-mantle` listing in was built and verified live, then taken back out with the
rest of the Mantle integration; see `agent/model_provider.py` for the findings worth
keeping (the listing path, the two mutually exclusive API formats, the bearer-token
auth). Re-adding it here is one more listing plus an `endpoint` value that is not
always "runtime".

The result carries ``catalogError`` instead of silently shortening the list. A
failed lookup and a genuinely empty catalog are the same shape otherwise -- an
empty array inside a 200 -- and that ambiguity has already cost this project real
diagnosis time on the A2A catalog (see index.list_a2a_agents). An admin who sees
an empty picker goes looking for models to enable; one who sees the reason goes
and looks at permissions.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import boto3

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# The default when nothing is configured. Must stay in agreement with
# agent/agent.py MODEL_ID and the two writes in scripts/setup-agentcore.py.
DEFAULT_MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-6")

# Weeks, not seconds, is the real rate of change for a model catalog. 15 minutes
# keeps a newly enabled model from needing a Lambda redeploy to appear while
# costing at most one pair of calls per container per quarter hour.
_TTL_SECONDS = 15 * 60

_CACHE: dict | None = None
_CACHE_AT: float = 0.0
_LOCK = threading.Lock()

# Provider prefix -> display name. Only for labelling; an unknown prefix falls
# back to the prefix itself rather than being dropped, because a model we cannot
# label is still a model an admin may need to select.
_PROVIDERS = {
    "ai21": "AI21 Labs",
    "amazon": "Amazon",
    "anthropic": "Anthropic",
    "cohere": "Cohere",
    "deepseek": "DeepSeek",
    "google": "Google",
    "meta": "Meta",
    "minimax": "MiniMax",
    "mistral": "Mistral AI",
    "moonshot": "Moonshot AI",
    "moonshotai": "Moonshot AI",
    "nvidia": "NVIDIA",
    "openai": "OpenAI",
    "qwen": "Qwen",
    "stability": "Stability AI",
    "twelvelabs": "TwelveLabs",
    "writer": "Writer",
    "xai": "xAI",
    "zai": "Z.AI",
}


def _bedrock():
    return boto3.client("bedrock", region_name=AWS_REGION)


def _strip_inference_prefix(model_id: str) -> str:
    """``us.anthropic.claude-...`` -> ``anthropic.claude-...``.

    Cross-region inference profiles prefix the geography onto the id. The prefix
    is part of the id you invoke and is kept as such; it is stripped only to work
    out the provider, so ``us.anthropic...`` is not filed under a provider called
    "us".
    """
    for geo in ("us.", "eu.", "apac.", "global."):
        if model_id.startswith(geo):
            return model_id[len(geo):]
    return model_id


def _provider_of(model_id: str) -> str:
    head = _strip_inference_prefix(model_id).split(".", 1)[0]
    return _PROVIDERS.get(head, head or "Other")


def _label_from_id(model_id: str) -> str:
    """A human label for an id the listing gave no name for (the Mantle case).

    ``qwen.qwen3-32b`` -> ``QWEN3 32B``.

    Deliberately a shallow transform: split, upper-case the acronym-ish and
    version-ish fragments, capitalise the words. An earlier version tried to weld
    versions onto family names so this would read exactly like the model cards
    ("GPT-5.6 Luna"), and it turned ``qwen.qwen3-235b-a22b-2507-v1:0`` into
    "QWEN3-235B A22B-2507 V1:0". Chasing every naming convention with a heuristic
    is not winnable, and it is not worth winning: the UI shows the full model id
    next to this label, so the label only has to be recognisable, and a wrong
    guess about punctuation is cheaper than hiding a model an admin needs.

    Reached only when a listing gave no ``modelName``, which ``ListFoundationModels``
    always does — so this is a fallback for an inference profile whose name is
    missing, and for any future listing that returns ids alone.
    """
    tail = _strip_inference_prefix(model_id).split(".", 1)[-1]
    words: list[str] = []
    for word in tail.replace("_", "-").split("-"):
        if not word:
            continue
        # Version-ish and acronym-ish fragments read worse title-cased:
        # "e2b" -> "E2B", "235b" -> "235B", "oss" -> "OSS".
        if any(ch.isdigit() for ch in word) or len(word) <= 3:
            piece = word.upper()
        else:
            piece = word.capitalize()
        words.append(piece)
    return " ".join(words) or model_id


def _base_models() -> dict[str, dict]:
    """Text-output foundation models, keyed by model ARN.

    Keyed by ARN because that is the only field an inference profile gives back to
    join on. Filtered to TEXT output: an image or embedding model is not a choice
    an admin can usefully make in the orchestrator's model picker.
    """
    resp = _bedrock().list_foundation_models(byOutputModality="TEXT")
    return {
        summary["modelArn"]: summary
        for summary in resp.get("modelSummaries", [])
        if summary.get("modelArn") and summary.get("modelId")
    }


def _inference_profiles() -> list[dict]:
    """System-defined (cross-region) inference profiles, paginated."""
    client = _bedrock()
    out: list[dict] = []
    token = None
    while True:
        kwargs = {"typeEquals": "SYSTEM_DEFINED", "maxResults": 1000}
        if token:
            kwargs["nextToken"] = token
        resp = client.list_inference_profiles(**kwargs)
        out.extend(resp.get("inferenceProfileSummaries") or [])
        token = resp.get("nextToken")
        if not token:
            return out


def _runtime_models() -> list[dict]:
    """Everything ``bedrock-runtime`` serves, as catalog entries.

    Two listings, because the id you can actually invoke is not always the base
    model id. Newer Claude models reject a bare id with "Invocation of model ID
    ... with on-demand throughput isn't supported. Retry your request with the ID
    or ARN of an inference profile" -- the cross-region profile id (``us.``,
    ``global.``) from ListInferenceProfiles is the invocable one. Models like Kimi
    and DeepSeek support ON_DEMAND directly and have no profile.

    **One row per model.** Where a base model has a system-defined profile, the
    profile id wins and the bare id is dropped: offering both would put two
    entries in front of the admin that differ only in routing, and the bare one is
    the one that can fail. Where there is no profile, the bare id is offered if it
    supports ON_DEMAND.
    """
    bases = _base_models()

    try:
        profiles = _inference_profiles()
    except Exception as exc:  # noqa: BLE001
        # A missing ListInferenceProfiles permission must not empty the catalog --
        # it should cost the cross-region entries and nothing else.
        logger.warning("ListInferenceProfiles failed, offering base ids only: %s", exc)
        profiles = []

    out: list[dict] = []
    covered_arns: set[str] = set()

    for profile in profiles:
        profile_id = profile.get("inferenceProfileId", "")
        if not profile_id or profile.get("status") != "ACTIVE":
            continue
        arns = [m.get("modelArn", "") for m in (profile.get("models") or [])]
        base = next((bases[a] for a in arns if a in bases), None)
        if base is None:
            # Either not a text model or not visible to this account. Skipping is
            # right for both: the picker must not offer what cannot be invoked.
            continue
        covered_arns.update(a for a in arns if a in bases)
        lifecycle = (base.get("modelLifecycle") or {}).get("status", "")
        out.append({
            "id": profile_id,
            "label": profile.get("inferenceProfileName") or _label_from_id(profile_id),
            "provider": base.get("providerName") or _provider_of(profile_id),
            "endpoint": "runtime",
            "vision": "IMAGE" in (base.get("inputModalities") or []),
            "deprecated": bool(lifecycle) and lifecycle != "ACTIVE",
        })

    for arn, base in bases.items():
        if arn in covered_arns:
            continue
        if "ON_DEMAND" not in (base.get("inferenceTypesSupported") or []):
            # Provisioned-only and no profile: naming it produces a runtime error
            # the admin cannot act on from this page.
            continue
        lifecycle = (base.get("modelLifecycle") or {}).get("status", "")
        out.append({
            "id": base["modelId"],
            "label": base.get("modelName") or _label_from_id(base["modelId"]),
            "provider": base.get("providerName") or _provider_of(base["modelId"]),
            "endpoint": "runtime",
            "vision": "IMAGE" in (base.get("inputModalities") or []),
            "deprecated": bool(lifecycle) and lifecycle != "ACTIVE",
        })
    return out


def _build() -> dict:
    """The catalog. Never raises: a partial catalog beats none."""
    errors: list[str] = []

    try:
        runtime = _runtime_models()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ListFoundationModels failed: %s", exc)
        runtime = []
        errors.append(f"bedrock-runtime listing failed: {exc}")

    merged: dict[str, dict] = {}
    for entry in runtime:
        merged.setdefault(entry["id"], entry)

    models = sorted(
        merged.values(),
        key=lambda m: (m["provider"].lower(), m["deprecated"], m["label"].lower()),
    )
    return {
        "models": models,
        "defaultModelId": DEFAULT_MODEL_ID,
        "catalogError": "; ".join(errors),
    }


def catalog(refresh: bool = False) -> dict:
    """The merged catalog, cached per container for ``_TTL_SECONDS``.

    A result carrying a ``catalogError`` is NOT cached: the next request should
    retry rather than inherit a throttle or a propagation delay for 15 minutes.
    """
    global _CACHE, _CACHE_AT
    with _LOCK:
        fresh = _CACHE is not None and (time.time() - _CACHE_AT) < _TTL_SECONDS
        if fresh and not refresh:
            return _CACHE

        built = _build()
        if not built["catalogError"]:
            _CACHE, _CACHE_AT = built, time.time()
        return built


def endpoint_for(model_id: str) -> str:
    """``"runtime"`` or ``"mantle"`` for one id, or ``""`` when unknown.

    Used on the settings write path so the resolved endpoint is stored next to the
    chosen model. The agent cannot reach this Lambda, and having it re-derive the
    endpoint on every cold start would put an avoidable HTTP call in front of the
    first turn.
    """
    if not model_id:
        return ""
    for entry in catalog().get("models", []):
        if entry["id"] == model_id:
            return entry["endpoint"]
    return ""
