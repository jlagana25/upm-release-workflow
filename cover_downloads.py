"""Shared atomic cover download and cache helpers."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

import requests


def _looks_like_image(path: Path) -> bool:
    with path.open("rb") as handle:
        head = handle.read(16)
    return (
        head.startswith(b"\xff\xd8\xff")
        or head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith((b"GIF87a", b"GIF89a", b"BM"))
        or head.startswith((b"II*\x00", b"MM\x00*"))
        or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")
    )


def download_image_atomic(
    url: str,
    destination: Path,
    *,
    timeout: int,
    session: Any | None = None,
) -> None:
    """Download and validate an image before atomically replacing its target."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.with_name(
        f".{destination.name}.new-{uuid.uuid4().hex}"
    )
    client = session or requests
    try:
        response = client.get(url, stream=True, timeout=timeout)
        response.raise_for_status()
        with staged.open("wb") as handle:
            for chunk in response.iter_content(8192):
                if chunk:
                    handle.write(chunk)
        if staged.stat().st_size == 0 or not _looks_like_image(staged):
            raise ValueError("downloaded content is not a recognized image")
        staged.replace(destination)
    finally:
        if staged.exists():
            staged.unlink()


def find_cached_cover(cache_root: Path, filename: str) -> Path | None:
    """Find an exact cover basename beneath the shared master cache."""
    if not cache_root.is_dir():
        return None
    return next(
        (path for path in cache_root.rglob(filename) if path.is_file()), None
    )


def copy_cached_cover(source: Path, destination: Path) -> None:
    """Copy a cached image through a sibling staging file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.with_name(
        f".{destination.name}.cache-{uuid.uuid4().hex}"
    )
    try:
        shutil.copy2(source, staged)
        staged.replace(destination)
    finally:
        if staged.exists():
            staged.unlink()
