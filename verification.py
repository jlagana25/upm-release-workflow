"""
verification.py — Step 9: Verify All Expected Files
====================================================
Refactors 4-Verify-NewReleases.py and expands its coverage.

Reads three source CSVs and confirms that every audio file and cover
referenced in them exists at its expected location under the Specials
folder.  Anything missing is written to a CSV report so the user can
fix the upstream issue (re-run UniSync, re-download covers, etc.).

Sources checked
---------------
US Tracklist            → ctx.us_tracklist_csv
Ex-US Tracklist         → ctx.exus_tracklist_csv
Japan NTT Metadata      → ctx.japan_metadata_csv

File-system layout assumed (created by Step 2 + UniSync + cover steps)
----------------------------------------------------------------------
  {specials_dir}/1-ORIGINAL/Music/MP3/MEDIA/{Label}/{AlbumNo - Title}/{Filename}.mp3
  {specials_dir}/1-ORIGINAL/Music/WAV/MEDIA/{Label}/{AlbumNo - Title}/{Filename}.wav
  {specials_dir}/1-ORIGINAL/Music/WAV w COVERS/MEDIA/{Label}/{AlbumNo - Title}/{Filename}.wav
  {specials_dir}/1-ORIGINAL/Music/WAV w COVERS/MEDIA/{Label}/{AlbumNo - Title}/{AlbumCoverArt}
  {specials_dir}/1-ORIGINAL/Covers/{AlbumCoverArt}              (flat — no subfolders)
  {specials_dir}/1-ORIGINAL/Music/Ex-US (MP3)/MEDIA/{Label}/{AlbumNo - Title}/{Filename}.mp3
  {specials_dir}/1-ORIGINAL/Music/Ex-US (WAV)/MEDIA/{Label}/{AlbumNo - Title}/{Filename}.wav
  {specials_dir}/1-ORIGINAL/Music/Japan/MEDIA/{Label}/{AlbumNo - Title}/{Filename}.wav

Album-folder lookup (per legacy script): for each (Label, AlbumNo) the
verifier looks under `{root}/{Label}/` for any directory whose name
starts with `"{AlbumNo} -"`.  If found, the audio file should live
inside that directory.  Otherwise it falls back to the bare-AlbumNo
folder name.

Missing-file report
-------------------
Written to ctx.missing_report_csv with columns:
    Type, Source CSV, WorkAudioID, Label, AlbumNo, AlbumNoMasters,
    Filename, AlbumCoverArt, Expected Path, Folder Checked, Reason

Each missing entry is deduped by (Type, Expected Path) so a cover that
fails the flat-folder check AND is also missing from a particular album
folder shows up as two separate, distinct rows — but the same cover
referenced by 80 tracks only generates one missing-cover row.

Standalone test:
    python verification.py --test --year 2026 --month 5 --part 1 [--debug]
                                                                 [--skip-verify]
                                                                 [--source us|exus|japan]
"""

from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

from config import ReleaseContext, context_from_cli_args
from filesystem_names import resolve_label_dir
from release_manifest import read_table
from tracklist_columns import (
    _find_column,
    _normalize,
    POSSIBLE_ALBUMNO_COLS,
    POSSIBLE_ALBUMNOMASTERS_COLS,
    POSSIBLE_ALBUMTITLE_COLS,
    POSSIBLE_COVER_COLS,
    POSSIBLE_FILENAME_COLS,
    POSSIBLE_LABEL_COLS,
    POSSIBLE_WORKID_COLS,
)


# ---------------------------------------------------------------------------
# Column name candidates + matcher (_normalize / _find_column) now live in
# tracklist_columns (shared, imported above).
# ---------------------------------------------------------------------------

# Report column order — must match the user-specified schema exactly.
REPORT_FIELDS = [
    "Type",
    "Source CSV",
    "WorkAudioID",
    "Label",
    "AlbumNo",
    "AlbumNoMasters",
    "Filename",
    "AlbumCoverArt",
    "Expected Path",
    "Folder Checked",
    "Reason",
]


