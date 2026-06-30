"""
covers.py — Steps 6, 7, 8: Album Cover Download and Distribution
================================================================
Refactors Album_Cover_Manager.py and 3-Copy-Covers_to_WAVwCOVERS.py into
three discrete, idempotent pipeline steps.

Step 6 — Download covers from CDN URLs to the master library
    Source CSV: ctx.us_tracklist_csv
    Destination: /Volumes/Pegasus32 R8 - 1/UPM-US-Masters/Covers/{Label}/{AlbumCoverArt}
    Behavior:    Skip files that already exist unless `overwrite=True`.
                 Stream-download with timeout, write to temp then atomic
                 rename so a half-finished download never corrupts a real
                 file.  Failures collected and exported to a CSV report.

Step 7 — Flatten covers into the Specials Covers folder
    Source:      /Volumes/Pegasus32 R8 - 1/UPM-US-Masters/Covers/{Label}/{AlbumCoverArt}
    Destination: ctx.specials_dir / "1-ORIGINAL" / "Covers" / {AlbumCoverArt}
    Behavior:    Only copies covers actually referenced in the tracklist.
                 Flat layout — no Label subfolders.  Idempotent overwrite.

Step 8 — Distribute covers into WAV w COVERS album folders
    Source:      ctx.specials_dir / "1-ORIGINAL" / "Covers" / {AlbumCoverArt}
    Destination: ctx.specials_dir / "1-ORIGINAL" / "Music" / "WAV w COVERS"
                 / "MEDIA" / {Label} / {AlbumNo - AlbumTitle} / {AlbumCoverArt}
    Behavior:    Reuses any existing folder under {Label}/ that already
                 starts with "{AlbumNo} -" (so we don't create a parallel
                 folder when UniSync already laid down a slightly-different
                 album-title spelling).

Flexible column-name detection (matched against the normalized form
without spaces or underscores) handles every Domo export variant we've
seen.  Exact matches are tried first; substring fallback is second.

Standalone test:
    python covers.py --test --year 2026 --month 5 --part 1 [--step 6|7|8|all]
                                                            [--dry-run]
                                                            [--overwrite]
                                                            [--skip-covers]
                                                            [--debug]
"""

from __future__ import annotations

import csv
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import pandas as pd
import requests

from config import EXPORTS_DIR, MASTERS_COVERS_DIR, ReleaseContext, context_from_cli_args
from tracklist_columns import (
    _find_column,
    _normalize,
    POSSIBLE_ALBUMNO_COLS,
    POSSIBLE_ALBUMTITLE_COLS,
    POSSIBLE_COVER_COLS,
    POSSIBLE_LABEL_COLS,
    POSSIBLE_URL_COLS,
)


# ---------------------------------------------------------------------------
# Column name candidates + matcher now live in tracklist_columns (shared,
# imported above).
# ---------------------------------------------------------------------------

# Network settings for Step 6 downloads
DOWNLOAD_TIMEOUT_SEC = 30
DOWNLOAD_CHUNK_BYTES = 8192


# ---------------------------------------------------------------------------
# CSV reading + column detection (_normalize / _find_column imported above)
# ---------------------------------------------------------------------------

def _detect_columns(df: pd.DataFrame) -> dict[str, Optional[str]]:
    """
    Return the original column names that map to each role we need.
    Roles for which no column was found are returned as None.
    """
    cols = list(df.columns)
    return {
        "label":      _find_column(cols, POSSIBLE_LABEL_COLS),
        "albumno":    _find_column(cols, POSSIBLE_ALBUMNO_COLS),
        "albumtitle": _find_column(cols, POSSIBLE_ALBUMTITLE_COLS),
        "cover":      _find_column(cols, POSSIBLE_COVER_COLS),
        "url":        _find_column(cols, POSSIBLE_URL_COLS),
    }


