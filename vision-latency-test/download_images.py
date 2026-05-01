#!/usr/bin/env python3
"""Fetch a small, fixed set of images from Wikimedia Commons and resize them
to roughly uniform size for the vision-latency probe. Runs once; the images
directory is gitignored (only download_images.py + manifest.json are tracked).

Target: each image ~200 KB on disk as JPEG quality=85, max 1024 px long edge.
Keeping the sizes tight means the HTTP upload leg is a small, constant factor
so the differences we measure are dominated by model latency.
"""
import hashlib
import io
import json
import os
import sys
import time
import urllib.request

# 20 images from Lorem Picsum (Unsplash-backed). Deterministic seeds so the
# corpus is reproducible — the probe picks by name, so the same seed always
# surfaces the same subject. Raw PNG/JPG, no CDN gating.
SOURCES = [f"https://picsum.photos/seed/vision-{i:02d}/1024/768.jpg" for i in range(1, 21)]

TARGET_BYTES = 200 * 1024     # ~200 KB
MAX_EDGE = 1024
JPEG_QUALITY = 85

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "images")
MANIFEST_PATH = os.path.join(HERE, "manifest.json")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "smarthome-vision-latency-test/0.1 (local research)",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def normalize(raw: bytes) -> bytes:
    """Re-encode as JPEG <= TARGET_BYTES, capping long edge at MAX_EDGE."""
    from PIL import Image
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    if max(img.size) > MAX_EDGE:
        scale = MAX_EDGE / max(img.size)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    # Iteratively lower quality until we fit the target envelope.
    for q in (JPEG_QUALITY, 80, 75, 70, 65, 60):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True)
        data = buf.getvalue()
        if len(data) <= TARGET_BYTES * 1.15:  # within 15% of target is good enough
            return data
    return data


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    manifest = []
    for i, url in enumerate(SOURCES, start=1):
        name = f"img-{i:02d}.jpg"
        path = os.path.join(OUT_DIR, name)
        print(f"[{i:02d}/{len(SOURCES)}] {url}")
        try:
            raw = fetch(url)
        except Exception as e:
            print(f"  fetch failed: {e}")
            continue
        try:
            out = normalize(raw)
        except Exception as e:
            print(f"  normalize failed: {e}")
            continue
        with open(path, "wb") as f:
            f.write(out)
        sha = hashlib.sha256(out).hexdigest()
        manifest.append({
            "name": name,
            "source": url,
            "bytes": len(out),
            "sha256": sha,
        })
        print(f"  -> {len(out):,} bytes, sha {sha[:12]}")
        time.sleep(0.5)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\n{len(manifest)} images, manifest at {MANIFEST_PATH}")


if __name__ == "__main__":
    main()