def _detect_columns(df: pd.DataFrame) -> dict[str, Optional[str]]:
    cols = list(df.columns)
    return {
        "label":           _find_column(cols, POSSIBLE_LABEL_COLS),
        "albumno":         _find_column(cols, POSSIBLE_ALBUMNO_COLS),
        "albumno_masters": _find_column(cols, POSSIBLE_ALBUMNOMASTERS_COLS),
        "albumtitle":      _find_column(cols, POSSIBLE_ALBUMTITLE_COLS),
        "filename":        _find_column(cols, POSSIBLE_FILENAME_COLS),
        "cover":           _find_column(cols, POSSIBLE_COVER_COLS),
        "workid":          _find_column(cols, POSSIBLE_WORKID_COLS),
    }


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _find_album_folders(label_dir: Path, album_no: str) -> list[Path]:
    """
    Return ALL subdirectories under label_dir whose name starts with
    "{album_no} -", sorted by name for deterministic order.

    Returning every match (not just the first) makes the file checks robust
    to duplicate / stale album folders for the same AlbumNo — which accumulate
    in derived trees like 'WAV w COVERS' that are copied into but never pruned.
    A file counts as present if it lives in ANY of these folders.
    """
    if not label_dir.is_dir():
        return []
    try:
        return sorted(
            (e for e in label_dir.iterdir()
             if e.is_dir() and e.name.startswith(f"{album_no} -")),
            key=lambda p: p.name,
        )
    except OSError:
        return []


def _find_album_folder(label_dir: Path, album_no: str) -> Optional[Path]:
    """
    First subdirectory under label_dir whose name starts with "{album_no} -",
    or None.  Retained for the missing-report path; existence checks use
    _find_album_folders (all matches).
    """
    matches = _find_album_folders(label_dir, album_no)
    return matches[0] if matches else None


def _resolve_audio_path(
    media_root: Path, label: str, album_no: str, filename: str, ext: str
) -> tuple[Path, Path, bool, bool]:
    """
    Compute where an audio file SHOULD live and whether it's actually there.
    Returns (expected_file_path, folder_checked, found_album_folder, exists).

    Existence is checked across ALL album folders matching "{album_no} -"
    (handles duplicate/stale sibling folders) plus the bare-AlbumNo fallback,
    so a file present in any of them counts.  expected_file_path /
    folder_checked describe the primary (first) album folder, used only for the
    missing-report when the file genuinely isn't found anywhere.

    Extension handling: most tracklists store the bare basename
    (e.g. "BR_848_1_Don_t_Have_to_Talk_about_It"), but Japan's NTT
    metadata stores the filename WITH the ".wav" extension already
    baked in.  If the filename already ends with the expected
    extension (case-insensitive), use it as-is; otherwise append.
    """
    suffix = f".{ext.lower()}"
    if filename.lower().endswith(suffix):
        leaf = filename
    else:
        leaf = f"{filename}.{ext}"

    label_dir = resolve_label_dir(media_root, label)
    album_dirs = _find_album_folders(label_dir, album_no)

    # Present if the leaf exists in ANY matching album folder…
    exists = any((d / leaf).exists() for d in album_dirs)
    # …or in the bare-AlbumNo fallback folder.
    fallback_dir = label_dir / album_no
    if not exists and (fallback_dir / leaf).exists():
        exists = True

    if album_dirs:
        primary = album_dirs[0]
        return primary / leaf, primary, True, exists
    return fallback_dir / leaf, label_dir, False, exists


# ---------------------------------------------------------------------------
# Per-row data extraction (handles missing columns gracefully)
# ---------------------------------------------------------------------------

def _row_value(row: dict, cols: dict[str, Optional[str]], key: str) -> str:
    """Pull a string value out of a row by role-key.  Returns '' if the
    column doesn't exist in this CSV."""
    col_name = cols.get(key)
    if not col_name:
        return ""
    val = row.get(col_name, "")
    return ("" if pd.isna(val) else str(val)).strip()


