#!/usr/bin/env python3
"""Guarded ESPN Media Shuttle folder delivery.

The orchestration is fully testable through ``EspnGateway``.  The Playwright
adapter intentionally uses a retained private browser profile and performs no
credential entry; an unauthenticated page fails closed.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path
from typing import Protocol

from config import PRIVATE_STATE_DIR, ReleaseContext, context_from_cli_args
from delivery_common import (
    DeliverySafetyError,
    checkpoint,
    collect_manifest,
    latest_workflow_gates,
    receipt,
    require_live_authorization,
)
from delivery_state import set_partner_status


PORTAL_URL = "https://espn-file-transfers-shr.mediashuttle.com/memberLogin"
DESTINATION = "from_killer_tracks"


class EspnDeliveryError(DeliverySafetyError):
    pass


class EspnGateway(Protocol):
    def require_authenticated(self) -> None: ...
    def transfer_status(self, folder_name: str) -> str | None: ...
    def destination_contains(self, folder_name: str) -> bool: ...
    def start_folder_upload(self, package: Path, destination: str) -> None: ...
    def resume_transfer(self, folder_name: str) -> None: ...
    def close(self) -> None: ...


class PlaywrightEspnGateway:
    """Media Shuttle browser adapter; imported and launched only for live use."""

    def __init__(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise EspnDeliveryError("ESPN delivery requires Playwright") from exc
        profile = PRIVATE_STATE_DIR / "espn_media_shuttle_profile"
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=False
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.goto(PORTAL_URL, wait_until="domcontentloaded")

    def require_authenticated(self) -> None:
        if self._page.locator("input[type=password]").count():
            raise EspnDeliveryError("ESPN portal session is not authenticated")
        if "memberLogin" in self._page.url:
            raise EspnDeliveryError("ESPN portal did not leave the login route")

    def _open_transfers(self) -> None:
        self._page.get_by_text("My Transfers", exact=True).click()

    def transfer_status(self, folder_name: str) -> str | None:
        self._open_transfers()
        row = self._page.locator("tr", has_text=folder_name).first
        if not row.count():
            return None
        text = " ".join(row.inner_text().split())
        lowered = text.casefold()
        if "uploaded 1 file" in lowered:
            return "uploaded"
        if "interrupted" in lowered:
            return "interrupted"
        if any(value in lowered for value in ("uploading", "queued", "processing")):
            return "active"
        if "failed" in lowered or "error" in lowered:
            return "failed"
        return "unknown"

    def destination_contains(self, folder_name: str) -> bool:
        self._page.get_by_text(DESTINATION, exact=True).click()
        return bool(self._page.get_by_text(folder_name, exact=True).count())

    def start_folder_upload(self, package: Path, destination: str) -> None:
        self._page.get_by_text(destination, exact=True).click()
        self._page.get_by_text("Upload", exact=True).click()
        picker = self._page.locator("input[type=file][webkitdirectory]")
        if picker.count() != 1:
            raise EspnDeliveryError("ESPN folder-upload input was not uniquely available")
        picker.set_input_files(str(package))

    def resume_transfer(self, folder_name: str) -> None:
        self._open_transfers()
        row = self._page.locator("tr", has_text=folder_name).first
        button = row.get_by_text("Resume", exact=True)
        if button.count() != 1:
            raise EspnDeliveryError("Interrupted ESPN transfer has no unique Resume action")
        button.click()

    def close(self) -> None:
        self._context.close()
        self._pw.stop()


def package_root(ctx: ReleaseContext) -> Path:
    return ctx.specials_dir / "3-FINAL PACKAGING" / ctx.partner_folder_name("ESPN")


def deliver_espn(
    ctx: ReleaseContext,
    dry_run: bool,
    logger: logging.Logger,
    *,
    gateway: EspnGateway | None = None,
    live_confirmation: str | None = None,
    timeout_hours: float = 24,
    poll_seconds: float = 15,
) -> bool:
    owned = False
    try:
        package = package_root(ctx)
        files = collect_manifest(package, include_root=True)
        if package.name != ctx.partner_folder_name("ESPN"):
            raise EspnDeliveryError("ESPN package name is not canonical")
        logger.info("  ESPN package: %d file(s)", len(files))
        if dry_run:
            logger.info("  [DRY RUN] Would upload one folder to %s", DESTINATION)
            return True
        require_live_authorization(ctx, live_confirmation)
        gate_ok, detail = latest_workflow_gates(
            ctx, ("10 Final packaging", "15 Final metadata check")
        )
        if not gate_ok:
            raise EspnDeliveryError(f"ESPN upload is blocked: {detail}")
        if gateway is None:
            gateway = PlaywrightEspnGateway()
            owned = True
        gateway.require_authenticated()
        status = gateway.transfer_status(package.name)
        visible = gateway.destination_contains(package.name)
        if status == "uploaded" and visible:
            logger.info("  ESPN folder was already uploaded and verified")
        else:
            if visible:
                raise EspnDeliveryError(
                    "Exact ESPN destination folder exists without a verified completed transfer"
                )
            if status == "interrupted":
                checkpoint(ctx, "espn", files, "submitted", "resuming interrupted transfer")
                gateway.resume_transfer(package.name)
            elif status in {None, "failed"}:
                checkpoint(ctx, "espn", files, "submitted", "folder upload initiated")
                gateway.start_folder_upload(package, DESTINATION)
            elif status not in {"active"}:
                raise EspnDeliveryError(f"Unrecognized existing ESPN transfer state: {status}")
            deadline = time.monotonic() + max(timeout_hours, 0.01) * 3600
            while time.monotonic() < deadline:
                status = gateway.transfer_status(package.name)
                if status == "uploaded":
                    break
                if status in {"failed", "interrupted", "unknown", None}:
                    raise EspnDeliveryError(f"ESPN transfer did not complete: {status}")
                time.sleep(max(poll_seconds, 0.01))
            else:
                raise EspnDeliveryError("ESPN upload timed out")
            if not gateway.destination_contains(package.name):
                raise EspnDeliveryError("ESPN completed transfer is absent from destination")
        checkpoint(ctx, "espn", files, "verified", "history and destination verified")
        path = receipt(ctx, "espn", files, {
            "destination": DESTINATION,
            "folder": package.name,
            "history_result": "Uploaded 1 file(s)",
        })
        set_partner_status(ctx.specials_dir, "espn", "uploaded")
        logger.info("  ✓ ESPN upload verified: %s", path)
        return True
    except Exception as exc:
        logger.error("  ✗ ESPN delivery failed: %s", exc)
        return False
    finally:
        if owned and gateway is not None:
            gateway.close()


def _main() -> int:
    parser = argparse.ArgumentParser(description="Deliver the ESPN folder through Media Shuttle")
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--part", type=int, choices=(1, 2))
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--full-month-content", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-live-release")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return 0 if deliver_espn(
        context_from_cli_args(args), args.dry_run, logging.getLogger("espn"),
        live_confirmation=args.confirm_live_release,
    ) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
