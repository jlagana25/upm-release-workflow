#!/usr/bin/env python3
"""Guarded ESPN Media Shuttle folder delivery.

The orchestration is fully testable through ``EspnGateway``. The Playwright
adapter uses a retained private browser profile, recovers a fresh login from
workflow-owned Keychain items, and selects the exact package through Signiant's
native folder panel. Authentication or exact-path ambiguity fails closed.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
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
TRANSFER_PANEL_SELECTOR = ".activity-frame"
TRANSFER_RECORD_SELECTOR = ".activity-item"


class EspnDeliveryError(DeliverySafetyError):
    pass


def _select_native_folder(package: Path) -> None:
    """Select one exact package in Signiant's macOS NSOpenPanel."""
    if not package.is_dir():
        raise EspnDeliveryError(f"ESPN package folder is missing: {package}")
    try:
        import pyautogui
    except ImportError as exc:
        raise EspnDeliveryError(
            "ESPN native folder selection requires pyautogui on the workflow Mac"
        ) from exc

    time.sleep(3)
    pyautogui.hotkey("command", "shift", "g")
    time.sleep(1)
    pyautogui.hotkey("command", "a")
    copied = subprocess.run(
        ["/usr/bin/pbcopy"],
        input=str(package), text=True, check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if copied.returncode != 0:
        raise EspnDeliveryError("Could not place the exact ESPN path on the clipboard")
    pyautogui.hotkey("command", "v")
    time.sleep(0.5)
    pyautogui.press("enter")
    time.sleep(1.5)
    # Go-to-Folder opens a directory rather than selecting it. Moving back to
    # the parent retains that exact directory as the selected row, allowing the
    # panel's default action to choose it without typing a Unicode package name.
    pyautogui.hotkey("command", "up")
    time.sleep(1)
    pyautogui.press("enter")
    time.sleep(2)


class EspnGateway(Protocol):
    def require_authenticated(self) -> None: ...
    def transfer_status(self, folder_name: str) -> str | None: ...
    def destination_contains(self, folder_name: str) -> bool: ...
    def start_folder_upload(self, package: Path, destination: str) -> None: ...
    def resume_transfer(self, folder_name: str) -> None: ...
    def close(self) -> None: ...


class PlaywrightEspnGateway:
    """Media Shuttle browser adapter; imported and launched only for live use."""

    def __init__(self, *, interactive_native_selection: bool = False) -> None:
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
        self._interactive_native_selection = interactive_native_selection

    def require_authenticated(self) -> None:
        def ready() -> bool:
            # Media Shuttle keeps authenticated content on /memberLogin.  The
            # URL therefore cannot be used as an authentication signal.
            return bool(
                not self._page.locator("input[type=password]:visible").count()
                and self._page.get_by_text("My Transfers", exact=True).count()
                and self._page.get_by_text(DESTINATION, exact=True).count()
            )

        if ready():
            return
        from auth_manager import load_espn_credentials
        from portal_auth import PortalAuthenticationError, attempt_keychain_login
        try:
            authenticated = attempt_keychain_login(
                self._page, load_espn_credentials(), ready=ready
            )
        except PortalAuthenticationError as exc:
            raise EspnDeliveryError(f"ESPN authentication failed closed: {exc}") from exc
        if not authenticated:
            raise EspnDeliveryError(
                "ESPN portal session is not authenticated; run "
                "auth_manager.py --enroll-espn-keychain"
            )

    def _open_transfers(self) -> None:
        activity = self._page.locator(TRANSFER_PANEL_SELECTOR)
        if not activity.count() or not activity.is_visible():
            self._page.get_by_text("My Transfers", exact=True).first.click()
            activity.wait_for(state="visible")

    def _close_transfers(self) -> None:
        activity = self._page.locator(TRANSFER_PANEL_SELECTOR)
        if activity.count() and activity.is_visible():
            close = self._page.locator(
                '[role="dialog"]:visible .ui-dialog-titlebar-close'
            )
            if close.count() != 1:
                raise EspnDeliveryError(
                    "ESPN transfer-history dialog has no unique close control"
                )
            close.click()
            activity.wait_for(state="hidden", timeout=15_000)

    def _open_destination(self, destination: str) -> None:
        self._close_transfers()
        target = self._page.get_by_text(destination, exact=True)
        if not target.count():
            raise EspnDeliveryError(f"ESPN destination {destination!r} is unavailable")
        target.first.click()
        breadcrumb = self._page.locator(
            ".teamspace-breadcrumb-path-element", has_text=destination
        )
        breadcrumb.wait_for(state="visible")
        upload = self._page.get_by_text("Upload", exact=True)
        if not upload.count() or not upload.first.is_enabled():
            raise EspnDeliveryError(f"ESPN destination {destination!r} is not writable")

    def transfer_status(self, folder_name: str) -> str | None:
        self._open_transfers()
        records = self._page.locator(TRANSFER_RECORD_SELECTOR)
        for index in range(records.count()):
            text = " ".join(records.nth(index).inner_text().split())
            if folder_name in text:
                return classify_transfer_text(text)
        return None

    def destination_contains(self, folder_name: str) -> bool:
        self._open_destination(DESTINATION)
        return bool(
            self._page.locator(".datagrid-row", has_text=folder_name).count()
        )

    def start_folder_upload(self, package: Path, destination: str) -> None:
        self._open_destination(destination)
        subprocess.run(
            ["/usr/bin/open", "-a", "Signiant App"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._page.get_by_text("Upload", exact=True).click()
        continue_link = self._page.get_by_text("YES, CONTINUE", exact=True)
        if continue_link.count():
            continue_link.click()
        if not self._interactive_native_selection:
            _select_native_folder(package)
        try:
            self._page.get_by_text(package.name, exact=True).wait_for(
                state="visible", timeout=600_000
            )
        except Exception as exc:
            raise EspnDeliveryError(
                "The exact ESPN package was not selected in the Signiant picker"
            ) from exc
        upload = self._page.get_by_role("button", name="Upload", exact=True)
        if upload.count() != 1 or not upload.is_enabled():
            raise EspnDeliveryError("ESPN staged upload is not ready")
        upload.click()

    def resume_transfer(self, folder_name: str) -> None:
        self._open_transfers()
        row = self._page.locator(TRANSFER_RECORD_SELECTOR, has_text=folder_name).first
        button = row.get_by_text("Resume", exact=True)
        if button.count() != 1:
            raise EspnDeliveryError("Interrupted ESPN transfer has no unique Resume action")
        button.click()

    def close(self) -> None:
        self._context.close()
        self._pw.stop()


def classify_transfer_text(text: str) -> str:
    lowered = " ".join(str(text).split()).casefold()
    if "uploaded 1 file" in lowered:
        return "uploaded"
    if "interrupted" in lowered:
        return "interrupted"
    if any(value in lowered for value in ("uploading", "queued", "processing")):
        return "active"
    if "failed" in lowered or "error" in lowered:
        return "failed"
    return "unknown"


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
    interactive_native_selection: bool = False,
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
            gateway = PlaywrightEspnGateway(
                interactive_native_selection=interactive_native_selection
            )
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
        set_partner_status(ctx.specials_dir, "espn", "delivered")
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
    parser.add_argument(
        "--interactive-native-selection", action="store_true",
        help="wait for supervised Signiant selection instead of exact-path automation",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return 0 if deliver_espn(
        context_from_cli_args(args), args.dry_run, logging.getLogger("espn"),
        live_confirmation=args.confirm_live_release,
        interactive_native_selection=args.interactive_native_selection,
    ) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