# ---------------------------------------------------------------------------
# Missing-entry construction
# ---------------------------------------------------------------------------

def _missing_entry(
    *,
    type_label: str,
    source_csv: str,
    expected_path: Path,
    folder_checked: Path,
    reason: str,
    workid: str = "",
    label: str = "",
    albumno: str = "",
    albumno_masters: str = "",
    filename: str = "",
    cover: str = "",
) -> dict[str, str]:
    return {
        "Type":            type_label,
        "Source CSV":      source_csv,
        "WorkAudioID":     workid,
        "Label":           label,
        "AlbumNo":         albumno,
        "AlbumNoMasters":  albumno_masters,
        "Filename":        filename,
        "AlbumCoverArt":   cover,
        "Expected Path":   str(expected_path),
        "Folder Checked":  str(folder_checked),
        "Reason":          reason,
    }


# ---------------------------------------------------------------------------
# Audio-file verification (one row → one check per media-type)
# ---------------------------------------------------------------------------

def _check_audio_file(
    *,
    type_label: str,
    source_csv: str,
    media_root: Path,
    extension: str,
    row_data: dict[str, str],
    seen: set[tuple[str, str]],
) -> Optional[dict[str, str]]:
    """
    Confirm one audio file exists.  Returns a missing-report entry if
    not, None if it does (or if it would be a duplicate of one already
    reported).
    """
    label = row_data["label"]
    album_no = row_data["albumno"]
    filename = row_data["filename"]

    if not label or not album_no or not filename:
        return None  # skip rows missing the basics

    expected, folder_checked, found_album_dir, exists = _resolve_audio_path(
        media_root, label, album_no, filename, extension
    )

    if exists:
        return None

    dedupe_key = (type_label, str(expected))
    if dedupe_key in seen:
        return None
    seen.add(dedupe_key)

    if not found_album_dir:
        if not (media_root / label).is_dir():
            reason = "Label folder not found"
        else:
            reason = f"Album folder starting with '{album_no} -' not found"
    else:
        reason = "Audio file missing"

    return _missing_entry(
        type_label=type_label,
        source_csv=source_csv,
        expected_path=expected,
        folder_checked=folder_checked,
        reason=reason,
        workid=row_data["workid"],
        label=label,
        albumno=album_no,
        albumno_masters=row_data["albumno_masters"],
        filename=filename,
        cover=row_data["cover"],
    )


# ---------------------------------------------------------------------------
# Cover verification (per-album, deduped — covers are 1-per-album)
# ---------------------------------------------------------------------------

def _check_flat_cover(
    *,
    source_csv: str,
    covers_dir: Path,
    cover_name: str,
    info: dict[str, str],
    seen: set[tuple[str, str]],
) -> Optional[dict[str, str]]:
    """Check {specials}/1-ORIGINAL/Covers/{AlbumCoverArt}."""
    expected = covers_dir / cover_name
    if expected.exists():
        return None
    dedupe_key = ("COVERS", str(expected))
    if dedupe_key in seen:
        return None
    seen.add(dedupe_key)
    return _missing_entry(
        type_label="COVERS",
        source_csv=source_csv,
        expected_path=expected,
        folder_checked=covers_dir,
        reason="Cover file missing from flat /Covers folder",
        label=info["label"],
        albumno=info["albumno"],
        cover=cover_name,
    )


