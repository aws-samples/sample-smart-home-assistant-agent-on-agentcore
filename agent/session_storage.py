"""Per-session persistent filesystem on AgentCore Runtime.

AgentCore mounts an isolated volume per `runtimeSessionId` at
`/mnt/workspace` (configured on the runtime via
`filesystemConfigurations.sessionStorage.mountPath`). Everything written
there survives between invocations within the same session and is
inaccessible from other sessions or runtimes.

Layout (designed for reuse across modalities):

    /mnt/workspace/<session_id>/
      uploads/
        images/
          <iso-ts>__<sha256[:16]>.<ext>   # raw bytes
          index.jsonl                     # append-only catalog

Callers pass `session_id` explicitly — AgentCore gives us the session_id in
`context.session_id`, and we namespace inside the mount to stay robust if
AWS ever changes the per-session mounting semantics (today the runtime
itself isolates per session, but owning the layout keeps us portable).
"""
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_ROOT_ENV = "AGENT_SESSION_ROOT"
_DEFAULT_ROOT = "/mnt/workspace"

_MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _root() -> str:
    return os.environ.get(_ROOT_ENV, _DEFAULT_ROOT)


def _session_dir(session_id: str) -> str:
    return os.path.join(_root(), session_id)


def _timestamp_prefix() -> str:
    # ISO 8601 without colons (filesystem-safe on all OSes) and millisecond precision.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%f")[:-3] + "Z"


def save_image(
    session_id: str,
    mime: str,
    raw: bytes,
    user_prompt: Optional[str] = None,
    caption_event_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist raw image bytes under the session's uploads/images/ folder.

    - Deduplicates by sha256: a repeat call with identical bytes reuses the
      existing file but still appends a new index entry (callers often want
      to record that the image was referenced again).
    - Writes atomically via tempfile + os.replace so a crash mid-write
      leaves no partial file.
    - Appends one JSON line per call to index.jsonl — a simple, tail-friendly
      catalog future features can read without parsing file names.

    Returns the catalog entry as a dict.
    """
    sha = hashlib.sha256(raw).hexdigest()
    ext = _MIME_TO_EXT.get(mime, ".bin")
    images_dir = os.path.join(_session_dir(session_id), "uploads", "images")
    os.makedirs(images_dir, exist_ok=True)

    # Dedup: if any existing file has the same sha prefix (within this session),
    # reuse its path.
    existing_path = None
    for name in os.listdir(images_dir):
        if name == "index.jsonl":
            continue
        stem, _ext = os.path.splitext(name)
        if stem.endswith(f"__{sha[:16]}"):
            existing_path = os.path.join("uploads", "images", name)
            break

    if existing_path is None:
        filename = f"{_timestamp_prefix()}__{sha[:16]}{ext}"
        abs_final = os.path.join(images_dir, filename)
        # Atomic write: tempfile in the same dir → os.replace.
        fd, tmp_path = tempfile.mkstemp(prefix=".writing__", dir=images_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            os.replace(tmp_path, abs_final)
        except Exception:
            # On failure: best-effort cleanup of the tempfile; re-raise so the
            # caller can decide whether to continue (vision path should still
            # reply to the user even if disk is full).
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        rel_path = os.path.join("uploads", "images", filename)
    else:
        rel_path = existing_path

    entry = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sha256": sha,
        "mime": mime,
        "bytes": len(raw),
        "path": rel_path,
    }
    if user_prompt is not None:
        entry["user_prompt"] = user_prompt
    if caption_event_id is not None:
        entry["caption_event_id"] = caption_event_id

    # Append to catalog; ignore catalog write failures so the caller's
    # response path is never broken by an index.jsonl hiccup.
    try:
        with open(os.path.join(images_dir, "index.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass

    return entry
