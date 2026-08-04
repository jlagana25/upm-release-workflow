"""
soundmouse.py — Step 16: Build the SoundMouse release delivery.

The step exports the SoundMouse tracklist and bucket from Domo, creates the
ActivationRange/Covers/Metadata/MEDIA tree, downloads WAVs with UniSync and
cover art from the tracklist, then exports only the metadata workbooks named
by the bucket card.

All browser and GUI dependencies remain lazy so this module imports headless.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import tempfile
from contextlib import contextmanager
from copy import copy
from pathlib import Path

from config import (
    MASTERS_COVERS_DIR,
    SOUNDMOUSE_BASE,
    SOUNDMOUSE_DOMO_CARDS,
    SOUNDMOUSE_DOMO_PAGE_ID,
    UPM_CACHE_WAV,
    ReleaseContext,
    context_from_cli_args,
)
from cover_downloads import (
    copy_cached_cover,
    download_image_atomic,
    find_cached_cover,
)
from tracklist_columns import (
    POSSIBLE_COVER_COLS,
    POSSIBLE_FILENAME_COLS,
    POSSIBLE_LABEL_COLS,
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
        worksheet.sheet_view.showGridLines = True
        worksheet.sheet_view.zoomScale = None
        worksheet.sheet_view.zoomScaleNormal = None
        for table in worksheet.tables.values():
            table.tableStyleInfo = None

    temp_path = path.with_name(f".{path.stem}.unformatted.tmp.xlsx")
    try:
        workbook.save(temp_path)
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
    tracklist_csv: Path,
    base_directory: Path,
    dry_run: bool,
    logger: logging.Logger,
) -> list[Path]:
    """Headless replacement for SM-create_new_directories.py."""
    ranges = activation_ranges_from_tracklist(tracklist_csv)
    if not ranges:
        logger.warning("  SoundMouse tracklist has no ActivationRange values.")
        return []

    roots = [base_directory / value for value in ranges]
    for root in roots:
        for child in (root, root / "Covers", root / "Metadata", root / "MEDIA"):
            if dry_run:
                logger.info(f"  [DRY RUN] Would create: {child}")
            else:
                child.mkdir(parents=True, exist_ok=True)
        logger.info(f"  {'Would prepare' if dry_run else 'Prepared'}: {root}")
    return roots


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


def remove_stale_soundmouse_metadata(
    metadata_dir: Path,
    selected_codes: list[str],
    dry_run: bool,
    logger: logging.Logger,
) -> int:
    """Remove only generated SoundMouse workbooks not selected by this bucket."""
    expected = {metadata_filename(code) for code in selected_codes}
    generated = {metadata_filename(f"{code:02d}") for code in range(1, 11)}
    stale = sorted(
        metadata_dir / name for name in generated - expected
        if (metadata_dir / name).is_file()
    ) if metadata_dir.is_dir() else []
    for path in stale:
        if dry_run:
            logger.info(f"  [DRY RUN] Would remove stale metadata: {path.name}")
        else:
            path.unlink()
            logger.info(f"  Removed stale generated metadata: {path.name}")
    return len(stale)


def _domo_configs(ctx: ReleaseContext, codes: list[str] | None = None) -> list[dict]:
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

    return [
        {
            "key": f"soundmouse_metadata_{code}",
            "card_id": SOUNDMOUSE_DOMO_CARDS[code],
            "page_id": SOUNDMOUSE_DOMO_PAGE_ID,
            "description": f"SoundMouse Metadata {code}",
            "output_fn": (
                lambda _ctx, c=code: ctx.soundmouse_release_dir
                / "Metadata" / metadata_filename(c)
            ),
            "format": "xlsx",
            "postprocess": strip_xlsx_formatting,
        }
        for code in codes
    ]


def domo_seed_cards(ctx: ReleaseContext) -> list[dict]:
    """Cards required before SoundMouse metadata selection is known."""
    return _domo_configs(ctx)


def domo_metadata_cards_from_bucket(ctx: ReleaseContext) -> list[dict]:
    """Resolve bucket-selected cards after the seed exports complete."""
    codes = metadata_codes_from_bucket(ctx.soundmouse_bucket_csv)
    if not codes:
        raise ValueError("SoundMouse bucket selected no known metadata sheets")
    return _domo_configs(ctx, codes)


def _export_domo_cards(
    ctx: ReleaseContext,
    cards: list[dict],
    dry_run: bool,
    logger: logging.Logger,
    followup_cards=None,
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
    with domo.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=False, downloads_path=str(domo.TEMP_DOWNLOAD_DIR)
        )
        browser_ctx = browser.new_context(accept_downloads=True)
        page = browser_ctx.new_page()
        try:
            domo._authenticate(page, logger)
            def export_batch(batch):
                nonlocal ok
                for card in batch:
                    output = card["output_fn"](ctx)
                    logger.info(f"  Exporting {card['description']} → {output}")
                    try:
                        domo._export_card(page, card, output, ctx, logger)
                        postprocess = card.get("postprocess")
                        if postprocess:
                            postprocess(output)
                            logger.info(
                                f"  Removed XLSX formatting: {output.name}"
                            )
                    except Exception as exc:
                        logger.error(f"  ✗ {card['description']} failed: {exc}")
                        ok = False

            export_batch(cards)
            if ok and followup_cards:
                export_batch(followup_cards())
        finally:
            browser.close()
    return ok


def _rows_by_range(path: Path) -> tuple[list[str], dict[str, list[dict[str, str]]]]:
    fields, rows = _read_csv(path)
    activation_col = _find_column(fields, ["ACTIVATIONRANGE"])
    if not activation_col:
        raise ValueError("SoundMouse tracklist has no ActivationRange column")
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        value = str(row.get(activation_col, "")).strip()
        if value:
            grouped.setdefault(value, []).append(row)
    return fields, grouped


def run_soundmouse_unisync(
    ctx: ReleaseContext,
    dry_run: bool,
    overwrite: bool,
    logger: logging.Logger,
) -> bool:
    """Run one isolated UniSync WAV job per ActivationRange."""
    if dry_run:
        logger.info(
            f"  [DRY RUN] Would run SoundMouse WAV UniSync → "
            f"{ctx.soundmouse_release_dir / 'MEDIA'}"
        )
        return True

    with soundmouse_job_batch(ctx) as jobs:
        class _Jobs:
            unisync_jobs = jobs

        from unisync_automation import STATUS_FAILED, run_all_unisync_jobs
        results = run_all_unisync_jobs(
            _Jobs(), dry_run=dry_run, logger=logger, overwrite=overwrite
        )
        return bool(results) and not any(
            value == STATUS_FAILED for value in results.values()
        )


@contextmanager
def soundmouse_job_batch(ctx: ReleaseContext):
    """Yield SoundMouse UniSync jobs and clean any split request CSVs."""
    fields, grouped = _rows_by_range(ctx.soundmouse_tracklist_csv)
    temp_paths: list[Path] = []
    jobs: list[dict] = []
    try:
        for activation_range, rows in grouped.items():
            csv_path = ctx.soundmouse_tracklist_csv
            if len(grouped) > 1:
                handle = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".csv", prefix="soundmouse_",
                    encoding="utf-8-sig", newline="", delete=False,
                )
                with handle:
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
                csv_path = Path(handle.name)
                temp_paths.append(csv_path)
            jobs.append({
                "name": f"SoundMouse WAV ({activation_range})",
                "territory": "United States",
                "cache_path": str(UPM_CACHE_WAV),
                "client_path": str(SOUNDMOUSE_BASE / activation_range / "MEDIA"),
                "csv": str(csv_path),
            })
        yield jobs
    finally:
        for path in temp_paths:
            try:
                path.unlink()
            except OSError:
                pass


def download_soundmouse_covers(
    tracklist_csv: Path,
    base_directory: Path,
    dry_run: bool,
    overwrite: bool,
    logger: logging.Logger,
) -> bool:
    """Download each unique cover into its row's ActivationRange/Covers."""
    fields, grouped = _rows_by_range(tracklist_csv)
    cover_col = _find_column(fields, POSSIBLE_COVER_COLS)
    url_col = _find_column(fields, POSSIBLE_URL_COLS)
    label_col = _find_column(fields, POSSIBLE_LABEL_COLS)
    if not cover_col or not url_col:
        logger.error("  ✗ SoundMouse tracklist needs AlbumCoverArt and CDNAlbumArt columns.")
        return False

    if not dry_run:
        import requests
        session = requests.Session()
    else:
        session = None

    ok = True
    for activation_range, rows in grouped.items():
        seen: set[str] = set()
        for row in rows:
            name = Path(str(row.get(cover_col, "")).strip()).name
            url = str(row.get(url_col, "")).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            destination = base_directory / activation_range / "Covers" / name
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
                cached = None
                if label_col:
                    from filesystem_names import resolve_label_dir
                    label = str(row.get(label_col, "")).strip()
                    candidate = resolve_label_dir(
                        MASTERS_COVERS_DIR, label
                    ) / name
                    if candidate.is_file():
                        cached = candidate
                if cached is None:
                    cached = find_cached_cover(MASTERS_COVERS_DIR, name)
                if cached is not None:
                    copy_cached_cover(cached, destination)
                    logger.info(f"  Reused cached cover: {name}")
                else:
                    download_image_atomic(
                        url, destination, timeout=45, session=session
                    )
            except Exception as exc:
                logger.error(f"  ✗ Cover download failed ({name}): {exc}")
                ok = False
        logger.info(f"  SoundMouse covers for {activation_range}: {len(seen)}")
    if session is not None:
        session.close()
    return ok