def _check_album_cover(
    *,
    source_csv: str,
    wwc_root: Path,
    cover_name: str,
    info: dict[str, str],
    seen: set[tuple[str, str]],
) -> Optional[dict[str, str]]:
    """
    Check that the album cover lives inside the album's WAV w COVERS folder.
    The album-folder lookup is the same '{AlbumNo} -' prefix search the
    audio checks use.
    """
    label = info["label"]
    album_no = info["albumno"]
    label_dir = resolve_label_dir(wwc_root, label)
    album_dirs = _find_album_folders(label_dir, album_no)

    if not album_dirs:
        expected = label_dir / album_no / cover_name
        folder = label_dir
        if not label_dir.is_dir():
            reason = "Label folder not found in WAV w COVERS"
        else:
            reason = f"Album folder starting with '{album_no} -' not found"
    else:
        # Present if the cover exists in ANY matching album folder.
        for d in album_dirs:
            if (d / cover_name).exists():
                return None
        album_dir = album_dirs[0]
        expected = album_dir / cover_name
        folder = album_dir
        reason = "Cover file missing from album folder"

    dedupe_key = ("WAV w COVERS (COVERS)", str(expected))
    if dedupe_key in seen:
        return None
    seen.add(dedupe_key)

    return _missing_entry(
        type_label="WAV w COVERS (COVERS)",
        source_csv=source_csv,
        expected_path=expected,
        folder_checked=folder,
        reason=reason,
        label=label,
        albumno=album_no,
        cover=cover_name,
    )


# ---------------------------------------------------------------------------
# Per-CSV verifiers
# ---------------------------------------------------------------------------

def _load_csv(
    csv_path: Path, csv_label: str, logger: logging.Logger
) -> tuple[Optional[pd.DataFrame], Optional[dict[str, Optional[str]]]]:
    """
    Read the CSV and detect columns.  Returns (None, None) on failure;
    callers should treat that as a hard verification failure for that
    source.
    """
    if not csv_path.is_file():
        logger.error(f"    ✗  {csv_label} CSV not found: {csv_path}")
        return None, None
    try:
        df = read_table(csv_path)
    except Exception as exc:
        logger.error(f"    ✗  Could not read {csv_label}: {exc}")
        return None, None
    cols = _detect_columns(df)
    logger.info(
        f"    Columns: label={cols['label']!r}, albumno={cols['albumno']!r}, "
        f"filename={cols['filename']!r}, cover={cols['cover']!r}, "
        f"workid={cols['workid']!r}"
    )
    return df, cols


def _verify_us(
    ctx: ReleaseContext, logger: logging.Logger
) -> list[dict[str, str]]:
    csv_label = "US Tracklist"
    csv_path = ctx.us_tracklist_csv
    logger.info(f"  → {csv_label}: {csv_path}")

    df, cols = _load_csv(csv_path, csv_label, logger)
    if df is None:
        return [_missing_entry(
            type_label="MISSING_CSV",
            source_csv=csv_label,
            expected_path=csv_path,
            folder_checked=csv_path.parent,
            reason="Tracklist CSV not found — run Step 1 (Domo export) first.",
        )]

    # Required columns: label, albumno, filename.  Without any of these
    # we cannot construct expected paths — bail out for the whole CSV.
    for required in ("label", "albumno", "filename"):
        if not cols[required]:
            logger.error(
                f"    ✗  Required column '{required}' not found in {csv_label}."
            )
            return [_missing_entry(
                type_label="MISSING_COLUMN",
                source_csv=csv_label,
                expected_path=csv_path,
                folder_checked=csv_path.parent,
                reason=f"CSV is missing a usable {required.upper()} column.",
            )]

    music_root = ctx.specials_dir / "1-ORIGINAL" / "Music"
    mp3_root    = music_root / "MP3" / "MEDIA"
    wav_root    = music_root / "WAV" / "MEDIA"
    wwc_root    = music_root / "WAV w COVERS" / "MEDIA"
    covers_dir  = ctx.specials_dir / "1-ORIGINAL" / "Covers"

    missing: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    # ---- Audio checks per row ------------------------------------------
    audio_checks = [
        ("MP3",                  mp3_root, "mp3"),
        ("WAV",                  wav_root, "wav"),
        ("WAV w COVERS (MEDIA)", wwc_root, "wav"),
    ]
    for _, row in df.iterrows():
        row_data = {
            "label":           _row_value(row, cols, "label"),
            "albumno":         _row_value(row, cols, "albumno"),
            "albumno_masters": _row_value(row, cols, "albumno_masters"),
            "filename":        _row_value(row, cols, "filename"),
            "cover":           _row_value(row, cols, "cover"),
            "workid":          _row_value(row, cols, "workid"),
        }
        for type_label, root, ext in audio_checks:
            entry = _check_audio_file(
                type_label=type_label,
                source_csv=csv_label,
                media_root=root,
                extension=ext,
                row_data=row_data,
                seen=seen,
            )
            if entry:
                missing.append(entry)

    # ---- Cover checks deduped by AlbumCoverArt -------------------------
    if cols["cover"]:
        unique_covers = _unique_covers_for_verify(df, cols)
        logger.info(
            f"    {len(df)} rows, {len(unique_covers)} unique album cover(s)."
        )
        for cover_name, info in unique_covers.items():
            entry = _check_flat_cover(
                source_csv=csv_label,
                covers_dir=covers_dir,
                cover_name=cover_name,
                info=info,
                seen=seen,
            )
            if entry:
                missing.append(entry)

            entry = _check_album_cover(
                source_csv=csv_label,
                wwc_root=wwc_root,
                cover_name=cover_name,
                info=info,
                seen=seen,
            )
            if entry:
                missing.append(entry)
    else:
        logger.info(
            f"    {len(df)} rows, no cover column detected — skipping cover checks."
        )

    logger.info(f"    Missing for {csv_label}: {len(missing)}")
    return missing


