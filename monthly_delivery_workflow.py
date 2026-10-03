#!/usr/bin/env python3
"""Standalone first-of-month workflow for NTT, JMD/TSS, Qwire, and Scripps."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime
from pathlib import Path

from config import LOGS_DIR, ReleaseContext, VOLUMES
from logging_utils import get_logger, log_section


MONTHLY_DOMO_KEYS = (
    "japan_metadata",
    "japan_jmdtss_metadata",
    "qwire_metadata",
    "scripps_metadata",
)


def _preflight(ctx: ReleaseContext, dry_run: bool, logger: logging.Logger) -> bool:
    from auth_manager import auth_status, secure_auth_permissions
    from volume_mounts import ensure_workflow_volumes

    ok = ensure_workflow_volumes(logger)
    specials_volume = VOLUMES["R8_1"]
    if not specials_volume.is_dir():
        logger.error("  ✗ Monthly Specials volume is unavailable: %s", specials_volume)
        ok = False
    secure_auth_permissions()
    auth = auth_status()
    for key, label in (("domo", "Domo"), ("unisync", "UniSync"), ("monday", "Monday")):
        configured = auth[key]["state"] == "configured"
        if key == "domo":
            configured = configured and bool(auth[key]["keychain_credentials_present"])
        if configured:
            logger.info("  ✓ %s unattended authentication configured", label)
        else:
            logger.error("  ✗ %s unattended authentication is not configured", label)
            ok = False
    if not Path("/Applications/UniSync.app").is_dir():
        logger.error("  ✗ UniSync is not installed")
        ok = False
    if dry_run:
        logger.info("  [DRY RUN] Preflight made no workflow changes")
    return ok


def _metadata_files(ctx: ReleaseContext) -> tuple[Path, ...]:
    return (
        ctx.japan_metadata_csv,
        ctx.partner_metadata["japan_jmdtss"],
        ctx.partner_metadata["qwire"],
        ctx.partner_metadata["scripps"],
    )


def _verify_monthly_sources(
    ctx: ReleaseContext, *, dry_run: bool, logger: logging.Logger
) -> bool:
    if dry_run and not ctx.specials_dir.exists():
        logger.info("  [DRY RUN] Would verify all four metadata files and NTT audio")
        return True
    missing_metadata = [
        path for path in _metadata_files(ctx)
        if not path.is_file() or path.stat().st_size <= 0
    ]
    for path in missing_metadata:
        logger.error("  ✗ Missing or empty monthly metadata: %s", path)
    from verification import _verify_japan
    japan_missing = _verify_japan(ctx, logger)
    return not missing_metadata and not japan_missing


def _write_report(
    ctx: ReleaseContext,
    *,
    dry_run: bool,
    started_at: datetime,
    steps: dict[str, str],
    log_path: Path,
) -> Path:
    finished_at = datetime.now().astimezone()
    report_dir = LOGS_DIR / "reports" / ctx.release_id
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / f"run-{finished_at.strftime('%Y%m%d-%H%M%S')}.json"
    payload = {
        "schema_version": 1,
        "workflow": "monthly-delivery",
        "release": {
            "id": ctx.release_id,
            "monday_batch": ctx.monthly_monday_batch,
            "delivery_month": ctx.monthly_metadata_display_folder,
            "content_start": ctx.release_start,
            "content_end": ctx.release_end,
        },
        "run": {
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "dry_run": dry_run,
            "overall": "failed" if "failed" in steps.values() else "completed",
        },
        "steps": steps,
        "artifacts": {"log": str(log_path), "root": str(ctx.specials_dir)},
    }
    temporary = report.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(report)
    report.chmod(0o600)
    return report


def run_monthly_delivery(
    ctx: ReleaseContext,
    *,
    dry_run: bool,
    overwrite: bool,
    skip_monday: bool,
    logger: logging.Logger,
) -> dict[str, str]:
    if not ctx.is_monthly_delivery:
        raise ValueError("run_monthly_delivery requires a monthly context")
    steps: dict[str, str] = {}
    monday_started = False

    def finish() -> dict[str, str]:
        if skip_monday or dry_run or not monday_started:
            return steps
        from monday_sync import run_monday_sync
        failed = "failed" in steps.values()
        monday_results = {
            "10 Final packaging": (
                "failed" if failed else steps.get("final_packaging", "failed")
            ),
            "15 Final metadata check": (
                "failed" if failed else steps.get("final_verification", "failed")
            ),
        }
        monday_ok = run_monday_sync(
            ctx, monday_results, dry_run=False, logger=logger, include_history=False
        )
        steps["monday_final"] = "completed" if monday_ok else "failed"
        return steps

    log_section(logger, "Monthly delivery preflight")
    if not _preflight(ctx, dry_run, logger):
        steps["preflight"] = "failed"
        return steps
    steps["preflight"] = "completed"

    from folder_setup import create_monthly_delivery_folder
    if not create_monthly_delivery_folder(ctx, dry_run, logger):
        steps["folder_setup"] = "failed"
        return steps
    steps["folder_setup"] = "completed"

    if not skip_monday:
        from monday_sync import ensure_monday_monthly_batch, run_monday_sync
        if not ensure_monday_monthly_batch(ctx, dry_run=dry_run, logger=logger):
            steps["monday_start"] = "failed"
            return steps
        steps["monday_start"] = "completed"
        monday_started = True
        if not dry_run and not run_monday_sync(
            ctx, {}, dry_run=False, logger=logger, include_history=False
        ):
            steps["monday_start"] = "failed"
            return steps

    from domo_exports import run_domo_exports
    domo = run_domo_exports(
        ctx, dry_run, logger, only_keys=list(MONTHLY_DOMO_KEYS)
    )
    domo_ok = dry_run or all(domo.get(key) == "ok" for key in MONTHLY_DOMO_KEYS)
    steps["domo_exports"] = "completed" if domo_ok else "failed"
    if not domo_ok:
        return finish()

    from unisync_automation import STATUS_FAILED, run_all_unisync_jobs
    unisync = run_all_unisync_jobs(
        ctx, dry_run, logger, overwrite=overwrite, allow_domo_refresh=True
    )
    unisync_ok = not any(status == STATUS_FAILED for status in unisync.values())
    steps["ntt_audio"] = "completed" if unisync_ok else "failed"
    if not unisync_ok:
        return finish()

    sources_ok = _verify_monthly_sources(ctx, dry_run=dry_run, logger=logger)
    steps["source_verification"] = "completed" if sources_ok else "failed"
    if not sources_ok:
        return finish()

    from final_packaging import copy_originals_to_finals
    packaged = copy_originals_to_finals(
        ctx, dry_run, logger, overwrite=overwrite
    )
    steps["final_packaging"] = "completed" if packaged else "failed"
    if not packaged:
        return finish()

    from final_metadata_verification import verify_final_packaging_metadata
    verified = verify_final_packaging_metadata(ctx, logger, dry_run)
    steps["final_verification"] = "completed" if verified else "failed"

    return finish()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the independent monthly NTT/JMD/Qwire/Scripps delivery"
    )
    parser.add_argument(
        "--delivery-date",
        help="first day after the named content month (YYYY-MM-01); defaults to today",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--skip-monday", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    ctx = ReleaseContext.for_monthly_delivery(args.delivery_date or date.today())
    logger, log_path = get_logger(
        ctx.year, ctx.month, 1, release_label="MONTHLY"
    )
    started_at = datetime.now().astimezone()
    logger.info(
        "Monthly delivery %s: content %s through %s",
        ctx.monthly_metadata_display_folder,
        ctx.release_start,
        ctx.release_end,
    )
    try:
        steps = run_monthly_delivery(
            ctx,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
            skip_monday=args.skip_monday,
            logger=logger,
        )
    except Exception:
        logger.exception("Monthly delivery failed unexpectedly")
        steps = {"unexpected_error": "failed"}
    report = _write_report(
        ctx,
        dry_run=args.dry_run,
        started_at=started_at,
        steps=steps,
        log_path=log_path,
    )
    logger.info("Monthly report: %s", report)
    return 1 if "failed" in steps.values() else 0


if __name__ == "__main__":
    raise SystemExit(main())