def run_soundmouse_step(
    ctx: ReleaseContext,
    dry_run: bool,
    overwrite: bool,
    logger: logging.Logger,
    *,
    domo_prepared: bool = False,
    unisync_prepared: bool = False,
    prepared_codes: list[str] | None = None,
) -> bool:
    logger.info(f"  Tracklist: {ctx.soundmouse_tracklist_csv}")
    logger.info(f"  Release:   {ctx.soundmouse_release_dir}")

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

    if not domo_prepared:
        if not _export_domo_cards(
            ctx,
            _domo_configs(ctx),
            False,
            logger,
            followup_cards=lambda: domo_metadata_cards_from_bucket(ctx),
        ):
            return False
    else:
        logger.info("  ↩ SoundMouse Domo exports reused from Step 1 session.")
    try:
        roots = create_soundmouse_directories(
            ctx.soundmouse_tracklist_csv, SOUNDMOUSE_BASE, False, logger
        )
    except (OSError, ValueError) as exc:
        logger.error(f"  ✗ SoundMouse directory setup failed: {exc}")
        return False
    if ctx.soundmouse_release_dir not in roots:
        logger.error(
            f"  ✗ Tracklist does not contain the requested ActivationRange "
            f"{ctx.soundmouse_activation_range}."
        )
        return False
    if not unisync_prepared:
        if not run_soundmouse_unisync(ctx, False, overwrite, logger):
            return False
    else:
        logger.info("  ↩ SoundMouse WAV acquisition reused from Step 5 batch.")
    if not download_soundmouse_covers(
        ctx.soundmouse_tracklist_csv, SOUNDMOUSE_BASE, False, overwrite, logger
    ):
        return False

    try:
        codes = prepared_codes or metadata_codes_from_bucket(
            ctx.soundmouse_bucket_csv
        )
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
    try:
        remove_stale_soundmouse_metadata(
            ctx.soundmouse_release_dir / "Metadata", codes, False, logger
        )
    except OSError as exc:
        logger.error(f"  ✗ Could not remove stale SoundMouse metadata: {exc}")
        return False
    metadata_paths = [
        ctx.soundmouse_release_dir / "Metadata" / metadata_filename(code)
        for code in codes
    ]
    return validate_soundmouse_delivery(
        metadata_paths,
        ctx.soundmouse_release_dir / "MEDIA",
        ctx.soundmouse_release_dir / "Covers",
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
        ctx, args.dry_run, args.overwrite, logging.getLogger("soundmouse")
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