def _verify_exus(
    ctx: ReleaseContext, logger: logging.Logger
) -> list[dict[str, str]]:
    csv_label = "Ex-US Tracklist"
    csv_path = ctx.exus_tracklist_csv
    logger.info(f"  → {csv_label}: {csv_path}")

    df, cols = _load_csv(csv_path, csv_label, logger)
    if df is None:
        return [_missing_entry(
            type_label="MISSING_CSV",
            source_csv=csv_label,
            expected_path=csv_path,
            folder_checked=csv_path.parent,
            reason="Tracklist CSV not found — run Step 1 first.",
        )]

    for required in ("label", "albumno", "filename"):
        if not cols[required]:
            logger.error(
                f"    ✗  Required column '{required}' not found in {csv_label}."
            )
            return [_missing_entry(
                type_label="MISSING_COLUMN",
                source_csv=csv_label,
                expected_path=csv_path,
                folder_checked=csv_path.parent,
                reason=f"CSV missing usable {required.upper()} column.",
            )]

    music_root = ctx.specials_dir / "1-ORIGINAL" / "Music"
    exus_mp3_root = music_root / "Ex-US (MP3)" / "MEDIA"
    exus_wav_root = music_root / "Ex-US (WAV)" / "MEDIA"

    missing: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    audio_checks = [
        ("Ex-US (MP3)", exus_mp3_root, "mp3"),
        ("Ex-US (WAV)", exus_wav_root, "wav"),
    ]
    for _, row in df.iterrows():
        row_data = {
            "label":           _row_value(row, cols, "label"),
            "albumno":         _row_value(row, cols, "albumno"),
            "albumno_masters": _row_value(row, cols, "albumno_masters"),
            "filename":        _row_value(row, cols, "filename"),
            "cover":           _row_value(row, cols, "cover"),
            "workid":          _row_value(row, cols, "workid"),
        }
        for type_label, root, ext in audio_checks:
            entry = _check_audio_file(
                type_label=type_label,
                source_csv=csv_label,
                media_root=root,
                extension=ext,
                row_data=row_data,
                seen=seen,
            )
            if entry:
                missing.append(entry)

    logger.info(f"    Missing for {csv_label}: {len(missing)}")
    return missing


