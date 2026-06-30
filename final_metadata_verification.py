"""
final_metadata_verification.py — Step 15: Final Packaging verification

A last sanity pass over the deliverables.  For every partner folder it confirms
that the audio actually present on disk matches an authoritative track list, and
(where applicable) that the album cover art is present too.

Audio source per partner:
  • Partners WITH their own metadata sheet  (Netmix, SynchTank, Tunesat,
    NTT Data) are checked against that sheet.
  • Partners WITHOUT a sheet (Discovery, ESPN) are checked against the original
    US tracklist.
  • The post-copy STAGING trees (SME WAV 48K NBC, SME WAV ExUS) are checked for
    BOTH media and covers — against the US / Ex-US tracklist respectively.

Cover checks (where covers are expected):
  • Netmix      — covers live alongside the audio (WAV w COVERS layout).
  • SynchTank   — covers live in a flat SynchTank/Covers folder.
  • SME staging — covers alongside the media.
  The expected cover filenames come from the relevant tracklist's cover-art
  column; the search is recursive over the partner's delivery root, so flat or
  nested cover layouts both pass.

Deliberate exclusions:
  • NBC final package — built separately and its files are renamed afterward, so
    they intentionally won't match the original metadata.
  • Cover art in 1-ORIGINAL/Covers and WAV w COVERS is verified earlier, in
    Step 9 (verification.py); it is NOT re-checked here.

Matching is by BASENAME, case-insensitive, extension-stripped, so .wav vs .mp3
vs .aif (and .jpg vs .png) never causes a false mismatch.

A "missing" item (in the list, absent on disk) is a FAILURE; an "extra" file is
a WARNING.  A per-discrepancy CSV report is written next to the other reports.

Standalone:
    python3 final_metadata_verification.py --previous-month [--dry-run]
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from config import ReleaseContext, context_from_cli_args, EXPORTS_DIR
from tracklist_columns import (
    _find_column,
    POSSIBLE_LABEL_COLS,
    POSSIBLE_ALBUMNO_COLS,
)   # shared column-name matcher + header candidates

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUDIO_EXTS = {".wav", ".mp3", ".aif", ".aiff", ".flac", ".m4a"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".bmp"}

FILENAME_COLS = [
    "FILENAME", "AUDIOFILENAME", "FILE NAME", "AUDIO FILE NAME",
    "AUDIOFILE", "WAVFILENAME", "MP3FILENAME", "TRACKFILENAME",
]
COVER_COLS = [
    "ALBUMCOVERART", "CDCOVER", "ALBUMART", "ALBUMCOVER", "CDARTWORK",
    "ARTWORK", "COVER ART", "COVER",
]

SAMPLE_CAP = 25

# Domo exports sometimes append an aggregate footer row (e.g. "count 2044",
# "Total: 511").  Anchored so it only ever matches a bare summary token + number
# — a real filename like "Count 2044.wav" keeps its extension and won't match.
_SUMMARY_RE = re.compile(
    r"^(count|totals?|sum|subtotal|grand\s*total)\s*[:=]?\s*[\d,]+$", re.I
)


# ---------------------------------------------------------------------------
# Check spec
# ---------------------------------------------------------------------------

@dataclass
class Check:
    label:        str
    audio_source: Path            # sheet or tracklist providing the filename col
    media_dir:    Path            # where the audio files live
    check_covers: bool = False
    cover_source: Optional[Path] = None   # tracklist providing the cover-art col
    cover_root:   Optional[Path] = None   # recursive search root for cover files
    cover_mode:   str = "tree"            # "tree" = present anywhere under root;
                                          # "album" = cover must sit in the same
                                          #           {Label}/{AlbumNo - …}/ folder
                                          #           as that album's audio


def _build_checks(ctx: ReleaseContext) -> list[Check]:
    pm  = ctx.partner_metadata
    pd  = ctx.partner_dirs
    us  = ctx.us_tracklist_csv
    exus = ctx.exus_tracklist_csv

    def root_of(media_dir: Path) -> Path:
        # Partner delivery root = the folder that holds Music/Covers/etc.
        return media_dir.parent

    return [
        # ---- Partners with their own metadata sheet ----
        # Netmix covers must sit in each album folder (built from WAV w COVERS).
        Check("Netmix",    pm["netmix"],    pd["netmix_music"],
              check_covers=True, cover_source=us, cover_root=pd["netmix_music"],
              cover_mode="album"),
        # SynchTank covers live in a separate flat Covers folder → present-anywhere.
        Check("SynchTank", pm["synchtank"], pd["synchtank_wav"],
              check_covers=True, cover_source=us, cover_root=root_of(pd["synchtank_wav"]),
              cover_mode="tree"),
        Check("Tunesat",   ctx.cleanup_metadata_csv, pd["tunesat_mp3"]),
        Check("NTT Data",  ctx.japan_metadata_csv,   pd["japan_final_media"]),

        # ---- Partners without a sheet → checked against the US tracklist ----
        Check("Discovery (MP3)", us, pd["discovery_mp3"]),
        Check("Discovery (WAV)", us, pd["discovery_wav"]),
        Check("ESPN",            us, pd["espn_wav"]),

        # ---- Post-copy STAGING trees ----
        # NBC staging is a plain WAV folder copy — media only, no covers.
        Check("SME WAV 48K NBC", us,   pd["nbc_staging_media"]),
        # Ex-US staging gets covers distributed into each album folder (Step 10).
        Check("SME WAV ExUS",    exus, pd["exus_staging_media"],
              check_covers=True, cover_source=exus, cover_root=pd["exus_staging_media"],
              cover_mode="album"),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _key(name: str) -> str:
    """Lowercased stem — strip any directory and the extension."""
    return Path(str(name).strip()).stem.strip().lower()


@lru_cache(maxsize=None)
def _read_df(path: Path):
    """Read a sheet/tracklist into a DataFrame.

    Memoized by path: a single Step 15 run cross-references the same source
    files many times (the US tracklist alone backs ~6 checks), so caching turns
    a dozen full reads into four.  Callers treat the result as read-only.
    `verify_final_packaging_metadata` clears this cache at the start of each run
    so re-runs in the same process never see stale data.
    """
    import pandas as pd
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(path, dtype=str).fillna("")
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")


def _column_keys(
    path: Path, candidates: list[str], what: str, logger: logging.Logger
) -> Optional[set[str]]:
    """Basename-keys from the first matching column in a sheet/tracklist."""
    try:
        df = _read_df(path)
    except Exception as exc:
        logger.error(f"      ✗ Could not read {what} source: {exc}")
        return None
    col = _find_column(list(df.columns), candidates)
    if col is None:
        logger.error(
            f"      ✗ No {what} column found (looked for {candidates}). "
            f"Columns: {list(df.columns)}"
        )
        return None
    raw = [str(v).strip() for v in df[col].tolist() if str(v).strip()]
    kept = [v for v in raw if not _SUMMARY_RE.match(v)]
    dropped = len(raw) - len(kept)
    keys = {_key(v) for v in kept}
    note = f"  (skipped {dropped} summary row(s))" if dropped else ""
    logger.info(f"      {what} column {col!r}: {len(keys)} entr(ies){note}")
    return keys


def _disk_keys(root: Path, exts: set[str]) -> set[str]:
    return {
        _key(p.name)
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in exts
    }


def _echo(logger: logging.Logger, title: str, items: list[str]) -> None:
    if not items:
        return
    logger.info(f"      {title} ({len(items)}):")
    for name in sorted(items)[:SAMPLE_CAP]:
        logger.info(f"         {name}")
    if len(items) > SAMPLE_CAP:
        logger.info(f"         … and {len(items) - SAMPLE_CAP} more")


def _check_covers_per_album(chk: "Check", logger: logging.Logger, add) -> bool:
    """Verify each album's cover sits in the SAME folder as that album's audio.

    Drives off chk.cover_source (a tracklist with Label / AlbumNo / cover
    columns): for each album it resolves {media_dir}/{Label}/{AlbumNo - …}/
    (reusing whatever folder UniSync created, matched by the "{AlbumNo} -"
    prefix) and confirms that album's cover image is present IN that folder —
    not merely somewhere under the tree.  Album folders that don't exist are
    skipped (the audio check already reports those).
    """
    try:
        df = _read_df(chk.cover_source)
    except Exception as exc:
        logger.error(f"      ✗ Could not read cover source: {exc}")
        add(chk.label, "UNREADABLE_COVER_SOURCE",
            chk.cover_source.name, str(chk.cover_source))
        return False

    cols = list(df.columns)
    label_col   = _find_column(cols, POSSIBLE_LABEL_COLS)
    albumno_col = _find_column(cols, POSSIBLE_ALBUMNO_COLS)
    cover_col   = _find_column(cols, COVER_COLS)
    if not (label_col and albumno_col and cover_col):
        logger.error(
            "      ✗ Per-album cover check needs Label, AlbumNo and cover "
            f"columns (found label={label_col!r}, albumno={albumno_col!r}, "
            f"cover={cover_col!r})."
        )
        add(chk.label, "NO_ALBUM_COLUMNS", "", str(chk.cover_source))
        return False

    # One (label, albumno) → cover per album.
    seen: set[tuple[str, str]] = set()
    albums: list[tuple[str, str, str]] = []
    for _, row in df.iterrows():
        lbl = str(row[label_col]).strip()
        ano = str(row[albumno_col]).strip()
        cov = str(row[cover_col]).strip()
        if not (lbl and ano and cov):
            continue
        if (lbl, ano) in seen:
            continue
        seen.add((lbl, ano))
        albums.append((lbl, ano, cov))

    media = chk.media_dir
    missing: list[str] = []
    checked = 0
    for lbl, ano, cov in albums:
        label_dir = media / lbl
        album_dir: Optional[Path] = None
        if label_dir.is_dir():
            for entry in label_dir.iterdir():
                if entry.is_dir() and entry.name.startswith(f"{ano} -"):
                    album_dir = entry
                    break
        if album_dir is None:
            continue   # audio folder absent → covered by the audio check
        checked += 1
        here = {
            _key(p.name)
            for p in album_dir.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        }
        if _key(cov) not in here:
            missing.append(f"{lbl}/{album_dir.name}/{cov}")

    logger.info(f"      per-album cover check: {checked} album folder(s)")
    _echo(logger, "MISSING covers (not in their album folder)", missing)
    for m in missing:
        add(chk.label, "COVER_NOT_IN_ALBUM_FOLDER", m, str(media))

    if missing:
        logger.error(
            f"      ✗ {len(missing)} cover(s) not co-located with album audio."
        )
        return False
    if checked == 0:
        logger.warning("      ⚠ No album folders found to check covers in.")
        return True
    logger.info("      ✓ Covers co-located with album audio.")
    return True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _report_unchecked_partner_folders(
    ctx: ReleaseContext, checks: list[Check], logger: logging.Logger
) -> None:
    """Informational: list 3-FINAL PACKAGING subfolders that no check covers.

    Folds in the one useful idea from the retired final_verification.py — so a
    newly-added partner folder can't be silently skipped.  Metadata-only
    deliverables (SoundExchange, Qwire, …) legitimately appear here; the signal
    to watch for is an *unexpected* folder showing up in this list.
    """
    fp_root = ctx.specials_dir / "3-FINAL PACKAGING"
    if not fp_root.exists():
        return

    covered: set[str] = set()
    for chk in checks:
        try:
            rel = chk.media_dir.relative_to(fp_root)
        except ValueError:
            continue                       # media_dir lives outside FINAL PACKAGING
        if rel.parts:
            covered.add(rel.parts[0])

    existing = {d.name for d in fp_root.iterdir() if d.is_dir()}
    unchecked = sorted(existing - covered)
    if unchecked:
        logger.info(
            "\n  Partner folders with no audio cross-check "
            "(metadata-only or unconfigured):\n      "
            + ", ".join(unchecked)
        )


def verify_final_packaging_metadata(
    ctx: ReleaseContext,
    logger: logging.Logger,
    dry_run: bool = False,
) -> bool:
    logger.info("  Final packaging verification (audio + covers vs track lists).")
    logger.info(
        "  (NBC excluded — built separately & renamed.  1-ORIGINAL/Covers and "
        "WAV w COVERS covers are checked in Step 9.)"
    )
    _read_df.cache_clear()   # fresh reads each run; dedups within the run

    report_rows: list[dict] = []
    overall_ok = True
    checked = skipped = 0

    def add(partner, typ, name, detail):
        report_rows.append(
            {"Partner": partner, "Type": typ, "Name": name, "Detail": detail}
        )

    checks = _build_checks(ctx)
    for chk in checks:
        logger.info(f"\n  ── {chk.label} ──")
        logger.info(f"      media:  {chk.media_dir}")

        if not chk.media_dir.exists():
            logger.info(
                "      ↩  Media folder not present — skipping (delivery doesn't "
                "use this folder, or the copy hasn't run yet)."
            )
            skipped += 1
            continue
        checked += 1

        # ---- Audio ----------------------------------------------------------
        logger.info(f"      audio source: {chk.audio_source.name}")
        if not chk.audio_source.exists():
            logger.error("      ✗ Audio source list is MISSING.")
            add(chk.label, "MISSING_SOURCE_LIST", chk.audio_source.name,
                str(chk.audio_source))
            overall_ok = False
        else:
            want = _column_keys(chk.audio_source, FILENAME_COLS, "filename", logger)
            if want is None:
                add(chk.label, "UNREADABLE_SOURCE", chk.audio_source.name,
                    str(chk.audio_source))
                overall_ok = False
            else:
                have = _disk_keys(chk.media_dir, AUDIO_EXTS)
                logger.info(f"      audio files on disk: {len(have)}")
                missing = sorted(want - have)
                extra   = sorted(have - want)
                _echo(logger, "MISSING audio (listed, not on disk)", missing)
                _echo(logger, "EXTRA audio (on disk, not listed)", extra)
                for k in missing:
                    add(chk.label, "MISSING_AUDIO", k, str(chk.media_dir))
                for k in extra:
                    add(chk.label, "EXTRA_AUDIO", k, str(chk.media_dir))
                if missing:
                    logger.error(f"      ✗ {len(missing)} track(s) missing audio.")
                    overall_ok = False
                elif not have:
                    logger.warning("      ⚠ No audio files found in media folder.")
                else:
                    logger.info("      ✓ Audio matches.")

        # ---- Covers ---------------------------------------------------------
        if chk.check_covers and chk.cover_source and chk.cover_root:
            logger.info(f"      cover source: {chk.cover_source.name}")
            if not chk.cover_source.exists():
                logger.error("      ✗ Cover source list is MISSING.")
                add(chk.label, "MISSING_COVER_LIST", chk.cover_source.name,
                    str(chk.cover_source))
                overall_ok = False
            elif chk.cover_mode == "album":
                overall_ok = _check_covers_per_album(chk, logger, add) and overall_ok
            else:
                logger.info(f"      cover search: {chk.cover_root}")
                want_cov = _column_keys(
                    chk.cover_source, COVER_COLS, "cover", logger
                )
                if want_cov is None:
                    add(chk.label, "NO_COVER_COLUMN", chk.cover_source.name,
                        str(chk.cover_source))
                    overall_ok = False
                else:
                    have_cov = _disk_keys(chk.cover_root, IMAGE_EXTS)
                    logger.info(f"      cover images on disk: {len(have_cov)}")
                    miss_cov = sorted(want_cov - have_cov)
                    _echo(logger, "MISSING covers (listed, not on disk)", miss_cov)
                    for k in miss_cov:
                        add(chk.label, "MISSING_COVER", k, str(chk.cover_root))
                    if miss_cov:
                        logger.error(
                            f"      ✗ {len(miss_cov)} cover(s) missing."
                        )
                        overall_ok = False
                    elif not have_cov:
                        logger.warning("      ⚠ No cover images found.")
                    else:
                        logger.info("      ✓ Covers present.")

    # ---- Report + summary ---------------------------------------------------
    if report_rows and not dry_run:
        from datetime import date
        stamp = date.today().strftime("%m-%d-%Y")
        report_path = (
            EXPORTS_DIR
            / f"UPM {ctx.month_display_folder}_FinalPackagingCheck_{stamp}.csv"
        )
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(
                    f, fieldnames=["Partner", "Type", "Name", "Detail"]
                )
                w.writeheader()
                w.writerows(report_rows)
            logger.info(f"\n  Discrepancy report: {report_path}")
        except OSError as exc:
            logger.error(f"  Could not write discrepancy report: {exc}")

    _report_unchecked_partner_folders(ctx, checks, logger)

    logger.info(
        f"\n  ─── Final packaging verification summary ───\n"
        f"    Checked:        {checked}\n"
        f"    Skipped:        {skipped} (folder not present)\n"
        f"    Discrepancies:  {len(report_rows)}\n"
        f"    Result:         {'✓ PASS' if overall_ok else '✗ FAIL'}"
    )
    return overall_ok


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def _run_cli(argv: Optional[list[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description=(
            "Step 15 — verify 3-FINAL PACKAGING (and SME staging) deliverables: "
            "audio vs sheet/tracklist, plus covers where expected (NBC excluded)."
        )
    )
    p.add_argument("--year",  type=int)
    p.add_argument("--month", type=int)
    p.add_argument("--part",  type=int, choices=[1, 2])
    p.add_argument("--previous-month", action="store_true",
                   help="Full-month run for the previous month.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report findings but don't write the discrepancy CSV.")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("final_packaging_check")

    ctx = context_from_cli_args(args)
    logger.info(f"Release context: {ctx}")
    ok = verify_final_packaging_metadata(ctx, logger, dry_run=args.dry_run)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_run_cli())
