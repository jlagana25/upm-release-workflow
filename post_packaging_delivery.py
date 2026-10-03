#!/usr/bin/env python3
"""Unified post-packaging delivery runner.

The runner selects endpoints, enforces exact-batch live authorization, invokes
their standalone adapters, and keeps remote upload separate from confirmed
delivery.  Planning/dry-run never opens browsers, applications, connectors, or
network sessions.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Callable, Iterable

from config import (
    ReleaseContext,
    context_from_cli_args,
    monthly_metadata_delivery_ready,
)
from delivery_common import DeliverySafetyError, require_live_authorization
from delivery_state import partner_status, set_partner_status


Runner = Callable[[ReleaseContext, bool, logging.Logger], bool]
ProgressSync = Callable[[ReleaseContext, logging.Logger], bool]


@dataclass(frozen=True)
class Endpoint:
    key: str
    outcome: str
    availability: str
    description: str


ENDPOINTS = {
    "espn": Endpoint("espn", "delivered", "implemented", "Media Shuttle folder upload"),
    "soundexchange": Endpoint("soundexchange", "delivered", "implemented", "two registrant submissions"),
    "qwire": Endpoint("qwire", "connector_handoff_prepared", "implemented", "Outlook connector email"),
    "scripps": Endpoint("scripps", "connector_handoff_prepared", "implemented", "Outlook connector email"),
    "soundmouse": Endpoint("soundmouse", "uploaded", "implemented", "UPPM/Music native uploader; website metadata processing remains"),
    "synchtank": Endpoint("synchtank", "delivered", "implemented", "S3 inbox with final trigger"),
    "tunesat": Endpoint("tunesat", "delivered", "implemented", "complete-package SFTP"),
    "discovery": Endpoint("discovery", "delivered", "waiting_credentials", "Sony Ci + MediaBox"),
    "japan_ntt": Endpoint("japan_ntt", "delivered", "waiting_credentials", "Sony Ci + MediaBox"),
    "japan_jmdtss": Endpoint("japan_jmdtss", "delivered", "waiting_credentials", "Sony Ci + MediaBox"),
    "hd_updates": Endpoint("hd_updates", "delivered", "waiting_credentials", "Sony Ci MP3/WAV MediaBoxes"),
    "netmix": Endpoint("netmix", "delivered", "implemented", "API-priority CND portal fallback"),
    "sourceaudio": Endpoint("sourceaudio", "delivered", "separate_worktree", "SourceAudio US API"),
    "sourceaudio_exus": Endpoint("sourceaudio_exus", "delivered", "separate_worktree", "SourceAudio Ex-US API"),
}

MONTHLY_METADATA_ENDPOINTS = frozenset({
    "japan_ntt",
    "japan_jmdtss",
    "qwire",
    "scripps",
})


class PostPackagingError(DeliverySafetyError):
    pass


def _sync_monday_progress(ctx: ReleaseContext, logger: logging.Logger) -> bool:
    """Push the current verified delivery ledger to Monday after each mutation."""
    from monday_sync import run_monday_sync

    return run_monday_sync(
        ctx,
        {},
        dry_run=False,
        logger=logger,
        include_history=True,
    )


def _implemented_runners(
    *,
    live_confirmation: str | None,
    outlook_gateway=None,
    signature: str = "",
    interactive_login: bool = False,
    interactive_native_selection: bool = False,
) -> dict[str, Runner]:
    # All imports are lazy so planning remains headless and dependency-light.
    from email_deliveries import deliver_email
    from espn_delivery import deliver_espn
    from netmix_portal_delivery import deliver_netmix
    from soundexchange_delivery import deliver_soundexchange
    from soundmouse_uploader_delivery import deliver_soundmouse_uploader
    from synchtank_delivery import deliver_synchtank
    from tunesat_delivery import deliver_tunesat
    from outlook_connector_bridge import prepare_handoff

    def email_runner(endpoint: str) -> Runner:
        def run(ctx: ReleaseContext, dry: bool, log: logging.Logger) -> bool:
            if dry:
                return deliver_email(ctx, endpoint, True, log, signature=signature)
            path, _plan = prepare_handoff(ctx, endpoint)
            log.info("  ✓ %s Outlook connector handoff prepared: %s", endpoint, path)
            return True
        return run

    return {
        "espn": lambda ctx, dry, log: deliver_espn(
            ctx, dry, log, live_confirmation=live_confirmation,
            interactive_native_selection=interactive_native_selection,
        ),
        "soundexchange": lambda ctx, dry, log: deliver_soundexchange(
            ctx, dry, log, live_confirmation=live_confirmation,
            interactive_login=interactive_login,
        ),
        "qwire": email_runner("qwire"),
        "scripps": email_runner("scripps"),
        "netmix": lambda ctx, dry, log: deliver_netmix(
            ctx, dry, log, live_confirmation=live_confirmation,
        ),
        "soundmouse": deliver_soundmouse_uploader,
        "synchtank": deliver_synchtank,
        "tunesat": deliver_tunesat,
    }


def select_endpoints(raw: str) -> tuple[str, ...]:
    values = tuple(ENDPOINTS) if raw.strip().casefold() == "all" else tuple(
        dict.fromkeys(item.strip().casefold() for item in raw.split(",") if item.strip())
    )
    unknown = sorted(set(values) - set(ENDPOINTS))
    if unknown:
        raise PostPackagingError("Unknown endpoint(s): " + ", ".join(unknown))
    if not values:
        raise PostPackagingError("Select at least one endpoint")
    return values


def run_deliveries(
    ctx: ReleaseContext,
    endpoints: Iterable[str],
    logger: logging.Logger,
    *,
    dry_run: bool = True,
    live_confirmation: str | None = None,
    runners: dict[str, Runner] | None = None,
    interactive_login: bool = False,
    interactive_native_selection: bool = False,
    progress_sync: ProgressSync | None = None,
) -> dict[str, str]:
    """Run or plan selected endpoints without allowing duplicate final sends."""
    selected = tuple(dict.fromkeys(endpoints))
    if not dry_run:
        require_live_authorization(ctx, live_confirmation)
    if runners is None:
        runners = _implemented_runners(
            live_confirmation=live_confirmation,
            interactive_login=interactive_login,
            interactive_native_selection=interactive_native_selection,
        )
    if progress_sync is None:
        progress_sync = _sync_monday_progress
    results: dict[str, str] = {}
    for key in selected:
        endpoint = ENDPOINTS.get(key)
        if endpoint is None:
            raise PostPackagingError(f"Unknown endpoint: {key}")
        if (
            key in MONTHLY_METADATA_ENDPOINTS
            and not getattr(ctx, "monthly_metadata_due", True)
        ):
            logger.info("  – %s: monthly metadata is not due in this run", key)
            results[key] = "not_due"
            continue
        if (
            key in MONTHLY_METADATA_ENDPOINTS
            and hasattr(ctx, "monthly_metadata_delivery_date")
            and not monthly_metadata_delivery_ready(ctx)
        ):
            logger.info(
                "  – %s: held until monthly delivery date %s",
                key,
                ctx.monthly_metadata_delivery_date,
            )
            results[key] = "scheduled_for_month_start"
            continue
        current = partner_status(ctx.specials_dir, key)
        if current == "delivered":
            logger.info("  ↩ %s already delivered; skipping duplicate submission", key)
            results[key] = "already_delivered"
            continue
        if key == "soundmouse" and current == "uploaded":
            logger.info(
                "  ⏸ soundmouse: native upload is complete; website metadata "
                "processing with zero errors is still required"
            )
            results[key] = "awaiting_metadata_processing"
            continue
        runner = runners.get(key)
        if runner is None:
            logger.info("  ⏸ %s: %s", key, endpoint.availability)
            results[key] = endpoint.availability
            continue
        ok = bool(runner(ctx, dry_run, logger))
        results[key] = "planned" if dry_run and ok else endpoint.outcome if ok else "failed"
        if ok and not dry_run:
            monday_ok = progress_sync(ctx, logger)
            results["monday"] = "completed" if monday_ok else "failed"
    return results


def acknowledge_delivered(
    ctx: ReleaseContext,
    endpoints: Iterable[str],
    confirmation: str | None,
    *,
    logger: logging.Logger | None = None,
    progress_sync: ProgressSync | None = None,
) -> dict[str, str]:
    """Promote legacy verified-upload records to the current delivered state."""
    require_live_authorization(ctx, confirmation)
    logger = logger or logging.getLogger("post_packaging_delivery")
    progress_sync = progress_sync or _sync_monday_progress
    changed: dict[str, str] = {}
    for key in tuple(dict.fromkeys(endpoints)):
        endpoint = ENDPOINTS.get(key)
        if endpoint is None:
            raise PostPackagingError(f"Unknown endpoint: {key}")
        current = partner_status(ctx.specials_dir, key)
        if current == "delivered":
            changed[key] = "already_delivered"
            continue
        if key == "soundmouse":
            raise PostPackagingError(
                "Cannot acknowledge soundmouse from an uploader receipt; "
                "the website metadata processor must verify every workbook "
                "with zero errors"
            )
        if current != "uploaded":
            raise PostPackagingError(
                f"Cannot acknowledge {key}: expected uploaded, found {current}"
            )
        receipt_path = ctx.specials_dir / "_WORKFLOW" / f"{key}_delivery_receipt.json"
        if not receipt_path.is_file():
            raise PostPackagingError(f"Cannot acknowledge {key}: verified receipt is missing")
        set_partner_status(ctx.specials_dir, key, "delivered")
        changed[key] = "delivered"
        changed["monday"] = (
            "completed" if progress_sync(ctx, logger) else "failed"
        )
    return changed


def _main() -> int:
    parser = argparse.ArgumentParser(description="Run post-packaging partner deliveries")
    parser.add_argument("--endpoints", default="all", help="comma-separated endpoint keys or all")
    parser.add_argument("--execute", action="store_true", help="allow endpoint actions after authorization")
    parser.add_argument("--confirm-live-release")
    parser.add_argument("--acknowledge-delivered", action="store_true")
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--part", type=int, choices=(1, 2))
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--full-month-content", action="store_true")
    parser.add_argument(
        "--delivery-date",
        help="first day of the standalone monthly delivery month (YYYY-MM-01)",
    )
    parser.add_argument(
        "--interactive-login", action="store_true",
        help="allow a bounded one-session SoundExchange sign-in",
    )
    parser.add_argument(
        "--interactive-native-selection", action="store_true",
        help="wait for supervised ESPN Signiant selection instead of exact-path automation",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("post_packaging_delivery")
    try:
        ctx = context_from_cli_args(args)
        selected = select_endpoints(args.endpoints)
        if args.acknowledge_delivered:
            results = acknowledge_delivered(
                ctx, selected, args.confirm_live_release, logger=logger
            )
        else:
            results = run_deliveries(
                ctx, selected, logger, dry_run=not args.execute,
                live_confirmation=args.confirm_live_release,
                interactive_login=args.interactive_login,
                interactive_native_selection=args.interactive_native_selection,
            )
        for key, status in results.items():
            logger.info("%s: %s", key, status)
        return 0 if all(value not in {"failed"} for value in results.values()) else 1
    except DeliverySafetyError as exc:
        logger.error("Post-packaging delivery failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