def _verify_japan(
    ctx: ReleaseContext, logger: logging.Logger
) -> list[dict[str, str]]:
    csv_label = "Japan Metadata"
    csv_path = ctx.japan_metadata_csv
    logger.info(f"  → {csv_label}: {csv_path}")

    df, cols = _load_csv(csv_path, csv_label, logger)
    if df is None:
        return [_missing_entry(
            type_label="MISSING_CSV",
            source_csv=csv_label,
            expected_path=csv_path,
            folder_checked=csv_path.parent,
            reason="Japan metadata CSV not found — run Step 1 first.",
        )]

    for required in ("label", "albumno", "filename"):
        if not cols[required]:
            logger.error(
                f"    ✗  Required column '{required}' not found in {csv_label}."
            )
            return [_missing_entry(
                type_label="MISSING_COLUMN",
                source_csv=csv_label,
                expected_path=csv_path,
                folder_checked=csv_path.parent,
                reason=f"CSV missing usable {required.upper()} column.",
            )]

    japan_root = ctx.specials_dir / "1-ORIGINAL" / "Music" / "Japan" / "MEDIA"

    missing: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for _, row in df.iterrows():
        row_data = {
            "label":           _row_value(row, cols, "label"),
            "albumno":         _row_value(row, cols, "albumno"),
            "albumno_masters": "",
            "filename":        _row_value(row, cols, "filename"),
            "cover":           "",
            "workid":          _row_value(row, cols, "workid"),
        }
        entry = _check_audio_file(
            type_label="Japan WAV",
            source_csv=csv_label,
            media_root=japan_root,
            extension="wav",
            row_data=row_data,
            seen=seen,
        )
        if entry:
            missing.append(entry)

    logger.info(f"    Missing for {csv_label}: {len(missing)}")
    return missing


# ---------------------------------------------------------------------------
# Helper: dedupe rows down to unique covers + their album info
# ---------------------------------------------------------------------------

def _unique_covers_for_verify(
    df: pd.DataFrame, cols: dict[str, Optional[str]]
) -> dict[str, dict[str, str]]:
    """
    Collapse to one entry per AlbumCoverArt filename.  Returns
    {cover_filename: {label, albumno}}.  Same idea as covers._unique_covers
    but only needs Label and AlbumNo for verification.
    """
    out: dict[str, dict[str, str]] = {}
    cover_col = cols["cover"]
    label_col = cols["label"]
    albumno_col = cols["albumno"]
    if not (cover_col and label_col and albumno_col):
        return out
    for _, row in df.iterrows():
        cover = str(row.get(cover_col, "")).strip()
        label = str(row.get(label_col, "")).strip()
        if not cover or not label or cover in out:
            continue
        out[cover] = {
            "label":   label,
            "albumno": str(row.get(albumno_col, "")).strip(),
        }
    return out


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def _write_missing_report(
    rows: list[dict[str, str]],
    ctx: ReleaseContext,
    logger: logging.Logger,
) -> Path:
    report_path = ctx.missing_report_csv
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        for row in rows:
            # writer.writerow handles missing keys silently because we
            # build entries via _missing_entry which always supplies all
            # fields — but be defensive anyway.
            writer.writerow({k: row.get(k, "") for k in REPORT_FIELDS})
    logger.warning(f"  Missing-files report: {report_path}")
    return report_path


# ---------------------------------------------------------------------------
# Public entry point — matches the orchestrator's call signature
# ---------------------------------------------------------------------------

