#!/usr/bin/env python3
"""Process uploaded SoundMouse metadata workbooks in the UPPM website.

The native Uploader is only the transport boundary.  This module opens each
workbook already present in UPPM/Music, preserves its saved territory and field
mapping choices, requires a zero-error review, signs it off, and verifies that
no new spreadsheet-error report appeared.  Manifest-bound checkpoints make a
restart skip only workbooks that were already proved complete.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlparse

from config import ReleaseContext, context_from_cli_args
from delivery_common import (
    DeliverySafetyError,
    checkpoint,
    collect_manifest,
    load_endpoint_state,
    receipt,
    require_live_authorization,
)
from delivery_state import partner_status, set_partner_status


MUSIC_URL = "https://app.soundmouse.com/web-app/music"
REPORTS_URL = "https://app.soundmouse.com/web-app/reports"


class SoundMouseWebDeliveryError(DeliverySafetyError):
    pass


@dataclass(frozen=True)
class ProcessingResult:
    filename: str
    tracks: int
    updates: int
    new_tracks: int
    missing_audio: int
    missing_recommended: int
    missing_artwork: int
    missing_mandatory: int
    duplicate_filenames: int
    manually_edited: int
    missing_titles: int
    empty_filenames: int
    last_changed: str

    @property
    def blocking_errors(self) -> int:
        return sum((
            self.missing_audio,
            self.missing_artwork,
            self.missing_mandatory,
            self.duplicate_filenames,
            self.manually_edited,
            self.missing_titles,
            self.empty_filenames,
        ))


class SoundMouseWebGateway(Protocol):
    def available_workbooks(self) -> tuple[str, ...]: ...
    def process_workbook(self, filename: str) -> ProcessingResult: ...
    def close(self) -> None: ...


def metadata_manifest(
    ctx: ReleaseContext, *, correction: bool = False
) -> tuple:
    root = (
        ctx.soundmouse_release_dir / "Missing" / "Metadata"
        if correction
        else ctx.soundmouse_release_dir / "Metadata"
    )
    return collect_manifest(root, allowed_suffixes=frozenset({".xlsx"}))


def _uploader_receipt(ctx: ReleaseContext, *, correction: bool = False) -> dict:
    name = (
        "soundmouse_uploader_correction_receipt.json"
        if correction
        else "soundmouse_uploader_receipt.json"
    )
    path = ctx.specials_dir / "_WORKFLOW" / name
    if not path.is_file():
        raise SoundMouseWebDeliveryError(
            f"SoundMouse website processing requires the verified Uploader receipt: {path}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SoundMouseWebDeliveryError(f"Invalid SoundMouse Uploader receipt: {exc}") from exc
    if data.get("release_id") != ctx.release_id:
        raise SoundMouseWebDeliveryError("SoundMouse Uploader receipt belongs to another release")
    return data


def _require_uploaded_workbooks(
    ctx: ReleaseContext, manifest: tuple, *, correction: bool = False
) -> None:
    data = _uploader_receipt(ctx, correction=correction)
    uploaded = {
        str(item.get("path") or "")
        for item in (data.get("files") or [])
        if isinstance(item, dict)
    }
    prefix = "Missing/Metadata/" if correction else "Metadata/"
    missing = sorted(
        item.path.name for item in manifest
        if f"{prefix}{item.path.name}" not in uploaded
        and f"Metadata/{item.path.name}" not in uploaded
    )
    if missing:
        raise SoundMouseWebDeliveryError(
            "Uploader receipt does not contain metadata workbook(s): " + ", ".join(missing)
        )


def _completed_from_state(ctx: ReleaseContext, manifest: tuple) -> set[str]:
    prior = load_endpoint_state(ctx, "soundmouse") or {}
    phase = str(prior.get("phase") or "")
    if phase not in {"website_processing", "website_processing_complete"}:
        return set()
    return {
        str(value) for value in (prior.get("remote_ids") or [])
        if str(value) in {item.path.name for item in manifest}
    }


def deliver_soundmouse_web(
    ctx: ReleaseContext,
    dry_run: bool,
    logger: logging.Logger,
    *,
    gateway: SoundMouseWebGateway | None = None,
    live_confirmation: str | None = None,
    interactive_login: bool = False,
    correction: bool = False,
) -> bool:
    owned_gateway = False
    try:
        manifest = metadata_manifest(ctx, correction=correction)
        if dry_run:
            logger.info(
                "  [DRY RUN] Would process %d SoundMouse workbook(s) in UPPM/Music",
                len(manifest),
            )
            return True
        _require_uploaded_workbooks(ctx, manifest, correction=correction)
        if not correction and partner_status(ctx.specials_dir, "soundmouse") != "uploaded":
            raise SoundMouseWebDeliveryError(
                "SoundMouse website processing requires delivery state uploaded"
            )
        require_live_authorization(ctx, live_confirmation)
        completed = _completed_from_state(ctx, manifest)
        if gateway is None:
            gateway = PlaywrightSoundMouseWebGateway(
                allow_interactive=interactive_login
            )
            owned_gateway = True
        available = set(gateway.available_workbooks())
        expected = {item.path.name for item in manifest}
        missing_remote = sorted(expected - available)
        if missing_remote:
            raise SoundMouseWebDeliveryError(
                "Uploaded workbook(s) are absent from UPPM/Music: "
                + ", ".join(missing_remote)
            )

        results: list[ProcessingResult] = []
        for item in manifest:
            filename = item.path.name
            if filename in completed:
                logger.info("  ↩ SoundMouse workbook already processed: %s", filename)
                continue
            result = gateway.process_workbook(filename)
            if result.filename != filename:
                raise SoundMouseWebDeliveryError(
                    f"SoundMouse processed {result.filename!r}, expected {filename!r}"
                )
            if result.tracks <= 0 or result.new_tracks + result.updates != result.tracks:
                raise SoundMouseWebDeliveryError(
                    f"SoundMouse returned inconsistent track totals for {filename}"
                )
            if result.blocking_errors:
                raise SoundMouseWebDeliveryError(
                    f"SoundMouse returned {result.blocking_errors} blocking error(s) "
                    f"for {filename}"
                )
            results.append(result)
            completed.add(filename)
            checkpoint(
                ctx,
                "soundmouse",
                manifest,
                "website_processing",
                f"zero-error website processing completed for {filename}",
                remote_ids=completed,
            )
            logger.info(
                "  ✓ SoundMouse processed %s: %d track(s), %d recommended warning(s)",
                filename, result.tracks, result.missing_recommended,
            )

        if completed != expected:
            raise SoundMouseWebDeliveryError(
                "SoundMouse website processing ended with an incomplete workbook manifest"
            )
        checkpoint(
            ctx,
            "soundmouse",
            manifest,
            "website_processing_complete",
            "every uploaded metadata workbook completed with zero blocking errors",
            remote_ids=completed,
        )
        path = receipt(ctx, "soundmouse", manifest, {
            "workspace": "UPPM",
            "module": "Music",
            "workbooks": [
                asdict(result) for result in results
            ],
            "completed_workbooks": sorted(completed),
            "verification": "zero blocking errors and no new spreadsheet-error report",
            "correction": correction,
        })
        if not correction:
            set_partner_status(ctx.specials_dir, "soundmouse", "delivered")
        logger.info("  ✓ SoundMouse website processing verified: %s", path)
        return True
    except Exception as exc:
        logger.error("  ✗ SoundMouse website processing failed: %s", exc)
        return False
    finally:
        if owned_gateway and gateway is not None:
            gateway.close()


def _metric(text: str, label: str) -> int:
    normalized = " ".join(text.split())
    match = re.search(rf"([0-9][0-9,]*)\s+{re.escape(label)}", normalized, re.I)
    if not match:
        raise SoundMouseWebDeliveryError(f"SoundMouse review metric is missing: {label}")
    return int(match.group(1).replace(",", ""))


def parse_review(filename: str, text: str, *, last_changed: str = "") -> ProcessingResult:
    # New/Update occur three times (tracks, albums, libraries).  Bind them to
    # the track subsection rather than reading the first arbitrary number.
    normalized = " ".join(text.split())
    match = re.search(
        r"Tracks in spreadsheet\.\s*([0-9][0-9,]*)\s+New\s+([0-9][0-9,]*)"
        r"\s+Update\s+([0-9][0-9,]*)",
        normalized,
        re.I,
    )
    if not match:
        raise SoundMouseWebDeliveryError("SoundMouse track New/Update totals are missing")
    return ProcessingResult(
        filename=filename,
        tracks=int(match.group(1).replace(",", "")),
        new_tracks=int(match.group(2).replace(",", "")),
        updates=int(match.group(3).replace(",", "")),
        missing_audio=_metric(text, "Tracks missing a corresponding audio file."),
        missing_recommended=_metric(text, "Tracks missing recommended metadata."),
        missing_artwork=_metric(text, "Albums missing artwork."),
        missing_mandatory=_metric(text, "Tracks missing mandatory metadata."),
        duplicate_filenames=_metric(text, "Duplicate filenames in spreadsheet."),
        manually_edited=_metric(text, "Manually edited tracks to be updated."),
        missing_titles=_metric(text, "Tracks missing title field in spreadsheet."),
        empty_filenames=_metric(text, "Tracks with empty filenames in spreadsheet."),
        last_changed=last_changed,
    )


class PlaywrightSoundMouseWebGateway:
    """Visible retained-session implementation for the SoundMouse website."""

    def __init__(self, *, allow_interactive: bool = False):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SoundMouseWebDeliveryError(
                "SoundMouse website processing requires Playwright"
            ) from exc
        from auth_manager import private_creation_umask, secure_private_directory
        from soundmouse_web_auth import PROFILE_DIR, PORTAL_URL, require_authenticated

        secure_private_directory(PROFILE_DIR, recursive=True)
        self._playwright = sync_playwright().start()
        with private_creation_umask():
            self._context = self._playwright.chromium.launch_persistent_context(
                str(PROFILE_DIR), channel="chrome", headless=False
            )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.goto(PORTAL_URL, wait_until="domcontentloaded")
        require_authenticated(self._page, allow_interactive=allow_interactive)
        self._workspace = self._workspace_id()
        self._goto_music()

    def _workspace_id(self) -> str:
        parsed = parse_qs(urlparse(self._page.url).query).get("ws", [])
        if len(parsed) == 1 and parsed[0]:
            return parsed[0]
        links = self._page.locator('a[href*="/web-app/music?ws="]')
        values = {
            parse_qs(urlparse(str(links.nth(i).get_attribute("href") or "")).query)
            .get("ws", [""])[0]
            for i in range(links.count())
        }
        values.discard("")
        if len(values) != 1:
            raise SoundMouseWebDeliveryError("Could not resolve one UPPM workspace ID")
        return values.pop()

    def _goto_music(self) -> None:
        self._page.goto(
            f"{MUSIC_URL}?ws={self._workspace}", wait_until="domcontentloaded"
        )
        self._page.get_by_role("heading", name="Music", exact=True).wait_for(
            state="visible", timeout=60_000
        )
        self._page.get_by_text("UPPM", exact=True).first.wait_for(
            state="visible", timeout=60_000
        )

    def available_workbooks(self) -> tuple[str, ...]:
        self._goto_music()
        loading = self._page.get_by_text("Now Loading…", exact=True)
        if loading.count():
            loading.first.wait_for(state="hidden", timeout=60_000)
        self._page.get_by_text("Spreadsheet", exact=True).first.wait_for(
            state="visible", timeout=60_000
        )
        cells = self._page.locator('[role="gridcell"]')
        names = {
            str(cells.nth(index).inner_text()).strip()
            for index in range(cells.count())
            if str(cells.nth(index).inner_text()).strip().casefold().endswith(".xlsx")
        }
        return tuple(sorted(names))

    def _error_report_count(self, filename: str) -> int:
        self._page.goto(
            f"{REPORTS_URL}?ws={self._workspace}", wait_until="domcontentloaded"
        )
        self._page.get_by_role("heading", name="Reports", exact=True).wait_for(
            state="visible", timeout=60_000
        )
        self._page.get_by_text(re.compile(r"\d+\+? reports", re.I)).wait_for(
            state="visible", timeout=60_000
        )
        return self._page.get_by_text(f"Errors_{filename}", exact=True).count()

    def process_workbook(self, filename: str) -> ProcessingResult:
        before_errors = self._error_report_count(filename)
        self._goto_music()
        target = self._page.get_by_text(filename, exact=True)
        if target.count() != 1:
            raise SoundMouseWebDeliveryError(
                f"Expected one UPPM/Music workbook named {filename}, found {target.count()}"
            )
        target.click()
        self._page.get_by_text("Step 1: Origin", exact=True).wait_for(
            state="visible", timeout=60_000
        )
        if not self._page.get_by_role(
            "radio", name="Production Library", exact=True
        ).is_checked():
            raise SoundMouseWebDeliveryError(
                f"{filename} is not mapped as Production Library"
            )
        if self._page.locator('input[type="checkbox"]:checked').count() < 1:
            raise SoundMouseWebDeliveryError(
                f"{filename} has no saved territory selection"
            )
        for _ in range(3):
            button = self._page.get_by_role("button", name="Next ▶", exact=True)
            button.wait_for(state="visible", timeout=60_000)
            button.click()
        signoff = self._page.get_by_role(
            "button", name="Sign Off and Complete", exact=True
        )
        signoff.wait_for(state="visible", timeout=15 * 60_000)
        result = parse_review(filename, self._page.locator("body").inner_text())
        if result.blocking_errors:
            return result
        signoff.click()
        self._page.wait_for_url("**/web-app/music?ws=*", timeout=30 * 60_000)
        row = self._page.get_by_role("row").filter(has_text=filename)
        if row.count() != 1:
            raise SoundMouseWebDeliveryError(
                f"Completed workbook did not return to the UPPM/Music list: {filename}"
            )
        row_text = " ".join(row.inner_text().split())
        changed = re.search(r"(\d{1,2}\s+\w+\s+\d{4}\s+•\s+\d{2}:\d{2}:\d{2})", row_text)
        after_errors = self._error_report_count(filename)
        if after_errors != before_errors:
            raise SoundMouseWebDeliveryError(
                f"SoundMouse generated a new spreadsheet-error report for {filename}"
            )
        return ProcessingResult(**{
            **asdict(result),
            "last_changed": changed.group(1) if changed else "verified in Music list",
        })

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            self._playwright.stop()


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Process uploaded SoundMouse metadata in UPPM/Music"
    )
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--part", type=int, choices=(1, 2))
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--full-month-content", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-live-release")
    parser.add_argument("--interactive-login", action="store_true")
    parser.add_argument("--correction", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ok = deliver_soundmouse_web(
        context_from_cli_args(args),
        args.dry_run,
        logging.getLogger("soundmouse_web_delivery"),
        live_confirmation=args.confirm_live_release,
        interactive_login=args.interactive_login,
        correction=args.correction,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