def _load_tracklist(
    csv_path: Path, logger: logging.Logger
) -> tuple[pd.DataFrame, dict[str, Optional[str]]]:
    """
    Read the tracklist CSV and detect its columns.  Returns the
    (dataframe, role→column-name dict) tuple.  Raises FileNotFoundError if
    the CSV is missing — callers should let this propagate so the user
    knows to run Step 1 first.
    """
    if not csv_path.is_file():
        raise FileNotFoundError(
            f"Tracklist CSV not found: {csv_path}\n"
            f"  Run Step 1 (Domo exports) before Steps 6–8."
        )

    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")
    cols = _detect_columns(df)
    logger.info(
        f"  CSV columns detected:  "
        f"label={cols['label']!r}, albumno={cols['albumno']!r}, "
        f"title={cols['albumtitle']!r}, cover={cols['cover']!r}, "
        f"url={cols['url']!r}"
    )
    return df, cols


def _unique_covers(
    df: pd.DataFrame, cols: dict[str, Optional[str]]
) -> dict[str, dict[str, str]]:
    """
    Collapse the tracklist DataFrame to one entry per unique album cover.
    Returns {AlbumCoverArt_filename: {label, albumno, albumtitle, url}}.
    Rows with no Label or no AlbumCoverArt are dropped.
    """
    out: dict[str, dict[str, str]] = {}
    cover_col = cols["cover"]
    label_col = cols["label"]

    if not cover_col or not label_col:
        return out  # caller will check for required columns

    for _, row in df.iterrows():
        cover = str(row.get(cover_col, "")).strip()
        label = str(row.get(label_col, "")).strip()
        if not cover or not label or cover in out:
            continue
        out[cover] = {
            "label":      label,
            "albumno":    str(row.get(cols["albumno"] or "",    "")).strip(),
            "albumtitle": str(row.get(cols["albumtitle"] or "", "")).strip(),
            "url":        str(row.get(cols["url"] or "",        "")).strip(),
        }
    return out


def _sanitize_path_component(name: str) -> str:
    """
    Make a string safe for use as a single path component on macOS.
    macOS forbids '/' (and NUL) in filenames — albums occasionally have
    slashes in their titles (e.g. "Pop / Rock").  Replace them with
    " - " and trim trailing whitespace.
    """
    return name.replace("/", " - ").rstrip()


# ---------------------------------------------------------------------------
# Step 6 — Download covers
# ---------------------------------------------------------------------------


def _add_exus_covers(
    ctx: ReleaseContext,
    covers: dict,
    logger: logging.Logger,
    *,
    what: str,
) -> dict:
    """
    Merge the Ex-US tracklist's album covers into an existing
    {AlbumCoverArt: info} dict, so Steps 6 and 7 cover Ex-US albums too.

    Ex-US album art is referenced only in the Ex-US tracklist; without this
    the Ex-US covers never reach 1-ORIGINAL/Covers and the SME WAV ExUS
    staging (and any per-album cover check) comes up empty.

    A missing or malformed Ex-US tracklist is a warning, not a failure — the
    US covers still proceed.  US entries win on the (vanishingly unlikely)
    chance of a shared AlbumCoverArt filename.
    """
    exus = ctx.exus_tracklist_csv
    try:
        df, cols = _load_tracklist(exus, logger)
    except FileNotFoundError:
        logger.warning(
            f"  ⚠ Ex-US tracklist not found — {what} covers US albums only:\n"
            f"     {exus}"
        )
        return covers
    if not cols["label"] or not cols["cover"]:
        logger.warning(
            "  ⚠ Ex-US tracklist missing Label/AlbumCoverArt columns — "
            "skipping Ex-US covers."
        )
        return covers

    exus_covers = _unique_covers(df, cols)
    before = len(covers)
    for name, info in exus_covers.items():
        covers.setdefault(name, info)
    added = len(covers) - before
    logger.info(
        f"  + Ex-US tracklist: {len(exus_covers)} cover(s), {added} new "
        f"→ {len(covers)} total."
    )
    return covers

