"""Shared normalization for label directory names in delivery trees."""

from __future__ import annotations

from pathlib import Path


def normalize_label(value: object) -> str:
    """Return a case-insensitive, whitespace-stripped label key."""
    return str(value or "").strip().casefold()


def resolve_label_dir(root: Path, label: object) -> Path:
    """Resolve an existing immediate child after normalizing its label name.

    Exact clean paths win. If no matching directory exists, return the clean
    expected path so callers can report or create it.
    """
    clean = str(label or "").strip()
    exact = root / clean
    if exact.is_dir():
        return exact
    if root.is_dir():
        wanted = normalize_label(clean)
        try:
            matches = sorted(
                (
                    child for child in root.iterdir()
                    if child.is_dir() and normalize_label(child.name) == wanted
                ),
                key=lambda child: child.name.casefold(),
            )
        except OSError:
            matches = []
        if matches:
            return matches[0]
    return exact
