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

from config import ReleaseContext, context_from_cli_args
from delivery_common import DeliverySafetyError, require_live_authorization
from delivery_state import partner_status, set_partner_status


Runner = Callable[[ReleaseContext, bool, logging.Logger], bool]


@dataclass(frozen=True)
class Endpoint:
    key: str
    outcome: str
    availability: str
    description: str


ENDPOINTS = {
    "espn": Endpoint("espn", "uploaded", "implemented", "Media Shuttle folder upload"),
    "soundexchange": Endpoint("soundexchange", "delivered", "implemented", "two registrant submissions"),
    "qwire": Endpoint("qwire", "connector_handoff_prepared", "implemented", "Outlook connector email"),
    "scripps": Endpoint("scripps", "connector_handoff_prepared", "implemented", "Outlook connector email"),
    "soundmouse": Endpoint("soundmouse", "uploaded", "implemented", "UPPM/Music native uploader"),
    "synchtank": Endpoint("synchtank", "uploaded", "implemented", "S3 inbox with final trigger"),
    "tunesat": Endpoint("tunesat", "uploaded", "implemented", "complete-package SFTP"),
    "discovery": Endpoint("discovery", "uploaded", "waiting_credentials", "Sony Ci + MediaBox"),
    "japan_ntt": Endpoint("japan_ntt", "uploaded", "waiting_credentials", "Sony Ci + MediaBox"),
    "japan_jmdtss": Endpoint("japan_jmdtss", "uploaded", "waiting_credentials", "Sony Ci + MediaBox"),
    "hd_updates": Endpoint("hd_updates", "uploaded", "waiting_credentials", "Sony Ci MP3/WAV MediaBoxes"),
    "netmix": Endpoint("netmix", "uploaded", "waiting_credentials", "CND API contract and credentials"),
    "sourceaudio": Endpoint("sourceaudio", "uploaded", "separate_worktree", "SourceAudio US API"),
    "sourceaudio_exus": Endpoint("sourceaudio_exus", "uploaded", "separate_worktree", "SourceAudio Ex-US API"),
}


class PostPackagingError(DeliverySafetyError):
    pass


def _implemented_runners(
    *,
    live_confirmation: str | None,
    outlook_gateway=None,
    signature: str = "",
) -> dict[str, Runner]:
    # All imports are lazy so planning remains headless and dependency-light.
    from email_deliveries import deliver_email
    from espn_delivery import deliver_espn
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
            ctx, dry, log, live_confirmation=live_confirmation
        ),
        "soundexchange": lambda ctx, dry, log: deliver_soundexchange(
            ctx, dry, log, live_confirmation=live_confirmation
        ),
        "qwire": email_runner("qwire"),
        "scripps": email_runner("scripps"),
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
) -> dict[str, str]:
    """Run or plan selected endpoints without allowing duplicate final sends."""
    selected = tuple(dict.fromkeys(endpoints))
    if not dry_run:
        require_live_authorization(ctx, live_confirmation)
    if runners is None:
        runners = _implemented_runners(live_confirmation=live_confirmation)
    results: dict[str, str] = {}
    for key in selected:
        endpoint = ENDPOINTS.get(key)
        if endpoint is None:
            raise PostPackagingError(f"Unknown endpoint: {key}")
        current = partner_status(ctx.specials_dir, key)
        if current == "delivered":
            logger.info("  ↩ %s already delivered; skipping duplicate submission", key)
            results[key] = "already_delivered"
            continue
        runner = runners.get(key)
        if runner is None:
            logger.info("  ⏸ %s: %s", key, endpoint.availability)
            results[key] = endpoint.availability
            continue
        ok = bool(runner(ctx, dry_run, logger))
        results[key] = "planned" if dry_run and ok else endpoint.outcome if ok else "failed"
    return results


def acknowledge_delivered(
    ctx: ReleaseContext,
    endpoints: Iterable[str],
    confirmation: str | None,
) -> dict[str, str]:
    """Promote verified uploads only after an explicit downstream acknowledgement."""
    require_live_authorization(ctx, confirmation)
    changed: dict[str, str] = {}
    for key in tuple(dict.fromkeys(endpoints)):
        endpoint = ENDPOINTS.get(key)
        if endpoint is None:
            raise PostPackagingError(f"Unknown endpoint: {key}")
        current = partner_status(ctx.specials_dir, key)
        if current == "delivered":
            changed[key] = "already_delivered"
            continue
        if current != "uploaded":
            raise PostPackagingError(
                f"Cannot acknowledge {key}: expected uploaded, found {current}"
            )
        receipt_path = ctx.specials_dir / "_WORKFLOW" / f"{key}_delivery_receipt.json"
        # The existing standalone SoundMouse receipt includes its transport name.
        if key == "soundmouse" and not receipt_path.exists():
            receipt_path = ctx.specials_dir / "_WORKFLOW" / "soundmouse_uploader_receipt.json"
        if not receipt_path.is_file():
            raise PostPackagingError(f"Cannot acknowledge {key}: verified receipt is missing")
        set_partner_status(ctx.specials_dir, key, "delivered")
        changed[key] = "delivered"
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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("post_packaging_delivery")
    try:
        ctx = context_from_cli_args(args)
        selected = select_endpoints(args.endpoints)
        if args.acknowledge_delivered:
            results = acknowledge_delivered(ctx, selected, args.confirm_live_release)
        else:
            results = run_deliveries(
                ctx, selected, logger, dry_run=not args.execute,
                live_confirmation=args.confirm_live_release,
            )
        for key, status in results.items():
            logger.info("%s: %s", key, status)
        return 0 if all(value not in {"failed"} for value in results.values()) else 1
    except DeliverySafetyError as exc:
        logger.error("Post-packaging delivery failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