def download_covers(
    ctx: ReleaseContext,
    dry_run: bool,
    overwrite: bool,
    logger: logging.Logger,
) -> bool:
    """
    Step 6 — Download every album cover referenced in the US Tracklist CSV
    to the master library, organized by Label.

    Returns True on success (zero failures), False otherwise.
    Successes include "skipped because already present" — only download
    errors count against success.

    A per-run failures CSV is written under EXPORTS_DIR if any download
    fails, so the user can re-run with --overwrite or fix the source URLs.
    """
    logger.info("Step 6 — Download Album Covers")
    logger.info(f"  US CSV:      {ctx.us_tracklist_csv}")
    logger.info(f"  Ex-US CSV:   {ctx.exus_tracklist_csv}")
    logger.info(f"  Destination: {MASTERS_COVERS_DIR}")
    logger.info(f"  Overwrite:   {overwrite}")
    logger.info(f"  Dry-run:     {dry_run}")

    try:
        df, cols = _load_tracklist(ctx.us_tracklist_csv, logger)
    except FileNotFoundError as exc:
        if dry_run:
            logger.warning(f"  ⚠ {exc}")
            logger.info(
                "  [DRY RUN] Skipping preview "
                "(tracklist CSV not present yet — produced by Step 1)."
            )
            return True
        logger.error(f"  ✗  {exc}")
        return False

    if not cols["label"] or not cols["cover"]:
        logger.error(
            "  ✗  Required columns missing — need a Label column and an "
            "AlbumCoverArt column at minimum.\n"
            f"     Tried: label∈{POSSIBLE_LABEL_COLS}, cover∈{POSSIBLE_COVER_COLS}"
        )
        return False
    if not cols["url"]:
        logger.error(
            "  ✗  No URL column found in the CSV — cannot download.\n"
            f"     Tried: {POSSIBLE_URL_COLS}"
        )
        return False

    covers = _unique_covers(df, cols)
    covers = _add_exus_covers(ctx, covers, logger, what="download")
    logger.info(f"  {len(covers)} unique covers to consider (US + Ex-US).")

    stats = {"downloaded": 0, "skipped_exists": 0, "skipped_no_url": 0, "failed": 0}
    failures: list[dict[str, str]] = []

    for cover_name, info in covers.items():
        url = info["url"]
        label = info["label"]
        albumno = info["albumno"]

        if not url:
            logger.debug(f"  Skip (no URL): {label}/{cover_name}")
            stats["skipped_no_url"] += 1
            continue

        label_dir = MASTERS_COVERS_DIR / label
        final_path = label_dir / cover_name

        if final_path.exists() and not overwrite:
            logger.debug(f"  Skip (exists): {label}/{cover_name}")
            stats["skipped_exists"] += 1
            continue

        if dry_run:
            logger.info(f"  [DRY] would download: {label}/{cover_name}")
            stats["downloaded"] += 1
            continue

        # Real download — atomic via temp file
        tmp_path: Optional[Path] = None
        try:
            label_dir.mkdir(parents=True, exist_ok=True)
            ext = Path(urlparse(url).path).suffix or ".jpg"
            tmp_path = label_dir / f".tmp_{albumno or cover_name}{ext}"

            r = requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT_SEC)
            r.raise_for_status()
            with tmp_path.open("wb") as f:
                for chunk in r.iter_content(DOWNLOAD_CHUNK_BYTES):
                    f.write(chunk)
            tmp_path.replace(final_path)
            tmp_path = None  # success, nothing to clean up
            logger.info(f"  ✓  Downloaded: {label}/{cover_name}")
            stats["downloaded"] += 1
        except Exception as exc:
            logger.error(f"  ✗  Failed: {label}/{cover_name} — {exc}")
            stats["failed"] += 1
            failures.append(
                {
                    "Label":         label,
                    "AlbumNo":       albumno,
                    "AlbumCoverArt": cover_name,
                    "URL":           url,
                    "Error":         str(exc),
                }
            )
        finally:
            if tmp_path is not None:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass

    if failures and not dry_run:
        _write_failures_report(failures, ctx, logger)

    logger.info(
        f"  Summary: {stats['downloaded']} downloaded, "
        f"{stats['skipped_exists']} already present, "
        f"{stats['skipped_no_url']} skipped (no URL), "
        f"{stats['failed']} failed."
    )
    return stats["failed"] == 0


