"""
soundmouse.py — Step 16: Build the SoundMouse release delivery.

The step exports the SoundMouse tracklist and bucket from Domo, creates one
workflow-period directory with Covers/Metadata/MEDIA children, downloads WAVs
with UniSync and cover art from the tracklist, then exports only the metadata
workbooks named by the bucket card.  Raw Domo ActivationRange values never
control delivery-directory naming.

All browser and GUI dependencies remain lazy so this module imports headless.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import shutil
import tempfile
from copy import copy
from pathlib import Path

from config import (
    SOUNDMOUSE_BASE,
    SOUNDMOUSE_DOMO_CARDS,
    SOUNDMOUSE_DOMO_PAGE_ID,
    DOMO_PROFILE_DIR,
    UPM_CACHE_WAV,
    ReleaseContext,
    context_from_cli_args,
)
from auth_manager import private_creation_umask, secure_private_directory
from tracklist_columns import (
    POSSIBLE_COVER_COLS,
    POSSIBLE_FILENAME_COLS,
    POSSIBLE_URL_COLS,
    _find_column,
)


ACTIVATION_RANGE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_to_\d{4}-\d{2}-\d{2}$"
)
# code -> (territory label, territory set).  The set lets us recognize either
# a bucket label ("02 UK DE SE OZ") or a raw Territory List value.
METADATA_SHEETS: dict[str, tuple[str, frozenset[str]]] = {
    "01": ("ALL",         frozenset({"US", "UK", "DE", "SE", "OZ"})),
    "02": ("UK DE SE OZ", frozenset({"UK", "DE", "SE", "OZ"})),
    "03": ("US DE SE OZ", frozenset({"US", "DE", "SE", "OZ"})),
    "04": ("DE SE OZ",    frozenset({"DE", "SE", "OZ"})),
    "05": ("UK SE OZ",    frozenset({"UK", "SE", "OZ"})),
    "06": ("DE SE",       frozenset({"DE", "SE"})),
    "07": ("US OZ",       frozenset({"US", "OZ"})),
    "08": ("OZ",          frozenset({"OZ"})),
    "09": ("SE",          frozenset({"SE"})),
    "10": ("US",          frozenset({"US"})),
}


def metadata_filename(code: str) -> str:
    label = METADATA_SHEETS[code][0]
    return f"SoundMouseMetadata {code} - {label}.xlsx"


def strip_xlsx_formatting(path: Path) -> None:
    """Remove presentation formatting while preserving workbook cell values.

    SoundMouse requires real XLSX workbooks, but not Domo's fonts, fills,
    borders, alignments, number formats, row/column sizing, conditional
    formatting, or frozen views.  Merged ranges, formulas, values, worksheet
    names, and workbook structure are retained.

    The cleaned workbook is saved beside the download first and then atomically
    replaces it, so a failed save cannot destroy the original export.
    """
    from openpyxl import Workbook, load_workbook
    from openpyxl.worksheet.dimensions import SheetFormatProperties
    from openpyxl.worksheet.views import Selection

    workbook = load_workbook(path)
    default_cell = Workbook().active["A1"]

    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                cell.font = copy(default_cell.font)
                cell.fill = copy(default_cell.fill)
                cell.border = copy(default_cell.border)
                cell.alignment = copy(default_cell.alignment)
                cell.number_format = "General"
                cell.protection = copy(default_cell.protection)

        worksheet.row_dimensions.clear()
        worksheet.column_dimensions.clear()
        worksheet.sheet_format = SheetFormatProperties()
        worksheet.conditional_formatting._cf_rules.clear()
        worksheet.freeze_panes = None
        # Clearing freeze_panes does not clear the pane identifier on Domo's
        # existing selection.  That produces a contradictory sheet view
        # (selection pane="bottomLeft" with no <pane>) which Excel repairs on
        # open.  Reset to one ordinary selection after removing the pane.
        worksheet.sheet_view.selection = [
            Selection(activeCell="A1", sqref="A1")
        ]
        worksheet.sheet_view.showGridLines = True
        worksheet.sheet_view.zoomScale = None
        worksheet.sheet_view.zoomScaleNormal = None
        for table in worksheet.tables.values():
            table.tableStyleInfo = None

    temp_path = path.with_name(f".{path.stem}.unformatted.tmp.xlsx")
    try:
        workbook.save(temp_path)
        # Reopen the serialized package and enforce the view invariant before
        # replacing the valid Domo download. openpyxl itself tolerates the
        # stale selection state, but desktop Excel does not.
        check = load_workbook(temp_path, read_only=False, data_only=False)
        try:
            for worksheet in check.worksheets:
                if worksheet.sheet_view.pane is None and any(
                    selection.pane is not None
                    for selection in worksheet.sheet_view.selection
                ):
                    raise ValueError(
                        f"invalid pane selection remained in {worksheet.title!r}"
                    )
        finally:
            check.close()
        temp_path.replace(path)
    finally:
        workbook.close()
        if temp_path.exists():
            temp_path.unlink()


def _file_key(value: object) -> str:
    """Case-insensitive leaf filename key; metadata paths never control IO."""
    return Path(str(value or "").strip()).name.casefold()


def _disk_file_keys(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {
        path.name.casefold()
        for path in root.rglob("*")
        if path.is_file() and path.stat().st_size > 0
    }


def validate_soundmouse_delivery(
    metadata_paths: list[Path],
    media_root: Path,
    covers_root: Path,
    report_path: Path,
    dry_run: bool,
    logger: logging.Logger,
) -> bool:
    """Confirm every audio and cover named by the period metadata is present.

    Expected names are the union across every bucket-selected workbook.
    Matching is recursive and case-insensitive by exact leaf filename.  A CSV
    report is always written on a real run (header-only when clean), making the
    final gate auditable and preventing a stale prior report from lingering.
    """
    if dry_run:
        logger.info(
            f"  [DRY RUN] Would validate metadata audio against {media_root}"
        )
        logger.info(
            f"  [DRY RUN] Would validate metadata covers against {covers_root}"
        )
        logger.info(f"  [DRY RUN] Validation report: {report_path}")
        return True

    from openpyxl import load_workbook

    expected_audio: dict[str, set[str]] = {}
    expected_covers: dict[str, set[str]] = {}
    expected_names: dict[str, str] = {}
    errors: list[dict[str, str]] = []

    def add_expected(
        destination: dict[str, set[str]], raw: object, source: Path
    ) -> None:
        key = _file_key(raw)
        if key:
            destination.setdefault(key, set()).add(source.name)
            expected_names.setdefault(
                key, Path(str(raw or "").strip()).name
            )

    for metadata_path in metadata_paths:
        if not metadata_path.is_file():
            errors.append({
                "Type": "METADATA",
                "Filename": metadata_path.name,
                "Metadata Workbooks": metadata_path.name,
                "Expected Root": str(metadata_path.parent),
                "Problem": "Metadata workbook is missing",
            })
            continue
        try:
            workbook = load_workbook(
                metadata_path, read_only=True, data_only=False
            )
            try:
                recognized_sheet = False
                for worksheet in workbook.worksheets:
                    rows = worksheet.iter_rows(values_only=True)
                    header = next(rows, None)
                    if not header:
                        continue
                    headers = [str(value or "").strip() for value in header]
                    audio_col = _find_column(headers, POSSIBLE_FILENAME_COLS)
                    cover_col = _find_column(headers, POSSIBLE_COVER_COLS)
                    if not audio_col or not cover_col:
                        continue
                    recognized_sheet = True
                    audio_index = headers.index(audio_col)
                    cover_index = headers.index(cover_col)
                    for row in rows:
                        if audio_index < len(row):
                            add_expected(
                                expected_audio, row[audio_index], metadata_path
                            )
                        if cover_index < len(row):
                            add_expected(
                                expected_covers, row[cover_index], metadata_path
                            )
                if not recognized_sheet:
                    errors.append({
                        "Type": "METADATA",
                        "Filename": metadata_path.name,
                        "Metadata Workbooks": metadata_path.name,
                        "Expected Root": str(metadata_path),
                        "Problem": (
                            "No worksheet contains both an audio filename "
                            "and cover-art filename column"
                        ),
                    })
            finally:
                workbook.close()
        except Exception as exc:
            errors.append({
                "Type": "METADATA",
                "Filename": metadata_path.name,
                "Metadata Workbooks": metadata_path.name,
                "Expected Root": str(metadata_path),
                "Problem": f"Could not read workbook: {exc}",
            })

    present_audio = _disk_file_keys(media_root)
    present_covers = _disk_file_keys(covers_root)
    missing_audio = sorted(set(expected_audio) - present_audio)
    missing_covers = sorted(set(expected_covers) - present_covers)

    for filename in missing_audio:
        errors.append({
            "Type": "AUDIO",
            "Filename": expected_names[filename],
            "Metadata Workbooks": "; ".join(sorted(expected_audio[filename])),
            "Expected Root": str(media_root),
            "Problem": "Referenced audio file is missing",
        })
    for filename in missing_covers:
        errors.append({
            "Type": "COVER",
            "Filename": expected_names[filename],
            "Metadata Workbooks": "; ".join(sorted(expected_covers[filename])),
            "Expected Root": str(covers_root),
            "Problem": "Referenced cover file is missing",
        })

    report_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "Type", "Filename", "Metadata Workbooks", "Expected Root", "Problem"
    ]
    with report_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(errors)

    logger.info(
        f"  SoundMouse validation: {len(expected_audio)} audio, "
        f"{len(expected_covers)} covers referenced by metadata."
    )
    logger.info(
        f"  Present: {len(present_audio)} files under MEDIA, "
        f"{len(present_covers)} files under Covers."
    )
    logger.info(f"  Validation report: {report_path}")
    if errors:
        logger.error(
            f"  ✗ SoundMouse validation failed: {len(missing_audio)} missing "
            f"audio, {len(missing_covers)} missing covers, and "
            f"{len(errors) - len(missing_audio) - len(missing_covers)} "
            "metadata error(s)."
        )
        for item in errors[:25]:
            logger.error(
                f"     {item['Type']}: {item['Filename']} — {item['Problem']}"
            )
        if len(errors) > 25:
            logger.error(f"     … and {len(errors) - 25} more; see report.")
        return False

    logger.info("  ✓ SoundMouse validation passed — all metadata audio and covers exist.")
    return True


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header row: {path}")
        rows = [dict(row) for row in reader]
        return list(reader.fieldnames), rows


def activation_ranges_from_tracklist(path: Path) -> list[str]:
    """Return safe, distinct ActivationRange values in first-seen order."""
    fields, rows = _read_csv(path)
    col = _find_column(fields, ["ACTIVATIONRANGE"])
    if not col:
        raise ValueError(f"SoundMouse tracklist has no ActivationRange column: {path}")

    ranges: list[str] = []
    for row in rows:
        value = str(row.get(col, "")).strip()
        if not value:
            continue
        if not ACTIVATION_RANGE_RE.fullmatch(value):
            raise ValueError(
                f"Unsafe/invalid ActivationRange {value!r} in {path}; expected "
                "YYYY-MM-DD_to_YYYY-MM-DD."
            )
        if value not in ranges:
            ranges.append(value)
    return ranges


def create_soundmouse_directories(
    release_directory: Path,
    dry_run: bool,
    logger: logging.Logger,
) -> Path:
    """Create the one delivery root resolved by the workflow period.

    Domo's ``ActivationRange`` values describe upstream activation windows and
    may not match this workflow's Part 1/Part 2/full-month boundaries.  They
    therefore never control the delivery directory name.
    """
    for child in (
        release_directory,
        release_directory / "Covers",
        release_directory / "Metadata",
        release_directory / "MEDIA",
    ):
        if dry_run:
            logger.info(f"  [DRY RUN] Would create: {child}")
        else:
            child.mkdir(parents=True, exist_ok=True)
    logger.info(
        f"  {'Would prepare' if dry_run else 'Prepared'}: {release_directory}"
    )
    return release_directory


def _territory_set(value: str) -> frozenset[str]:
    tokens = set(re.findall(r"[A-Z]+", value.upper()))
    return frozenset(tokens & {"US", "UK", "DE", "SE", "OZ"})


def metadata_codes_from_bucket(path: Path) -> list[str]:
    """Recognize bucket codes/names or exact territory combinations."""
    _fields, rows = _read_csv(path)
    found: set[str] = set()
    by_territory = {territories: code for code, (_label, territories) in METADATA_SHEETS.items()}

    for row in rows:
        for raw in row.values():
            value = str(raw or "").strip()
            if not value:
                continue
            code_match = re.search(r"^\s*(0[1-9]|10)(?:\D|$)", value)
            if code_match:
                found.add(code_match.group(1))
            territories = _territory_set(value)
            if territories in by_territory:
                found.add(by_territory[territories])
            if value.upper() == "ALL":
                found.add("01")
    return sorted(found, key=int)


def _domo_configs(
    ctx: ReleaseContext,
    codes: list[str] | None = None,
    *,
    metadata_dir: Path | None = None,
) -> list[dict]:
    if codes is None:
        return [
            {
                "key": "soundmouse_tracklist",
                "card_id": SOUNDMOUSE_DOMO_CARDS["tracklist"],
                "page_id": SOUNDMOUSE_DOMO_PAGE_ID,
                "description": "SoundMouse Tracklist",
                "output_fn": lambda _ctx: ctx.soundmouse_tracklist_csv,
            },
            {
                "key": "soundmouse_bucket",
                "card_id": SOUNDMOUSE_DOMO_CARDS["bucket"],
                "page_id": SOUNDMOUSE_DOMO_PAGE_ID,
                "description": "SoundMouse Bucket",
                "output_fn": lambda _ctx: ctx.soundmouse_bucket_csv,
            },
        ]

    output_dir = metadata_dir or (ctx.soundmouse_release_dir / "Metadata")
    return [
        {
            "key": f"soundmouse_metadata_{code}",
            "card_id": SOUNDMOUSE_DOMO_CARDS[code],
            "page_id": SOUNDMOUSE_DOMO_PAGE_ID,
            "description": f"SoundMouse Metadata {code}",
            "output_fn": (
                lambda _ctx, c=code: output_dir / metadata_filename(c)
            ),
            "format": "xlsx",
            "strip_formatting": True,
        }
        for code in codes
    ]


def _export_domo_cards(
    ctx: ReleaseContext,
    cards: list[dict],
    dry_run: bool,
    logger: logging.Logger,
) -> bool:
    if dry_run:
        for card in cards:
            logger.info(
                f"  [DRY RUN] Would export {card['description']}: "
                f"{card['output_fn'](ctx)}"
            )
        return True

    # domo_exports owns the known-good Domo interaction.  Its Playwright import
    # is deliberately initialized here, inside the real-run function.
    import domo_exports as domo

    domo.TEMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    domo._require_playwright()
    ok = True
    secure_private_directory(DOMO_PROFILE_DIR, recursive=True)
    with domo.sync_playwright() as playwright:
        with private_creation_umask():
            browser_ctx = playwright.chromium.launch_persistent_context(
                user_data_dir=str(DOMO_PROFILE_DIR),
                headless=False,
                downloads_path=str(domo.TEMP_DOWNLOAD_DIR),
                accept_downloads=True,
            )
        page = browser_ctx.new_page()
        try:
            try:
                domo._authenticate(page, logger)
            except domo.PlaywrightTimeoutError:
                logger.error(
                    "  ✗ The private Domo session requires reauthentication. "
                    "The workflow did not pause. Run python3 auth_manager.py "
                    "--enroll-domo-keychain, then python3 auth_manager.py "
                    "--setup domo outside the release "
                    "run, then rerun with --reuse-domo-seeds."
                )
                return False
            for card in cards:
                output = card["output_fn"](ctx)
                logger.info(f"  Exporting {card['description']} → {output}")
                try:
                    domo._export_card(page, card, output, ctx, logger)
                    if card.get("strip_formatting"):
                        strip_xlsx_formatting(output)
                        logger.info(f"  Removed XLSX formatting: {output.name}")
                except Exception as exc:  # browser errors are logged per card
                    logger.error(f"  ✗ {card['description']} failed: {exc}")
                    ok = False
        finally:
            browser_ctx.close()
            secure_private_directory(DOMO_PROFILE_DIR, recursive=True)
    return ok


def install_soundmouse_metadata(
    source_workbooks: list[Path],
    metadata_directory: Path,
    logger: logging.Logger,
) -> list[Path]:
    """Atomically install cleaned full-period workbooks in the delivery."""
    metadata_directory.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for source in source_workbooks:
        destination = metadata_directory / source.name
        temporary = destination.with_name(f".{destination.name}.tmp")
        try:
            shutil.copy2(source, temporary)
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        outputs.append(destination)
        logger.info(f"  Installed metadata: {destination.name}")
    return outputs


def _wav_name(value: str) -> str:
    """Return a case-insensitive WAV leaf name for tracklist comparisons."""
    name = Path(str(value).strip()).name
    if name and not name.casefold().endswith(".wav"):
        name += ".wav"
    return name.casefold()


def _partition_soundmouse_rows(
    soundmouse_csv: Path,
    us_tracklist_csv: Path,
) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]]]:
    """Split SoundMouse rows into US and non-US UniSync requests."""
    fields, rows = _read_csv(soundmouse_csv)
    filename_col = _find_column(fields, ["FILENAME", "FILE"])
    us_fields, us_rows = _read_csv(us_tracklist_csv)
    us_filename_col = _find_column(us_fields, ["FILENAME", "FILE"])
    if not filename_col or not us_filename_col:
        raise ValueError("SoundMouse and US tracklists both need a Filename column")

    us_names = {
        _wav_name(row.get(us_filename_col, ""))
        for row in us_rows
        if str(row.get(us_filename_col, "")).strip()
    }
    routed_us: list[dict[str, str]] = []
    routed_exus: list[dict[str, str]] = []
    for row in rows:
        destination = routed_us if _wav_name(row.get(filename_col, "")) in us_names else routed_exus
        destination.append(row)
    return fields, routed_us, routed_exus


def _write_soundmouse_request_csv(
    path: Path,
    fields: list[str],
    rows: list[dict[str, str]],
) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _soundmouse_unisync_jobs(
    ctx: ReleaseContext,
    us_request_csv: Path | None = None,
    exus_request_csv: Path | None = None,
) -> list[dict[str, str]]:
    """Build the additive territory jobs used by the SoundMouse delivery."""
    jobs = [
        {
            "name": f"SoundMouse {label} WAV ({ctx.soundmouse_activation_range})",
            "territory": territory,
            "cache_path": str(UPM_CACHE_WAV),
            "client_path": str(ctx.soundmouse_release_dir / "MEDIA"),
            "csv": str(request_csv or ctx.soundmouse_tracklist_csv),
        }
        for label, territory, request_csv in (
            ("US", "United States", us_request_csv),
            ("Ex-US", "Rest of World", exus_request_csv),
            ("Japan", "Japan", None),
        )
    ]
    jobs[1]["fallback_territory"] = "Japan"
    return jobs


def run_soundmouse_unisync(
    ctx: ReleaseContext,
    dry_run: bool,
    overwrite: bool,
    logger: logging.Logger,
) -> bool:
    """Run all three WAV territories into the workflow-period directory.

    SoundMouse's selected metadata workbooks can reference US, Rest-of-World,
    and Japan-only tracks.  A United States-only pass silently tops out at the
    US delivery count, so all territories must contribute to the same MEDIA
    folder.  UniSync's skip-existing behavior makes the later passes additive.
    """
    if dry_run:
        logger.info(
            "  [DRY RUN] Would route SoundMouse WAVs through US, "
            f"Rest of World, then Japan fallback → {ctx.soundmouse_release_dir / 'MEDIA'}"
        )
        return True

    from unisync_automation import STATUS_FAILED, run_all_unisync_jobs
    with tempfile.TemporaryDirectory(prefix="soundmouse_unisync_") as tmp:
        request_dir = Path(tmp)
        try:
            fields, us_rows, exus_rows = _partition_soundmouse_rows(
                ctx.soundmouse_tracklist_csv, ctx.us_tracklist_csv
            )
            us_csv = request_dir / "SoundMouse-US.csv"
            exus_csv = request_dir / "SoundMouse-ExUS.csv"
            _write_soundmouse_request_csv(us_csv, fields, us_rows)
            _write_soundmouse_request_csv(exus_csv, fields, exus_rows)
            logger.info(
                f"  SoundMouse territory routing: {len(us_rows)} US, "
                f"{len(exus_rows)} Rest-of-World/Japan fallback row(s)."
            )
            jobs = _soundmouse_unisync_jobs(ctx, us_csv, exus_csv)
        except (OSError, ValueError) as exc:
            logger.warning(
                f"  Could not partition SoundMouse requests ({exc}); "
                "falling back to full-list territory passes."
            )
            jobs = _soundmouse_unisync_jobs(ctx)

        class _Jobs:
            unisync_jobs = jobs

        results = run_all_unisync_jobs(
            _Jobs(), dry_run=dry_run, logger=logger, overwrite=overwrite
        )
    return bool(results) and not any(v == STATUS_FAILED for v in results.values())


def download_soundmouse_covers(
    tracklist_csv: Path,
    release_directory: Path,
    dry_run: bool,
    overwrite: bool,
    logger: logging.Logger,
) -> bool:
    """Download unique covers into the workflow period's delivery root."""
    fields, rows = _read_csv(tracklist_csv)
    cover_col = _find_column(fields, POSSIBLE_COVER_COLS)
    url_col = _find_column(fields, POSSIBLE_URL_COLS)
    if not cover_col or not url_col:
        logger.error("  ✗ SoundMouse tracklist needs AlbumCoverArt and CDNAlbumArt columns.")
        return False

    if not dry_run:
        import requests

    ok = True
    seen: set[str] = set()
    for row in rows:
        name = Path(str(row.get(cover_col, "")).strip()).name
        url = str(row.get(url_col, "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        destination = release_directory / "Covers" / name
        if destination.exists() and not overwrite:
            continue
        if not url:
            logger.warning(f"  No cover URL for {name}")
            ok = False
            continue
        if dry_run:
            logger.info(f"  [DRY RUN] Would download cover: {destination}")
            continue
        try:
            response = requests.get(url, timeout=45)
            response.raise_for_status()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(response.content)
        except Exception as exc:
            logger.error(f"  ✗ Cover download failed ({name}): {exc}")
            ok = False
    logger.info(
        f"  SoundMouse covers for {release_directory.name}: {len(seen)}"
    )
    return ok


def run_soundmouse_step(
    ctx: ReleaseContext,
    dry_run: bool,
    overwrite: bool,
    logger: logging.Logger,
    *,
    reuse_domo_seeds: bool = False,
) -> bool:
    logger.info(f"  Tracklist: {ctx.soundmouse_tracklist_csv}")
    logger.info(f"  Release base: {SOUNDMOUSE_BASE}")

    if dry_run:
        _export_domo_cards(ctx, _domo_configs(ctx), True, logger)
        create_soundmouse_directories_preview(ctx, logger)
        run_soundmouse_unisync(ctx, True, overwrite, logger)
        logger.info("  [DRY RUN] Covers will be derived from the exported tracklist.")
        logger.info("  [DRY RUN] Bucket-selected metadata (possible sheets):")
        _export_domo_cards(ctx, _domo_configs(ctx, list(METADATA_SHEETS)), True, logger)
        validate_soundmouse_delivery(
            [],
            ctx.soundmouse_release_dir / "MEDIA",
            ctx.soundmouse_release_dir / "Covers",
            ctx.soundmouse_validation_report,
            True,
            logger,
        )
        return True

    if reuse_domo_seeds:
        missing_seeds = [
            path for path in (
                ctx.soundmouse_tracklist_csv,
                ctx.soundmouse_bucket_csv,
            )
            if not path.is_file()
        ]
        if missing_seeds:
            logger.error(
                "  ✗ Cannot reuse SoundMouse Domo seed exports; missing: "
                + ", ".join(str(path) for path in missing_seeds)
            )
            return False
        logger.info("  ↩ Reusing existing SoundMouse tracklist and bucket exports.")
    elif not _export_domo_cards(ctx, _domo_configs(ctx), False, logger):
        return False
    try:
        root = create_soundmouse_directories(
            ctx.soundmouse_release_dir, False, logger
        )
    except (OSError, ValueError) as exc:
        logger.error(f"  ✗ SoundMouse directory setup failed: {exc}")
        return False
    try:
        codes = metadata_codes_from_bucket(ctx.soundmouse_bucket_csv)
    except (OSError, ValueError) as exc:
        logger.error(f"  ✗ Could not read SoundMouse bucket export: {exc}")
        return False
    if not codes:
        logger.error(
            "  ✗ SoundMouse bucket did not identify any known metadata sheet "
            "(01–10); refusing to guess."
        )
        return False
    logger.info(f"  SoundMouse metadata buckets: {', '.join(codes)}")
    with tempfile.TemporaryDirectory(prefix="soundmouse_metadata_") as tmp:
        staging_dir = Path(tmp)
        if not _export_domo_cards(
            ctx,
            _domo_configs(ctx, codes, metadata_dir=staging_dir),
            False,
            logger,
        ):
            return False
        source_workbooks = [
            staging_dir / metadata_filename(code) for code in codes
        ]
        if not run_soundmouse_unisync(ctx, False, overwrite, logger):
            return False
        if not download_soundmouse_covers(
            ctx.soundmouse_tracklist_csv,
            root,
            False,
            overwrite,
            logger,
        ):
            return False
        try:
            metadata_paths = install_soundmouse_metadata(
                source_workbooks,
                root / "Metadata",
                logger,
            )
        except (OSError, ValueError) as exc:
            logger.error(f"  ✗ Could not install SoundMouse metadata: {exc}")
            return False

    return validate_soundmouse_delivery(
        metadata_paths,
        root / "MEDIA",
        root / "Covers",
        ctx.soundmouse_validation_report,
        False,
        logger,
    )


def create_soundmouse_directories_preview(
    ctx: ReleaseContext, logger: logging.Logger
) -> None:
    for child in (
        ctx.soundmouse_release_dir,
        ctx.soundmouse_release_dir / "Covers",
        ctx.soundmouse_release_dir / "Metadata",
        ctx.soundmouse_release_dir / "MEDIA",
    ):
        logger.info(f"  [DRY RUN] Would create: {child}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run SoundMouse Step 16")
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--part", type=int, choices=[1, 2])
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--reuse-domo-seeds",
        action="store_true",
        help=(
            "Reuse the existing SoundMouse tracklist and bucket CSVs; "
            "metadata cards are still refreshed."
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        ctx = context_from_cli_args(args)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    return 0 if run_soundmouse_step(
        ctx,
        args.dry_run,
        args.overwrite,
        logging.getLogger("soundmouse"),
        reuse_domo_seeds=args.reuse_domo_seeds,
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
