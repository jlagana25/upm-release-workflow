#!/usr/bin/env python3
"""Submit the two SoundExchange registrant batches with strict validation."""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from config import PRIVATE_STATE_DIR, ReleaseContext, context_from_cli_args
from delivery_common import (
    DeliverySafetyError,
    checkpoint,
    collect_manifest,
    latest_workflow_gates,
    load_endpoint_state,
    private_json,
    receipt,
    require_live_authorization,
)
from delivery_state import set_partner_status


PORTAL_URL = "https://sxdirect.soundexchange.com/login/?next=%2Fcatalog%2Fsubmit%2F"


class SoundExchangeDeliveryError(DeliverySafetyError):
    pass


@dataclass(frozen=True)
class Registrant:
    key: str
    name: str
    registrant_id: str
    rights_owner: str
    filename_prefix: str


@dataclass(frozen=True)
class ValidationEntry:
    entry_number: str
    title: str
    isrc: str
    valid: bool
    messages: tuple[str, ...] = ()


REGISTRANTS = (
    Registrant(
        "mgb", "Universal Music - Mgb Na Llc", "2178141344",
        "Universal Mgb_Na_Llc", "ISRC Ingest Form - MGB NA LLC - Part ",
    ),
    Registrant(
        "ztunes", "Universal Music – Z Tunes, Llc", "2178141458",
        "Firstcom Music", "ISRC Ingest Form - Z TUNES LLC - Part ",
    ),
)


class SoundExchangeGateway(Protocol):
    def require_authenticated(self) -> None: ...
    def select_registrant(self, registrant: Registrant) -> None: ...
    def pending_count(self) -> int: ...
    def bulk_import(self, workbook: Path) -> None: ...
    def wait_for_validation(self, expected_total: int, timeout_seconds: float) -> tuple[ValidationEntry, ...]: ...
    def submit_recordings(self) -> None: ...
    def verify_upload_history(self, registrant: Registrant, expected_count: int) -> str | None: ...
    def close(self) -> None: ...