def _write_failures_report(
    failures: list[dict[str, str]],
    ctx: ReleaseContext,
    logger: logging.Logger,
) -> Path:
    """Write a per-run CSV of failed downloads under EXPORTS_DIR."""
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%m-%d-%Y")
    report_path = (
        EXPORTS_DIR
        / f"UPM {ctx.month_display_folder}_Missing_Covers_{timestamp}.csv"
    )
    fieldnames = ["Label", "AlbumNo", "AlbumCoverArt", "URL", "Error"]
    with report_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(failures)
    logger.warning(f"  Failures report written: {report_path}")
    return report_path


# ---------------------------------------------------------------------------
# Step 7 — Copy covers to the flat Specials/Covers folder
# ---------------------------------------------------------------------------

def copy_covers_to_specials(
    ctx: ReleaseContext,
    dry_run: bool,
    logger: logging.Logger,
) -> bool:
    """
    Step 7 — Copy every cover referenced in the US Tracklist CSV from the
    master library into a single flat folder under the Specials root.

    No subfolders — the final filename must equal AlbumCoverArt exactly,
    because downstream consumers (Synchtank, NBC, etc.) look up files by
    that filename and a Label-prefixed name would break them.

    Always overwrites: cover images don't change after Domo publishes
    them, and re-copying is cheap.  Missing sources are reported but
    don't fail the whole step (download_covers ran first and would have
    already reported its own failures).
    """
    dest_dir = ctx.specials_dir / "1-ORIGINAL" / "Covers"
    logger.info("Step 7 — Copy Covers → Specials/Covers")
    logger.info(f"  Source base:  {MASTERS_COVERS_DIR}")
    logger.info(f"  Destination:  {dest_dir}")
    logger.info(f"  Dry-run:      {dry_run}")

    try:
        df, cols = _load_tracklist(ctx.us_tracklist_csv, logger)
    except FileNotFoundError as exc:
        if dry_run:
            logger.warning(f"  ⚠ {exc}")
            logger.info(
                "  [DRY RUN] Skipping preview "
                "(tracklist CSV not present yet — produced by Step 1)."
            )
            return True
        logger.error(f"  ✗  {exc}")
        return False

    if not cols["label"] or not cols["cover"]:
        logger.error(
            "  ✗  Required columns missing — need Label and AlbumCoverArt."
        )
        return False

    covers = _unique_covers(df, cols)
    logger.info(f"  {len(covers)} unique covers to flatten (US only).")

    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)

    stats = {"copied": 0, "missing_source": 0, "failed": 0}

    for cover_name, info in covers.items():
        label = info["label"]
        src = MASTERS_COVERS_DIR / label / cover_name
        dst = dest_dir / cover_name

        if not src.exists():
            logger.warning(f"  ✗  Missing in master library: {label}/{cover_name}")
            stats["missing_source"] += 1
            continue

        if dry_run:
            logger.info(f"  [DRY] would copy: {label}/{cover_name} → Covers/")
            stats["copied"] += 1
            continue

        try:
            shutil.copy2(src, dst)
            logger.debug(f"  ✓  {cover_name}")
            stats["copied"] += 1
        except Exception as exc:
            logger.error(f"  ✗  Copy failed for {cover_name}: {exc}")
            stats["failed"] += 1

    logger.info(
        f"  Summary: {stats['copied']} copied, "
        f"{stats['missing_source']} missing in master library, "
        f"{stats['failed']} failed."
    )
    # Missing-source is a separate concern (Step 6's job) — only real
    # copy errors fail the step.
    return stats["failed"] == 0


