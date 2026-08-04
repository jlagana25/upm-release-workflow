"""Shared, automatically invalidated table cache for one workflow process."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


@dataclass
class ReleaseManifest:
    """Read exported CSV/XLSX tables once until their on-disk version changes."""

    _tables: dict[Path, tuple[tuple[int, int], pd.DataFrame]] = field(
        default_factory=dict
    )

    def read(self, path: Path) -> pd.DataFrame:
        resolved = Path(path)
        stat = resolved.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = self._tables.get(resolved)
        if cached and cached[0] == signature:
            return cached[1]
        if resolved.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
            frame = pd.read_excel(resolved, dtype=str).fillna("")
        else:
            frame = pd.read_csv(
                resolved, dtype=str, encoding="utf-8-sig"
            ).fillna("")
        self._tables[resolved] = (signature, frame)
        return frame

    def invalidate(self, path: Path | None = None) -> None:
        if path is None:
            self._tables.clear()
        else:
            self._tables.pop(Path(path), None)


MANIFEST = ReleaseManifest()


def read_table(path: Path) -> pd.DataFrame:
    """Read a workflow table through the shared release manifest cache."""
    return MANIFEST.read(path)


def invalidate_table(path: Path | None = None) -> None:
    """Invalidate one cached export, or the complete process-local manifest."""
    MANIFEST.invalidate(path)