def verify_all_files(
    ctx: ReleaseContext,
    dry_run: bool,
    logger: logging.Logger,
    *,
    findings_out: Optional[list[dict[str, str]]] = None,
) -> bool:
    """
    Step 9 — Verify every audio file and cover referenced in the three
    source CSVs exists at its expected location.

    Returns True only if no missing files were found.  Writes a CSV
    report to ctx.missing_report_csv if any are missing.

    `dry_run` is accepted for API symmetry with other steps; verification
    is read-only either way, so the flag has no effect beyond a log line.
    """
    logger.info("Step 9 — Verify all expected files")
    logger.info(f"  Specials dir:   {ctx.specials_dir}")
    logger.info(f"  Missing report: {ctx.missing_report_csv}")
    if dry_run:
        logger.info("  (dry-run flag set — verification is already read-only)")

    # In a from-scratch dry run the Specials tree hasn't been built yet, so a
    # verification scan would report every expected file as "missing" and fail
    # the step.  That's noise, not a real result — note it and treat as a
    # dry-run no-op so a full-pipeline preview can complete.
    if dry_run and not ctx.specials_dir.exists():
        logger.warning(
            f"  ⚠ Specials tree not present yet: {ctx.specials_dir}\n"
            "     (produced by Steps 2–10).  Skipping verification preview."
        )
        return True

    all_missing: list[dict[str, str]] = []
    all_missing.extend(_verify_us(ctx, logger))
    all_missing.extend(_verify_exus(ctx, logger))
    all_missing.extend(_verify_japan(ctx, logger))
    if findings_out is not None:
        findings_out.extend(all_missing)

    if not all_missing:
        logger.info("  ✓  All expected files present — nothing missing.")
        return True

    if dry_run:
        logger.warning(
            f"  [DRY RUN] Would write missing-files report: {ctx.missing_report_csv}"
        )
    else:
        _write_missing_report(all_missing, ctx, logger)

    # Summary by Type for quick scanning
    by_type: dict[str, int] = {}
    for row in all_missing:
        by_type[row["Type"]] = by_type.get(row["Type"], 0) + 1
    logger.error(f"  ✗  {len(all_missing)} missing file(s):")
    for type_label, count in sorted(by_type.items()):
        logger.error(f"      {count:5d}  {type_label}")
    return False


# ---------------------------------------------------------------------------
# Standalone test entry point
# ---------------------------------------------------------------------------

def _run_test(args) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("verification_test")

    ctx = context_from_cli_args(args)
    logger.info(f"Release context: {ctx}")

    if args.skip_verify:
        logger.info("Skipped — --skip-verify set; nothing to do.")
        sys.exit(0)

    if args.source == "us":
        missing = _verify_us(ctx, logger)
    elif args.source == "exus":
        missing = _verify_exus(ctx, logger)
    elif args.source == "japan":
        missing = _verify_japan(ctx, logger)
    else:
        ok = verify_all_files(ctx, dry_run=False, logger=logger)
        sys.exit(0 if ok else 1)

    # Single-source mode: still produce a report so the user can inspect
    if missing:
        _write_missing_report(missing, ctx, logger)
        by_type: dict[str, int] = {}
        for row in missing:
            by_type[row["Type"]] = by_type.get(row["Type"], 0) + 1
        logger.error(f"  ✗  {len(missing)} missing for source '{args.source}':")
        for t, c in sorted(by_type.items()):
            logger.error(f"      {c:5d}  {t}")
        sys.exit(1)
    logger.info(f"  ✓  No missing files for source '{args.source}'.")
    sys.exit(0)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description="Verify all expected music/cover files (Step 9)."
    )
    p.add_argument("--test",    action="store_true", required=True)
    p.add_argument("--year",    type=int)
    p.add_argument("--month",   type=int)
    p.add_argument("--part",    type=int, choices=[1, 2])
    p.add_argument(
        "--previous-month", action="store_true",
        help="Full-month run for the previous month "
             "(no Part split). Relative to today, or to "
             "--year/--month if given.")
    p.add_argument(
        "--source",
        choices=["us", "exus", "japan", "all"],
        default="all",
        help="Verify only one source CSV (default: all).",
    )
    p.add_argument("--skip-verify", action="store_true",
                   help="Exit immediately without verifying.")
    p.add_argument("--debug",   action="store_true",
                   help="Verbose logging.")

    args = p.parse_args()
    _run_test(args)