# ---------------------------------------------------------------------------
# Step 8 — Copy covers into WAV w COVERS album folders
# ---------------------------------------------------------------------------

def copy_covers_to_wav_with_covers(
    ctx: ReleaseContext,
    dry_run: bool,
    logger: logging.Logger,
) -> bool:
    """
    Step 8 — Drop each album cover into its album subfolder under the
    WAV w COVERS tree.  Thin wrapper over distribute_covers_into_album_folders.
    """
    dest_root = (
        ctx.specials_dir / "1-ORIGINAL" / "Music" / "WAV w COVERS" / "MEDIA"
    )
    logger.info("Step 8 — Copy Covers → WAV w COVERS album folders")
    return distribute_covers_into_album_folders(
        ctx, ctx.us_tracklist_csv, dest_root, dry_run, logger,
        what="WAV w COVERS",
    )


def distribute_covers_into_album_folders(
    ctx: ReleaseContext,
    tracklist_csv: Path,
    dest_root: Path,
    dry_run: bool,
    logger: logging.Logger,
    *,
    what: str = "",
    src_dir: Optional[Path] = None,
    src_by_label: bool = False,
) -> bool:
    """
    Drop each album's cover into its "{Label}/{AlbumNo - AlbumTitle}/" album
    folder under dest_root, using tracklist_csv for the cover→album mapping.

    Cover source:
      - Default (src_dir=None): the flat 1-ORIGINAL/Covers folder, looked up
        as src_dir/{cover}.  Used by Step 8 (WAV w COVERS, US tracklist).
      - src_by_label=True: a Label-organized library (e.g. the master covers
        dir), looked up as src_dir/{Label}/{cover}.  Used by Step 10's Ex-US
        staging so Ex-US art never has to enter the shared flat folder (which
        is US-only and feeds SynchTank / WAV w COVERS).

    Folder rules (per requirements):
      - Per album, target folder is "{AlbumNo - AlbumTitle}".
      - If an existing folder under {Label}/ already starts with
        "{AlbumNo} -" (typically created by UniSync with a slightly
        different title), reuse it instead of creating a sibling.
      - AlbumTitle is sanitized to replace '/' with ' - '.

    Always overwrites the destination cover.
    """
    if src_dir is None:
        src_dir = ctx.specials_dir / "1-ORIGINAL" / "Covers"
    tag = f" ({what})" if what else ""
    logger.info(f"  Distribute covers{tag}")
    logger.info(f"  Source covers: {src_dir}"
                f"{'  (by Label)' if src_by_label else ''}")
    logger.info(f"  Destination:   {dest_root}")
    logger.info(f"  Dry-run:       {dry_run}")

    try:
        df, cols = _load_tracklist(tracklist_csv, logger)
    except FileNotFoundError as exc:
        if dry_run:
            logger.warning(f"  ⚠ {exc}")
            logger.info(
                "  [DRY RUN] Skipping preview "
                "(tracklist CSV not present yet — produced by Step 1)."
            )
            return True
        logger.error(f"  ✗  {exc}")
        return False


    required = ["label", "albumno", "cover"]
    missing = [r for r in required if not cols[r]]
    if missing:
        logger.error(
            f"  ✗  Required columns missing in tracklist CSV: {missing}\n"
            f"     Need: Label, AlbumNo, AlbumCoverArt."
        )
        return False
    if not cols["albumtitle"]:
        logger.warning(
            "  AlbumTitle column not found — new folders will be named "
            "just by AlbumNo, e.g. '12345' instead of '12345 - Title'."
        )

    covers = _unique_covers(df, cols)
    logger.info(f"  {len(covers)} unique covers to distribute.")

    stats = {
        "copied": 0,
        "missing_source": 0,
        "no_albumno": 0,
        "reused_folder": 0,
        "created_folder": 0,
        "failed": 0,
    }

    for cover_name, info in covers.items():
        label = info["label"]
        albumno = info["albumno"]
        albumtitle = info["albumtitle"]

        if not albumno:
            logger.warning(f"  Skip (no AlbumNo): {label}/{cover_name}")
            stats["no_albumno"] += 1
            continue

        src = (src_dir / label / cover_name) if src_by_label else (src_dir / cover_name)
        if not src.exists():
            hint = "run Step 6 first?" if src_by_label else "run Step 7 first?"
            logger.warning(
                f"  ✗  Missing source cover ({hint}): {cover_name}"
            )
            stats["missing_source"] += 1
            continue

        label_dir = dest_root / label

        # Look for any existing "{albumno} - <whatever>" folder to reuse
        existing: Optional[Path] = None
        if label_dir.is_dir():
            for entry in label_dir.iterdir():
                if entry.is_dir() and entry.name.startswith(f"{albumno} -"):
                    existing = entry
                    break

        if existing is not None:
            album_dir = existing
            stats["reused_folder"] += 1
            logger.debug(f"  Reusing folder: {label}/{album_dir.name}")
        else:
            if albumtitle:
                folder_name = _sanitize_path_component(
                    f"{albumno} - {albumtitle}"
                )
            else:
                folder_name = albumno
            album_dir = label_dir / folder_name
            stats["created_folder"] += 1

        dst = album_dir / cover_name

        if dry_run:
            logger.info(
                f"  [DRY] would copy: {cover_name} → {label}/{album_dir.name}/"
            )
            stats["copied"] += 1
            continue

        try:
            album_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            logger.debug(f"  ✓  {label}/{album_dir.name}/{cover_name}")
            stats["copied"] += 1
        except Exception as exc:
            logger.error(
                f"  ✗  Copy failed for {label}/{album_dir.name}/{cover_name}: {exc}"
            )
            stats["failed"] += 1

    logger.info(
        f"  Summary: {stats['copied']} copied, "
        f"reused {stats['reused_folder']} folder(s), "
        f"created {stats['created_folder']} folder(s), "
        f"{stats['missing_source']} missing in /Covers, "
        f"{stats['no_albumno']} skipped (no AlbumNo), "
        f"{stats['failed']} failed."
    )
    return stats["failed"] == 0


