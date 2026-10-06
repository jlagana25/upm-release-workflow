#!/usr/bin/env python3
"""Adopt a completed standalone monthly release into its rolling owner.

This is a metadata/filesystem ownership migration only. It never rebuilds a
package or submits an endpoint delivery.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from config import LOGS_DIR, ReleaseContext
from delivery_state import partner_status
from logging_utils import get_logger


_COMPLETED_EMAIL_PARTNERS = ("qwire", "scripps")
_MONTHLY_PACKAGE_MARKERS = (
    "japan ntt data",
    "jmd and tss",
    "qwire",
    "scripps",
)


class MonthlyMigrationError(RuntimeError):
    """A completed monthly release cannot be migrated safely."""


def _replace_strings(value: Any, replacements: tuple[tuple[str, str], ...]) -> Any:
    if isinstance(value, str):
        for old, new in replacements:
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_replace_strings(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_strings(item, replacements)
            for key, item in value.items()
        }
    return value


def _load_rewritten_json(
    roots: tuple[Path, ...], replacements: tuple[tuple[str, str], ...]
) -> dict[Path, tuple[str, str]]:
    """Return original/transformed JSON text, validating every file first."""
    rewritten: dict[Path, tuple[str, str]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.json")):
            try:
                original = path.read_text(encoding="utf-8")
                payload = json.loads(original)
            except (OSError, json.JSONDecodeError) as exc:
                raise MonthlyMigrationError(f"Invalid migration JSON {path}: {exc}") from exc
            transformed = json.dumps(
                _replace_strings(payload, replacements),
                indent=2,
                sort_keys=True,
            ) + "\n"
            rewritten[path] = (original, transformed)
    return rewritten


def _write_private_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.monthly-migration.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _validate_completed_source(source_root: Path) -> None:
    if not source_root.is_dir():
        raise MonthlyMigrationError(f"Completed monthly root is missing: {source_root}")
    if source_root.is_symlink():
        raise MonthlyMigrationError(f"Completed monthly root is a symlink: {source_root}")
    final_root = source_root / "3-FINAL PACKAGING"
    if not final_root.is_dir():
        raise MonthlyMigrationError(f"Final Packaging is missing: {final_root}")
    package_names = [path.name.casefold() for path in final_root.iterdir() if path.is_dir()]
    missing = [
        marker for marker in _MONTHLY_PACKAGE_MARKERS
        if not any(marker in name for name in package_names)
    ]
    if missing:
        raise MonthlyMigrationError(
            "Completed monthly root is missing package(s): " + ", ".join(missing)
        )
    for marker in _MONTHLY_PACKAGE_MARKERS:
        packages = [path for path in final_root.iterdir() if path.is_dir() and marker in path.name.casefold()]
        if not any(file.is_file() and file.stat().st_size > 0 for package in packages for file in package.rglob("*")):
            raise MonthlyMigrationError(f"Monthly package is empty: {marker}")
    for partner in _COMPLETED_EMAIL_PARTNERS:
        if partner_status(source_root, partner) != "delivered":
            raise MonthlyMigrationError(f"{partner} is not recorded as delivered")
        receipt = source_root / "_WORKFLOW" / f"{partner}_delivery_receipt.json"
        if not receipt.is_file():
            raise MonthlyMigrationError(f"{partner} delivery receipt is missing: {receipt}")


def adopt_completed_monthly_release(
    ctx: ReleaseContext,
    *,
    source_root: Path | None = None,
    logs_dir: Path = LOGS_DIR,
    dry_run: bool = False,
    logger: logging.Logger,
) -> Path:
    """Move a completed monthly root and reports under the rolling batch ID."""
    if not ctx.is_monthly_delivery or not ctx.monthly_rolls_into_batch:
        raise MonthlyMigrationError("Migration requires a rolling-owned monthly context")
    delivery_date = date.fromisoformat(ctx.monthly_metadata_delivery_date)
    source = Path(source_root) if source_root else (
        ctx.specials_dir.parent / f"UPM-{delivery_date.strftime('%Y-%m')}-MONTHLY"
    )
    target = ctx.specials_dir
    old_release_id = source.name
    if source == target:
        raise MonthlyMigrationError("Source and rolling target are identical")
    if target.exists():
        raise MonthlyMigrationError(f"Rolling target already exists: {target}")
    _validate_completed_source(source)

    source_reports = Path(logs_dir) / "reports" / old_release_id
    target_reports = Path(logs_dir) / "reports" / ctx.release_id
    if target_reports.exists():
        raise MonthlyMigrationError(f"Rolling report target already exists: {target_reports}")
    replacements = (
        (str(source), str(target)),
        (old_release_id, ctx.release_id),
    )
    json_text = _load_rewritten_json((source / "_WORKFLOW", source_reports), replacements)
    logger.info("  Completed monthly source: %s", source)
    logger.info("  Rolling owner target: %s", target)
    if dry_run:
        logger.info(
            "  [DRY RUN] Would move the root, preserve %d JSON records, and send nothing",
            len(json_text),
        )
        return target

    moved_reports = False
    source.rename(target)
    try:
        if source_reports.is_dir():
            source_reports.rename(target_reports)
            moved_reports = True
        for old_path, (_, transformed) in json_text.items():
            if old_path.is_relative_to(source):
                new_path = target / old_path.relative_to(source)
            elif old_path.is_relative_to(source_reports):
                new_path = target_reports / old_path.relative_to(source_reports)
            else:  # pragma: no cover - roots above constrain this
                raise MonthlyMigrationError(f"Unexpected migration path: {old_path}")
            _write_private_text(new_path, transformed)

        workflow = target / "_WORKFLOW"
        marker = {
            "schema_version": 1,
            "delivery_date": ctx.monthly_metadata_delivery_date,
            "content_start": ctx.release_start,
            "content_end": ctx.release_end,
            "rolling_start": ctx.monthly_rolling_owner_start,
            "rolling_end": ctx.monthly_rolling_owner_end,
            "adopted_from": old_release_id,
        }
        _write_private_text(
            workflow / "early_monthly_build.json",
            json.dumps(marker, indent=2, sort_keys=True) + "\n",
        )
        audit = {
            "schema_version": 1,
            "migrated_at": datetime.now(timezone.utc).isoformat(),
            "source_release_id": old_release_id,
            "target_release_id": ctx.release_id,
            "source_root": str(source),
            "target_root": str(target),
            "endpoint_submissions_performed": False,
            "preserved_delivery_statuses": list(_COMPLETED_EMAIL_PARTNERS),
        }
        _write_private_text(
            workflow / "monthly_release_migration.json",
            json.dumps(audit, indent=2, sort_keys=True) + "\n",
        )
    except Exception:
        # Restore original JSON before returning the roots to their old names.
        for old_path, (original, _) in json_text.items():
            if old_path.is_relative_to(source):
                current = target / old_path.relative_to(source)
            else:
                current = target_reports / old_path.relative_to(source_reports)
            if current.parent.exists():
                _write_private_text(current, original)
        if moved_reports and target_reports.exists() and not source_reports.exists():
            target_reports.rename(source_reports)
        if target.exists() and not source.exists():
            target.rename(source)
        raise

    logger.info("  ✓ Adopted completed monthly release into %s; no endpoint was resent", ctx.release_id)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Adopt an already-completed monthly release into its rolling batch"
    )
    parser.add_argument("--delivery-date", required=True, help="monthly run date (YYYY-MM-01)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    ctx = ReleaseContext.for_monthly_delivery(args.delivery_date)
    logger, _ = get_logger(ctx.year, ctx.month, 1, release_label="MONTHLY-MIGRATION")
    try:
        adopt_completed_monthly_release(ctx, dry_run=args.dry_run, logger=logger)
    except MonthlyMigrationError as exc:
        logger.error("  ✗ Monthly release migration failed closed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