class PlaywrightSoundExchangeGateway:
    """Visible retained-session adapter. Selectors fail closed if the UI drifts."""

    def __init__(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SoundExchangeDeliveryError("SoundExchange delivery requires Playwright") from exc
        profile = PRIVATE_STATE_DIR / "soundexchange_profile"
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=False
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.goto(PORTAL_URL, wait_until="domcontentloaded")

    def require_authenticated(self) -> None:
        if self._page.locator("input[type=password]").count() or "/login" in self._page.url:
            raise SoundExchangeDeliveryError("SoundExchange session is not authenticated")

    def select_registrant(self, registrant: Registrant) -> None:
        self._page.get_by_text("My Catalog", exact=True).click()
        self._page.get_by_text("Submit Recordings", exact=True).click()
        row = self._page.locator("tr", has_text=registrant.name)
        if row.count() != 1 or registrant.registrant_id not in row.inner_text():
            raise SoundExchangeDeliveryError(f"Could not uniquely verify {registrant.name}")
        link = row.get_by_text(registrant.rights_owner, exact=True)
        if link.count() != 1:
            raise SoundExchangeDeliveryError(f"Rights Owner changed for {registrant.name}")
        link.click()
        self._page.get_by_text("Bulk Import", exact=True).wait_for()

    def pending_count(self) -> int:
        rows = self._page.locator("table tbody tr")
        return rows.count()

    def bulk_import(self, workbook: Path) -> None:
        self._page.get_by_text("Bulk Import", exact=True).click()
        picker = self._page.locator("input[type=file]")
        if picker.count() != 1:
            raise SoundExchangeDeliveryError("Bulk Import file input was not unique")
        picker.set_input_files(str(workbook))
        submit = self._page.get_by_role("button", name=re.compile("upload|import", re.I))
        if submit.count() != 1:
            raise SoundExchangeDeliveryError("Bulk Import action was not unique")
        submit.click()

    def wait_for_validation(self, expected_total: int, timeout_seconds: float) -> tuple[ValidationEntry, ...]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            rows = self._page.locator("table tbody tr")
            if rows.count() == expected_total:
                parsed: list[ValidationEntry] = []
                still_processing = False
                for index in range(rows.count()):
                    text = " ".join(rows.nth(index).inner_text().split())
                    lowered = text.casefold()
                    if "processing" in lowered or "validating" in lowered:
                        still_processing = True
                        break
                    isrc_match = re.search(r"\b[A-Z]{2}[A-Z0-9]{3}\d{7}\b", text, re.I)
                    messages = tuple(
                        item.strip() for item in re.findall(r"(?:error|invalid|warning):?[^|;]*", text, re.I)
                    )
                    parsed.append(ValidationEntry(
                        str(index + 1), text, isrc_match.group(0).upper() if isrc_match else "",
                        not any(word in lowered for word in ("invalid", "error", "rejected")),
                        messages,
                    ))
                if not still_processing:
                    return tuple(parsed)
            time.sleep(2)
        raise SoundExchangeDeliveryError("SoundExchange validation timed out")

    def submit_recordings(self) -> None:
        button = self._page.get_by_role("button", name="Submit Recordings", exact=True)
        if button.count() != 1 or not button.is_enabled():
            raise SoundExchangeDeliveryError("Submit Recordings is unavailable")
        button.click()

    def verify_upload_history(self, registrant: Registrant, expected_count: int) -> str | None:
        self._page.get_by_text("My Catalog", exact=True).click()
        self._page.get_by_text("Upload History", exact=True).click()
        self._page.get_by_text("View Upload History", exact=True).click()
        row = self._page.locator("tr", has_text=registrant.name).first
        if not row.count():
            return None
        text = " ".join(row.inner_text().split())
        if str(expected_count) not in text or not any(word in text.casefold() for word in ("submitted", "complete")):
            return None
        return row.get_attribute("data-id") or text[:200]

    def close(self) -> None:
        self._context.close()
        self._pw.stop()


def _part_number(path: Path) -> int:
    match = re.search(r" - Part (\d+)\.xlsx$", path.name, re.I)
    if not match:
        raise SoundExchangeDeliveryError(f"Unexpected SoundExchange workbook name: {path.name}")
    return int(match.group(1))


def registrant_workbooks(ctx: ReleaseContext, registrant: Registrant) -> tuple[Path, ...]:
    root = ctx.soundexchange_final_dir
    paths = tuple(sorted(root.glob(registrant.filename_prefix + "*.xlsx"), key=_part_number))
    if not paths:
        raise SoundExchangeDeliveryError(f"No final workbooks for {registrant.name}")
    parts = [_part_number(path) for path in paths]
    if parts != list(range(1, len(parts) + 1)):
        raise SoundExchangeDeliveryError(f"Non-contiguous workbook parts for {registrant.name}: {parts}")
    return paths


def workbook_rows(path: Path) -> tuple[tuple[str, str], ...]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise SoundExchangeDeliveryError("SoundExchange validation requires openpyxl") from exc
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        sheet.reset_dimensions()
        rows = sheet.iter_rows(values_only=True)
        isrc_index = None
        title_index = None
        header_row = None
        # SoundExchange's official ingest template has nine title/instruction
        # rows before its real row-10 headers. Generated workbooks preserve
        # that layout, so locate the header by content instead of assuming row 1.
        for row_number, row in enumerate(rows, start=1):
            normalized = {
                re.sub(r"[^a-z0-9]", "", str(value or "").casefold()): index
                for index, value in enumerate(row)
                if str(value or "").strip()
            }
            isrc_index = next(
                (
                    index for key, index in normalized.items()
                    if key == "isrc"
                    or (
                        key.startswith("isrc")
                        and "ingest" not in key
                        and "file" not in key
                    )
                    or key.startswith("soundrecordingisrc")
                ),
                None,
            )
            if isrc_index is None:
                if row_number >= 50:
                    break
                continue
            title_index = next(
                (
                    index for key, index in normalized.items()
                    if key == "title" or key.startswith("tracktitle")
                    or key.startswith("recordingtitle")
                    or key.startswith("soundrecordingtitle")
                ),
                None,
            )
            header_row = row_number
            break
        if isrc_index is None or header_row is None:
            raise SoundExchangeDeliveryError(
                f"Workbook has no ISRC header in its first 50 rows: {path}"
            )
        result: list[tuple[str, str]] = []
        for row in rows:
            if not any(value not in (None, "") for value in row):
                continue
            isrc = str(row[isrc_index] or "").strip().upper()
            title = str(row[title_index] or "").strip() if title_index is not None else ""
            if not isrc:
                raise SoundExchangeDeliveryError(f"Workbook contains a blank ISRC: {path}")
            result.append((isrc, title))
        return tuple(result)
    finally:
        workbook.close()


def _invalid_audit(
    ctx: ReleaseContext,
    registrant: Registrant,
    workbooks: tuple[Path, ...],
    entries: tuple[ValidationEntry, ...],
) -> Path:
    invalid = [entry for entry in entries if not entry.valid]
    workbook_by_isrc = {
        isrc: workbook.name
        for workbook in workbooks
        for isrc, _title in workbook_rows(workbook)
    }
    path = ctx.specials_dir / "_WORKFLOW" / "soundexchange_invalid_entries" / f"{registrant.key}.json"
    return private_json(path, {
        "registrant": registrant.name,
        "registrant_id": registrant.registrant_id,
        "rights_owner": registrant.rights_owner,
        "workbooks": [item.name for item in workbooks],
        "invalid_count": len(invalid),
        "total_count": len(entries),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "entries": [
            entry.__dict__ | {"source_workbook": workbook_by_isrc.get(entry.isrc)}
            for entry in invalid
        ],
    })


def deliver_soundexchange(
    ctx: ReleaseContext,
    dry_run: bool,
    logger: logging.Logger,
    *,
    gateway: SoundExchangeGateway | None = None,
    live_confirmation: str | None = None,
    validation_timeout_seconds: float = 3600,
) -> bool:
    owned = False
    try:
        batches = [(registrant, registrant_workbooks(ctx, registrant)) for registrant in REGISTRANTS]
        all_files = tuple(
            item for _, paths in batches for path in paths
            for item in collect_manifest(path, allowed_suffixes=frozenset({".xlsx"}))
        )
        expected = {
            registrant.key: tuple(row for path in paths for row in workbook_rows(path))
            for registrant, paths in batches
        }
        for registrant, _ in batches:
            isrcs = [isrc for isrc, _ in expected[registrant.key]]
            if len(isrcs) != len(set(isrcs)):
                raise SoundExchangeDeliveryError(f"Duplicate ISRC for {registrant.name}")
        all_isrcs = [isrc for rows in expected.values() for isrc, _ in rows]
        if len(all_isrcs) != len(set(all_isrcs)):
            raise SoundExchangeDeliveryError("An ISRC appears in both registrant batches")
        if dry_run:
            logger.info("  [DRY RUN] SoundExchange: %d workbook(s), %d recording(s)", len(all_files), sum(map(len, expected.values())))
            return True
        require_live_authorization(ctx, live_confirmation)
        gate_ok, detail = latest_workflow_gates(ctx, ("10 SoundExchange forms",))
        if not gate_ok:
            raise SoundExchangeDeliveryError(f"SoundExchange submission is blocked: {detail}")
        if gateway is None:
            gateway = PlaywrightSoundExchangeGateway()
            owned = True
        gateway.require_authenticated()
        history_ids: dict[str, str] = {}
        for registrant, paths in batches:
            rows = expected[registrant.key]
            existing_history = gateway.verify_upload_history(registrant, len(rows))
            if existing_history:
                history_ids[registrant.key] = existing_history
                continue
            prior = load_endpoint_state(ctx, "soundexchange")
            uncertain_submit = any(
                item.get("phase") == "submit_intent" and item.get("detail") == registrant.name
                for item in ((prior or {}).get("history") or [])
            )
            if uncertain_submit:
                raise SoundExchangeDeliveryError(
                    f"{registrant.name} has an unverified prior Submit Recordings action; "
                    "refusing a duplicate"
                )
            gateway.select_registrant(registrant)
            if gateway.pending_count() != 0:
                raise SoundExchangeDeliveryError(
                    f"{registrant.name} has pending recordings from another run"
                )
            for path in paths:
                gateway.bulk_import(path)
            entries = gateway.wait_for_validation(len(rows), validation_timeout_seconds)
            actual_isrcs = [entry.isrc for entry in entries]
            expected_isrcs = [isrc for isrc, _ in rows]
            invalid = [entry for entry in entries if not entry.valid]
            if invalid:
                audit = _invalid_audit(ctx, registrant, paths, entries)
                raise SoundExchangeDeliveryError(
                    f"{len(invalid)} invalid entries for {registrant.name}; audit: {audit}"
                )
            if len(entries) != len(rows) or sorted(actual_isrcs) != sorted(expected_isrcs):
                raise SoundExchangeDeliveryError(
                    f"Validated recordings do not exactly match {registrant.name} workbooks"
                )
            checkpoint(ctx, "soundexchange", all_files, "validated", registrant.name)
            checkpoint(
                ctx, "soundexchange", all_files, "submit_intent", registrant.name,
                remote_ids=history_ids.values(),
            )
            gateway.submit_recordings()
            deadline = time.monotonic() + validation_timeout_seconds
            history_id = gateway.verify_upload_history(registrant, len(rows))
            while not history_id and time.monotonic() < deadline:
                time.sleep(2)
                history_id = gateway.verify_upload_history(registrant, len(rows))
            if not history_id:
                raise SoundExchangeDeliveryError(f"Upload History did not verify {registrant.name}")
            history_ids[registrant.key] = history_id
            checkpoint(
                ctx, "soundexchange", all_files, "submitted", registrant.name,
                remote_ids=history_ids.values(),
            )
        path = receipt(ctx, "soundexchange", all_files, {
            "registrants": [
                {
                    "name": registrant.name,
                    "registrant_id": registrant.registrant_id,
                    "rights_owner": registrant.rights_owner,
                    "workbooks": [item.name for item in paths],
                    "recordings": len(expected[registrant.key]),
                    "history_id": history_ids[registrant.key],
                }
                for registrant, paths in batches
            ]
        })
        set_partner_status(ctx.specials_dir, "soundexchange", "delivered")
        logger.info("  ✓ SoundExchange submissions verified: %s", path)
        return True
    except Exception as exc:
        logger.error("  ✗ SoundExchange delivery failed: %s", exc)
        return False
    finally:
        if owned and gateway is not None:
            gateway.close()


def _main() -> int:
    parser = argparse.ArgumentParser(description="Submit SoundExchange registrant workbooks")
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
    return 0 if deliver_soundexchange(
        context_from_cli_args(args), args.dry_run, logging.getLogger("soundexchange"),
        live_confirmation=args.confirm_live_release,
    ) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