# ---------------------------------------------------------------------------
# Build WAV w COVERS by copying the WAV tree (replaces a redundant UniSync
# download).  Runs in the music-export phase, BEFORE the cover steps, so the
# album folders exist for Step 8 to drop covers into — and so WAV w COVERS
# exists for final packaging even when the cover steps are skipped.
# ---------------------------------------------------------------------------

def build_wav_with_covers_from_wav(
    ctx: ReleaseContext,
    dry_run: bool,
    logger: logging.Logger,
) -> bool:
    """
    Build  <Music>/WAV w COVERS/MEDIA/  by COPYING  <Music>/WAV/MEDIA/ .

    WAV w COVERS is identical to the WAV download plus an album cover dropped
    into each album folder (the covers are added by Step 8).  Re-downloading
    the same ~5k WAVs through UniSync a second time is wasted time, so we copy
    the already-downloaded WAV tree instead, preserving the
    {Label}/{AlbumNo - AlbumTitle}/ structure.

    Idempotent: files already present at the destination are skipped, so
    re-runs and partial WAV deliveries are handled cleanly.  Returns True
    unless a copy error occurred (a missing WAV source is a no-op warning,
    not a failure).
    """
    import os

    music = ctx.specials_dir / "1-ORIGINAL" / "Music"
    src = music / "WAV" / "MEDIA"
    dst = music / "WAV w COVERS" / "MEDIA"

    logger.info("Build WAV w COVERS ← copy of WAV (no re-download)")
    logger.info(f"  Source: {src}")
    logger.info(f"  Dest:   {dst}")
    logger.info(f"  Dry-run: {dry_run}")

    if not src.is_dir():
        logger.warning(
            f"  ⚠  WAV MEDIA folder not found — nothing to copy yet:\n"
            f"     {src}\n"
            f"     (Expected after the 'US WAV' UniSync job runs.)"
        )
        return True

    copied = skipped = errors = seen = 0
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_dir = dst if rel == "." else dst / rel
        for f in files:
            if f.startswith("."):
                continue
            seen += 1
            s = Path(root) / f
            d = target_dir / f
            if d.exists():
                skipped += 1
            elif dry_run:
                copied += 1
            else:
                try:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(s, d)
                    copied += 1
                except Exception as exc:
                    logger.warning(f"  ✗  Copy failed: {f}: {exc}")
                    errors += 1
            if seen % 500 == 0:
                logger.info(
                    f"    … {seen} files: {copied} copied, "
                    f"{skipped} skipped, {errors} errors"
                )

    logger.info(
        f"  result: {copied} copied, {skipped} skipped, {errors} errors "
        f"({seen} WAV file(s) seen)"
    )
    return errors == 0


