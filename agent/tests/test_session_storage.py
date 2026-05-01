"""Tests for agent/session_storage.py — per-session persistent filesystem.

AgentCore Runtime mounts a per-session volume at `/mnt/workspace`. Every call
to handle_invocation in a given runtimeSessionId sees the same directory;
different sessions (and different runtimes) are isolated.

The module exposes helpers that are agnostic to the mount point (overridable
via env var for tests) so future agent features (audio, documents) can reuse
the layout.
"""
import base64
import json
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def workspace_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSION_ROOT", str(tmp_path))
    # Drop cached module so env var is re-read
    import sys
    sys.modules.pop("session_storage", None)
    return tmp_path


@pytest.fixture
def tiny_png():
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
    )


def test_save_image_writes_bytes_and_returns_relative_path(workspace_root, tiny_png):
    from session_storage import save_image

    entry = save_image(session_id="sess-A", mime="image/png", raw=tiny_png, user_prompt="hi")

    assert entry["path"].startswith("uploads/images/")
    assert entry["path"].endswith(".png")
    assert entry["mime"] == "image/png"
    assert entry["bytes"] == len(tiny_png)
    assert len(entry["sha256"]) == 64

    abs_path = workspace_root / "sess-A" / entry["path"]
    assert abs_path.exists()
    assert abs_path.read_bytes() == tiny_png


def test_filename_is_sortable_and_includes_sha_prefix(workspace_root, tiny_png):
    from session_storage import save_image
    entry = save_image(session_id="s", mime="image/png", raw=tiny_png)
    name = os.path.basename(entry["path"])
    # iso-timestamp-safe prefix (no colons), __ separator, sha prefix, .ext
    stem, ext = os.path.splitext(name)
    assert ext == ".png"
    prefix, sep, sha = stem.rpartition("__")
    assert sep == "__"
    assert len(sha) == 16
    assert ":" not in prefix
    # Prefix starts with the year — sortable by time
    assert prefix.startswith("20")


def test_mime_to_ext_mapping(workspace_root, tiny_png):
    from session_storage import save_image
    for i, (mime, expected) in enumerate([
        ("image/png", ".png"),
        ("image/jpeg", ".jpg"),
        ("image/webp", ".webp"),
        ("image/gif", ".gif"),
    ]):
        # Each entry uses distinct bytes so dedup doesn't reuse the first .png
        entry = save_image(session_id=f"s-{i}", mime=mime, raw=tiny_png + bytes([i]))
        assert entry["path"].endswith(expected)


def test_dedup_by_sha256_reuses_path(workspace_root, tiny_png):
    from session_storage import save_image
    e1 = save_image(session_id="same", mime="image/png", raw=tiny_png)
    e2 = save_image(session_id="same", mime="image/png", raw=tiny_png)
    assert e1["path"] == e2["path"]
    # Only one file written
    images_dir = workspace_root / "same" / "uploads" / "images"
    pngs = [p for p in images_dir.iterdir() if p.suffix == ".png"]
    assert len(pngs) == 1


def test_index_appended_per_save(workspace_root, tiny_png):
    from session_storage import save_image
    save_image(session_id="s1", mime="image/png", raw=tiny_png, user_prompt="first")
    save_image(session_id="s1", mime="image/png", raw=b"\x89PNG different content", user_prompt="second")

    idx = (workspace_root / "s1" / "uploads" / "images" / "index.jsonl").read_text().strip().splitlines()
    assert len(idx) == 2
    first, second = (json.loads(line) for line in idx)
    assert first["user_prompt"] == "first"
    assert second["user_prompt"] == "second"
    for e in (first, second):
        for k in ("ts", "sha256", "mime", "bytes", "path"):
            assert k in e


def test_session_isolation(workspace_root, tiny_png):
    from session_storage import save_image
    save_image(session_id="A", mime="image/png", raw=tiny_png)
    save_image(session_id="B", mime="image/png", raw=tiny_png)

    assert (workspace_root / "A" / "uploads" / "images" / "index.jsonl").exists()
    assert (workspace_root / "B" / "uploads" / "images" / "index.jsonl").exists()
    # Each session got its own image file; same sha appears in both trees but
    # the filesystems are fully independent.
    a_imgs = [p for p in (workspace_root / "A" / "uploads" / "images").iterdir() if p.suffix == ".png"]
    b_imgs = [p for p in (workspace_root / "B" / "uploads" / "images").iterdir() if p.suffix == ".png"]
    assert len(a_imgs) == 1 and len(b_imgs) == 1
    a_shas = {p.stem.rsplit("__", 1)[1] for p in a_imgs}
    b_shas = {p.stem.rsplit("__", 1)[1] for p in b_imgs}
    assert a_shas == b_shas  # same input → same sha prefix


def test_save_image_is_atomic_on_write_failure(workspace_root, tiny_png, monkeypatch):
    """If write fails mid-way, no partial file should be left behind."""
    from session_storage import save_image
    original_replace = os.replace

    calls = {"n": 0}
    def flaky_replace(src, dst):
        calls["n"] += 1
        raise OSError("disk full")

    monkeypatch.setattr("session_storage.os.replace", flaky_replace)
    with pytest.raises(OSError):
        save_image(session_id="s", mime="image/png", raw=tiny_png)

    images_dir = workspace_root / "s" / "uploads" / "images"
    if images_dir.exists():
        # No .png left; .tmp may or may not be cleaned up, but the final path must not exist
        pngs = list(images_dir.glob("*.png"))
        assert pngs == []