# ---------------------------------------------------------------------------
# Convenience wrapper — runs all three steps in order
# ---------------------------------------------------------------------------

def run_all_cover_steps(
    ctx: ReleaseContext,
    dry_run: bool,
    overwrite: bool,
    logger: logging.Logger,
) -> bool:
    """
    Run Steps 6 → 7 → 8 sequentially.  Stops on first failure so the
    user can fix the underlying problem before re-running later steps.
    Returns True only when all three succeeded.
    """
    if not download_covers(ctx, dry_run, overwrite, logger):
        logger.error("Step 6 failed — stopping the cover pipeline.")
        return False
    if not copy_covers_to_specials(ctx, dry_run, logger):
        logger.error("Step 7 failed — stopping the cover pipeline.")
        return False
    if not copy_covers_to_wav_with_covers(ctx, dry_run, logger):
        logger.error("Step 8 failed.")
        return False
    return True


# ---------------------------------------------------------------------------
# Standalone test entry point
# ---------------------------------------------------------------------------

def _run_test(args) -> None:
    import sys

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("covers_test")

    ctx = context_from_cli_args(args)
    logger.info(f"Release context: {ctx}")
    logger.info(f"  dry_run:   {args.dry_run}")
    logger.info(f"  overwrite: {args.overwrite}")

    if args.skip_covers:
        logger.info("Skipped — --skip-covers set; nothing to do.")
        sys.exit(0)

    if args.step == "6":
        ok = download_covers(ctx, args.dry_run, args.overwrite, logger)
    elif args.step == "7":
        ok = copy_covers_to_specials(ctx, args.dry_run, logger)
    elif args.step == "8":
        ok = copy_covers_to_wav_with_covers(ctx, args.dry_run, logger)
    else:
        ok = run_all_cover_steps(ctx, args.dry_run, args.overwrite, logger)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description="Album cover download and placement (Steps 6, 7, 8)."
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
        "--step",
        choices=["6", "7", "8", "all"],
        default="all",
        help="Run just one step (6 = download, 7 = flatten to Specials/Covers, "
             "8 = distribute into WAV w COVERS).  Default: all.",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Log every action without copying or downloading.")
    p.add_argument("--skip-covers", action="store_true",
                   help="Exit immediately without doing anything.  Useful "
                        "when the orchestrator calls this module but the "
                        "user passed --skip-covers at the top level.")
    p.add_argument("--overwrite", action="store_true",
                   help="Step 6: re-download covers that already exist in "
                        "the master library.  Steps 7 and 8 always overwrite.")
    p.add_argument("--debug",   action="store_true",
                   help="Verbose logging.")

    args = p.parse_args()
    _run_test(args)
