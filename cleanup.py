"""
cleanup.py — Steps 13 & 14: Non-MainTrack Removal and NBC File Rename
======================================================================
Refactors:
  6-Remove_Non-MainTracks.py  (Step 13 — implemented)
  7-RenameFilenames.py        (Step 14 — implemented)

Step 13 — Remove Non-MainTracks
-------------------------------
Compares the MP3 files under a target folder (default: the Tunesat
Music directory built in config.py) against a "keepers" set drawn from
a metadata CSV's "File Name" column.  Anything not in the keepers
set is a candidate for removal; empty folders are tidied afterwards.

Safety model:
  • `--dry-run` is the safety net.  Without it, a run DELETES the
    non-maintracks; with it, the function only reports what WOULD be
    deleted and writes nothing.
  • Both the orchestrator and the standalone CLI pass
    `actually_delete=not dry_run`, so behaviour is identical either way:
    a normal run deletes, `--dry-run` holds it back to a report.
    (`--delete-non-maintracks` is retained as a deprecated no-op for
    backward compatibility; deletion no longer requires it.)
  • Empty-directory removal happens ONLY after files were actually
    deleted — never during dry-run.
  • If the keepers CSV is empty or missing its "File Name" column,
    the run aborts immediately rather than deleting everything.
  • Per-file deletion failures are caught, logged, and counted; the
    run continues so one bad file can't strand the rest.

The function also supports overriding the CSV and target paths so
that the same logic can be exercised against arbitrary folders from
the standalone CLI without touching the orchestrator.
"""

from __future__ import annotations

import argparse
import csv as _csv
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

from config import ReleaseContext, context_from_cli_args


# ---------------------------------------------------------------------------
# Step 13 — Constants
# ---------------------------------------------------------------------------

# CSV column to read keeper filenames from.  The source script's contract
# is exactly "File Name" (with a space) — we follow it strictly so that
# a typo upstream surfaces immediately rather than silently producing an
# empty keepers set and deleting everything.
KEEP_FILENAME_COLUMN = "File Name"

# Safety floor for keeper-vs-disk matching.  A legitimate cleanup run is
# supposed to find almost every keeper on disk (everything in the CSV
# exists in the target folder; only variations / extras get deleted).  If
# fewer than this fraction of keepers match anything on disk it's almost
# certainly a CSV/folder mismatch (wrong CSV, different filename
# convention, etc.) and the run aborts rather than mass-deleting files
# that probably shouldn't be touched.
SAFETY_MIN_KEEPER_MATCH_RATIO = 0.50


# ---------------------------------------------------------------------------
# Step 13 — Helpers
# ---------------------------------------------------------------------------

def _basename_key(name: str) -> str:
    """
    Normalize a filename to a comparable form: strip any single trailing
    extension and lowercase the result.  Used to match keepers (CSV entries
    that may or may not include ``.mp3``) against on-disk filenames (which
    do include the extension, possibly with mixed case).
    """
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return stem.lower()


def _looks_like_audio_filename(value: str) -> bool:
    """
    Heuristic to distinguish real filename rows from Domo footer rows.

    Domo CSV exports often append a "Count: N" footer row at the bottom
    of the file.  When that summary lands in the "File Name" column, it
    looks like ``"Count 691"`` — non-empty, but obviously not a track.
    The loader needs to skip those rows or every dry-run reports a phantom
    "unrecoverable keeper" at the end.

    Real UPM track filenames look like
    ``1KM_012_1_Key_of_Paradise_JAEKY_K_2217389.mp3`` — they always
    contain digits (track number + workAudioId), have no internal
    whitespace, and use only [A-Za-z0-9_.-].  Plain English words like
    "TOTAL", "Sum", "Count 691" fail at least one of those tests.
    """
    if not value:
        return False
    # No internal whitespace.  This alone catches "Count 691", "Total: N",
    # and any similar prose footer.
    if any(c.isspace() for c in value):
        return False
    # Must contain at least one digit.  Every UPM track filename has at
    # least a workAudioId; summary words like "TOTAL" / "Sum" have none.
    if not any(c.isdigit() for c in value):
        return False
    # Only filename-shaped punctuation allowed.  Rejects rows like
    # ",,,,," or "Total:5855" that snuck past the previous two checks.
    stripped = value.replace("_", "").replace(".", "").replace("-", "")
    return bool(stripped) and stripped.isalnum()


def _load_keep_filenames(
    csv_path: Path,
    logger: logging.Logger,
) -> Optional[set[str]]:
    """
    Read the "File Name" column from `csv_path` and return the set of
    non-empty values that look like real audio filenames.  Returns None
    if the CSV can't be read or the expected column is missing — the
    caller should treat that as a hard failure (never delete on a
    malformed keepers list).

    Domo summary footers (``Count 691``, etc.) are silently dropped so
    they don't show up as phantom unrecoverable keepers later.
    """
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = _csv.DictReader(f)
            fields = reader.fieldnames or []
            if KEEP_FILENAME_COLUMN not in fields:
                logger.error(
                    f"  '{KEEP_FILENAME_COLUMN}' column not found in CSV.\n"
                    f"     CSV: {csv_path}\n"
                    f"     Found columns: {fields}"
                )
                return None
            keep: set[str] = set()
            dropped_rows: list[str] = []
            for row in reader:
                raw = (row.get(KEEP_FILENAME_COLUMN) or "").strip()
                if not raw:
                    continue
                if _looks_like_audio_filename(raw):
                    keep.add(raw)
                else:
                    dropped_rows.append(raw)
            if dropped_rows:
                # These are almost always Domo "Count: N" footers, but
                # log them anyway so a real format regression surfaces.
                logger.info(
                    f"  Skipped {len(dropped_rows)} non-filename row(s) "
                    f"in CSV (likely Domo summary footers): "
                    f"{dropped_rows[:3]}"
                    + ("…" if len(dropped_rows) > 3 else "")
                )
            return keep
    except Exception as exc:
        logger.error(f"  Failed to read metadata CSV {csv_path}: {exc}")
        return None


def _find_mp3_files(target: Path) -> list[Path]:
    """
    Return every .mp3 file under `target` (recursive), case-insensitive
    on the suffix so a stray .MP3 file is still considered.
    """
    return sorted(
        p for p in target.rglob("*")
        if p.is_file() and p.suffix.lower() == ".mp3"
    )


def _find_empty_dirs_after_deletion(
    root: Path,
    files_to_delete: set[Path],
) -> list[Path]:
    """
    Preview which directories under `root` WOULD become empty if the
    given files were deleted.  Used by the dry-run report — never mutates
    the filesystem.  Returns dirs deepest-first so the order matches
    what `_remove_empty_dirs` would do for real.
    """
    empty: list[Path] = []
    for d in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not d.is_dir():
            continue
        # Any surviving file (at any depth under d) blocks d from being empty.
        has_survivor = any(
            p.is_file() and p not in files_to_delete
            for p in d.rglob("*")
        )
        if not has_survivor:
            empty.append(d)
    return empty


def _remove_empty_dirs(
    root: Path,
    logger: logging.Logger,
) -> list[Path]:
    """
    Remove every empty directory under `root`, deepest-first.  Called
    only after real deletions.  Returns the directories that were
    successfully removed.
    """
    removed: list[Path] = []
    candidates = sorted(
        [p for p in root.rglob("*") if p.is_dir()],
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for d in candidates:
        try:
            if not any(d.iterdir()):
                d.rmdir()
                logger.info(f"  [REMOVED DIR] {d.relative_to(root)}")
                removed.append(d)
        except OSError:
            # Not empty (race) or no permission — leave it alone.
            pass
    return removed


# ---------------------------------------------------------------------------
# Step 13 — Source-side helpers (for "missing-keeper" auto-fill)
# ---------------------------------------------------------------------------

def _build_source_index(
    source_roots: list[Path], logger: logging.Logger
) -> dict[str, Path]:
    """
    Walk one or more ``source_roots`` recursively and build a
    basename → full-path index of every MP3 they contain.  The basename key
    uses the same normalization as the keeper-matching code (``_basename_key``:
    extension stripped, lowercased) so a missing keeper from the CSV can be
    looked up directly.

    The Tunesat folder holds US MP3 *plus* Ex-US eligible MP3, so a keeper that
    is missing from the target can legitimately live in EITHER the US delivery
    (``1-ORIGINAL/Music/MP3/MEDIA``) or the Ex-US delivery
    (``1-ORIGINAL/Music/Ex-US (MP3)/MEDIA``) — hence a list of roots.

    If a basename collides (across albums or across the two deliveries), the
    first walk-order match wins; we log a warning so the user can investigate
    if it ever matters in practice.  A root that doesn't exist is skipped with
    a warning rather than aborting.
    """
    index: dict[str, Path] = {}
    collisions = 0
    for source_root in source_roots:
        if not source_root.exists():
            logger.warning(f"  Auto-fill source not found (skipping): {source_root}")
            continue
        n_before = len(index)
        for p in source_root.rglob("*"):
            if p.is_file() and p.suffix.lower() == ".mp3":
                key = _basename_key(p.name)
                if key in index:
                    collisions += 1
                else:
                    index[key] = p
        logger.info(f"  Indexed {len(index) - n_before} MP3(s) under {source_root}")

    logger.info(
        f"  Source index: {len(index)} unique MP3(s) across "
        f"{len(source_roots)} source root(s)."
    )
    if collisions:
        logger.warning(
            f"  {collisions} duplicate basename(s) across sources — only the "
            f"first occurrence of each is indexed.  Run with --debug if this "
            f"matters."
        )
    return index


def _copy_missing_keepers(
    source_roots: list[Path],
    target_root:  Path,
    source_index: dict[str, Path],
    missing_keys: set[str],
    dry_run:      bool,
    logger:       logging.Logger,
) -> tuple[int, int, list[str]]:
    """
    For every keeper not currently in the target, look it up in
    ``source_index`` and copy it (preserving its relative path under whichever
    of ``source_roots`` it came from) into ``target_root``.

    Returns ``(copied, failed, unrecoverable_keys)``:
      copied              — files copied (or, in dry-run, files that WOULD
                            be copied).
      failed              — files we tried to copy but couldn't (permission,
                            disk full, etc.).
      unrecoverable_keys  — keepers that aren't in ``source_index`` at all;
                            these are genuine data-integrity gaps and the
                            caller will treat them as failures.
    """
    copied        = 0
    failed        = 0
    unrecoverable: list[str] = []

    for key in sorted(missing_keys):
        src = source_index.get(key)
        if src is None:
            unrecoverable.append(key)
            continue

        # Mirror the source's structure so target ends up looking like a
        # delivery: <root>/{Label}/{Album}/{file}.mp3 → target/{Label}/…
        # `src` may be under any of the source roots (US or Ex-US MP3), so
        # find the one it lives under to compute its relative path.
        rel = None
        for root in source_roots:
            try:
                rel = src.relative_to(root)
                break
            except ValueError:
                continue
        if rel is None:
            logger.error(
                f"  [COPY ERROR] {src} is not under any source root; skipping."
            )
            failed += 1
            continue

        dst = target_root / rel

        if dry_run:
            logger.info(f"    [WOULD COPY] {rel}")
            copied += 1
            continue

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            logger.info(f"  [COPIED]  {rel}")
            copied += 1
        except Exception as exc:
            logger.error(f"  [COPY FAILED] {rel}: {exc}")
            failed += 1

    return copied, failed, unrecoverable


# ---------------------------------------------------------------------------
# Step 13 — Public entry point
# ---------------------------------------------------------------------------

def remove_non_maintracks(
    ctx: ReleaseContext,
    dry_run: bool,
    actually_delete: bool,
    logger: logging.Logger,
    metadata_csv:   Optional[Path] = None,
    target_folder:  Optional[Path] = None,
    source_folder:  Optional[Path] = None,
) -> bool:
    """
    Bidirectional sync of ``target_folder`` against ``metadata_csv``'s
    "File Name" column.  The CSV is treated as the COMPLETE spec for what
    should be in the folder:

      * Files in target NOT listed in CSV  → deleted.
      * Files in CSV NOT present in target → copied in from ``source_folder``.
      * CSV entries with no source on disk → unrecoverable; the step fails.

    Defaults (when each Optional is None):
        metadata_csv  ← ctx.cleanup_metadata_csv
        target_folder ← ctx.cleanup_target_folder
        source_folder ← ctx.specials_dir / "1-ORIGINAL" / "Music" / "MP3" / "MEDIA"
                        (the UniSync delivery from Step 5).

    Behaviour matrix:
        dry_run=True     ⇒ report only — both copy and delete plans.
        dry_run=False, actually_delete=False ⇒ report + advisory note.
        dry_run=False, actually_delete=True  ⇒ COPY missing keepers FIRST,
                                               then delete extras, then
                                               tidy empty dirs.

    The copy-first ordering is deliberate: if anything goes wrong mid-run,
    we'd rather have an over-full folder (extras still present) than a
    truncated one (keepers missing because we deleted before copying).

    Returns True only on full sync success.  Returns False on any of:
    missing CSV/target/source, empty keepers list, safety abort
    (CSV/folder don't agree), unrecoverable missing keepers, copy failures,
    or per-file deletion failures.
    """
    csv_path = metadata_csv  or ctx.cleanup_metadata_csv
    target   = target_folder or ctx.cleanup_target_folder
    # Auto-fill source(s): the Tunesat Music folder holds the US MP3 delivery
    # PLUS the Tunesat-eligible Ex-US MP3 labels, so a missing keeper can live
    # in either delivery — search BOTH.  An explicit --source override collapses
    # to that single root (backward-compatible).
    _music = ctx.specials_dir / "1-ORIGINAL" / "Music"
    if source_folder is not None:
        source_roots = [source_folder]
    else:
        source_roots = [
            _music / "MP3"         / "MEDIA",   # US MP3 (Step 5 delivery)
            _music / "Ex-US (MP3)" / "MEDIA",   # Ex-US MP3 (BTV/Bruton/Kosinus…)
        ]

    logger.info(f"  Metadata CSV:  {csv_path}")
    logger.info(f"  Target folder: {target}")
    logger.info(f"  Auto-fill source(s):")
    for s in source_roots:
        logger.info(f"     {s}")

    if not csv_path.exists():
        msg = (
            f"  Metadata CSV not found: {csv_path}\n"
            f"     Ensure Step 1 (Domo exports) has refreshed the Tunesat "
            f"Metadata CSV for this release before running this step."
        )
        if dry_run:
            logger.warning("  ⚠ " + msg.strip())
            logger.info(
                "  [DRY RUN] Skipping non-maintrack cleanup preview "
                "(metadata CSV not present yet — produced by Step 1)."
            )
            return True
        logger.error("  ✗ " + msg)
        return False
    if not target.exists():
        if dry_run:
            logger.warning(f"  ⚠ Target folder not found: {target}")
            logger.info(
                "  [DRY RUN] Skipping non-maintrack cleanup preview "
                "(target tree not present yet — produced by earlier steps)."
            )
            return True
        logger.error(f"  ✗  Target folder not found: {target}")
        return False
    existing_sources = [s for s in source_roots if s.exists()]
    if not existing_sources:
        msg = (
            f"  No auto-fill source folder exists.  Searched:\n"
            + "".join(f"       {s}\n" for s in source_roots)
            + f"     This step needs a source to copy any missing keepers from.\n"
            f"     The defaults are the Step 5 UniSync deliveries; run Step 5\n"
            f"     first, or pass --source PATH to point at the right source."
        )
        if dry_run:
            logger.warning("  ⚠ " + msg.strip())
            logger.info(
                "  [DRY RUN] Skipping non-maintrack cleanup preview "
                "(auto-fill source not present yet — produced by Step 5)."
            )
            return True
        logger.error("  ✗ " + msg)
        return False

    keep = _load_keep_filenames(csv_path, logger)
    if keep is None:
        return False
    if not keep:
        logger.error(
            "  ✗  Keepers CSV produced ZERO filenames — aborting to avoid "
            "deleting every track.  Check that the CSV is populated and "
            f"the column header reads exactly {KEEP_FILENAME_COLUMN!r}."
        )
        return False
    logger.info(f"  Keeper filenames loaded: {len(keep)}")

    # ---- Inventory target ---------------------------------------------------
    #
    # Compare on the BASENAME (no extension, lowercase) on both sides.  The
    # Tunesat metadata CSV's "File Name" column may or may not include the
    # ".mp3" suffix depending on the upstream tool that produced it; we want
    # the same comparison either way.  This also makes ".MP3" vs ".mp3"
    # disk-side variations a non-issue.
    all_mp3s          = _find_mp3_files(target)
    keep_keys         = {_basename_key(n) for n in keep}
    to_keep_paths     = [p for p in all_mp3s if _basename_key(p.name) in keep_keys]
    to_delete         = [p for p in all_mp3s if _basename_key(p.name) not in keep_keys]
    present_in_target = {_basename_key(p.name) for p in to_keep_paths}
    missing_keys      = keep_keys - present_in_target

    logger.info(f"  Target MP3 files found:           {len(all_mp3s)}")
    logger.info(f"  Keepers present in target:        {len(present_in_target)}")
    logger.info(f"  Keepers MISSING from target:      {len(missing_keys)}")
    logger.info(f"  Target files NOT in CSV (extras): {len(to_delete)}")

    # ---- Inventory source for the missing keepers ---------------------------
    #
    # Only walk the source folder if we actually need it.  In the common
    # "target already in sync" case, missing_keys is empty and the source
    # index would just be wasted work — a 16K-file rglob over a Pegasus
    # volume is ~30s of pointless I/O per re-run.  When the index isn't
    # built, all the downstream variables default to empty sets so the
    # safety check and downstream branches still behave correctly:
    # recoverable_total == len(present_in_target) == len(keep_keys), so
    # the safety ratio is 1.0 and the trivial-success branch fires.
    if missing_keys:
        source_index          = _build_source_index(existing_sources, logger)
        recoverable_missing   = missing_keys & source_index.keys()
        unrecoverable_missing = missing_keys - source_index.keys()
        logger.info(f"  Missing — recoverable from source: {len(recoverable_missing)}")
        logger.info(f"  Missing — UNRECOVERABLE:           {len(unrecoverable_missing)}")
    else:
        # Skipped: target already has every keeper.
        source_index          = {}
        recoverable_missing   = set()
        unrecoverable_missing = set()
        logger.debug("  Skipped source-index build (no missing keepers).")

    # ---- Safety: catch CSV/folder mismatches BEFORE proposing destructive ops
    #
    # In a legitimate run, the CSV's keepers should be findable EITHER in the
    # target (already delivered) OR in the source (deliverable on demand).  A
    # low recoverable ratio means the CSV is talking about files that don't
    # exist anywhere in this release — almost certainly the wrong CSV.
    recoverable_total = len(present_in_target) + len(recoverable_missing)
    recoverable_ratio = (
        recoverable_total / len(keep_keys) if keep_keys else 0.0
    )

    if recoverable_ratio < SAFETY_MIN_KEEPER_MATCH_RATIO:
        sample_csv  = sorted(keep)[:5]
        sample_disk = [p.name for p in all_mp3s[:5]]
        sample_src  = [src.name for src in list(source_index.values())[:5]]
        logger.error(
            f"  ✗  SAFETY ABORT — only {recoverable_total} of {len(keep_keys)} "
            f"keepers are findable in target+source "
            f"({recoverable_ratio:.0%}, required ≥ "
            f"{SAFETY_MIN_KEEPER_MATCH_RATIO:.0%}).\n"
            f"     The CSV is asking for files that don't exist anywhere in\n"
            f"     this release.  Almost always means the wrong CSV — refresh\n"
            f"     it via Step 1's Tunesat Metadata Domo export and retry.\n"
            f"\n"
            f"     Sample CSV entries:\n"
            + "".join(f"       {n!r}\n" for n in sample_csv)
            + f"     Sample target filenames:\n"
            + "".join(f"       {n!r}\n" for n in sample_disk)
            + f"     Sample source filenames:\n"
            + "".join(f"       {n!r}\n" for n in sample_src)
        )
        return False

    # ---- Trivial-success case: target already in sync -----------------------
    if not to_delete and not missing_keys:
        logger.info(
            "  ✓  Target folder is already in sync with CSV — nothing to do."
        )
        return True

    # ---- Planned reconciliation (always shown, both directions) ------------
    logger.info("  Reconciliation plan:")

    # 1. Missing keepers we'd copy in
    if recoverable_missing:
        logger.info(
            f"    ── Would COPY {len(recoverable_missing)} missing keeper(s) "
            f"from source ──"
        )
        _copy_missing_keepers(
            existing_sources, target, source_index, recoverable_missing,
            dry_run=True, logger=logger,
        )

    # 2. Missing keepers we CAN'T copy (source has no match)
    if unrecoverable_missing:
        logger.error(
            f"    ── {len(unrecoverable_missing)} keeper(s) UNRECOVERABLE "
            f"(not in source) ──"
        )
        # Map keys back to original CSV names for human-readable output.
        key_to_name = {_basename_key(n): n for n in keep}
        for key in sorted(unrecoverable_missing):
            original = key_to_name.get(key, key)
            logger.error(f"      [MISSING — UNRECOVERABLE] {original}")

    # 3. Extras to remove.  Word the preview by intent: a real run says
    #    "Will DELETE" (each removal is then logged as [DELETED] below); a
    #    dry-run / report says "Would DELETE" and lists every file.
    will_delete = (not dry_run) and actually_delete
    empty_dirs_preview: list[Path] = []
    if to_delete:
        verb = "Will" if will_delete else "Would"
        logger.info(
            f"    ── {verb} DELETE {len(to_delete)} non-keeper file(s) ──"
        )
        empty_dirs_preview = _find_empty_dirs_after_deletion(
            target, set(to_delete)
        )
        if not will_delete:
            for p in to_delete:
                logger.info(f"      [WOULD DELETE] {p.relative_to(target)}")
            for d in empty_dirs_preview:
                logger.info(f"      [WOULD REMOVE DIR] {d.relative_to(target)}")

    # ---- Gate on dry_run / actually_delete ----------------------------------
    if dry_run:
        # Even in dry-run, unrecoverable missing is a contract violation we
        # surface as a failure — the caller has to fix the gap (Step 5 didn't
        # deliver them, or the CSV is wrong) before sync is possible.
        if unrecoverable_missing:
            logger.error(
                f"  ✗  Dry-run shows {len(unrecoverable_missing)} unrecoverable "
                f"keeper(s).  The target cannot be brought into sync with the\n"
                f"     CSV until those files exist somewhere reachable.  Either:\n"
                f"       • Re-run Step 5 (UniSync) to redeliver them, OR\n"
                f"       • Verify the CSV reflects this release accurately."
            )
            return False
        logger.info(
            f"  ✓  Dry-run complete — would copy "
            f"{len(recoverable_missing)} file(s), delete {len(to_delete)} "
            f"file(s), and remove {len(empty_dirs_preview)} empty dir(s).  "
            f"--dry-run is set; nothing was changed on disk."
        )
        return True

    if not actually_delete:
        # actually_delete=False with dry_run=False is an explicit "report only"
        # request from a caller; mirror the dry-run advisory and don't mutate.
        if unrecoverable_missing:
            logger.error(
                f"  ✗  {len(unrecoverable_missing)} unrecoverable keeper(s) — "
                f"see [MISSING — UNRECOVERABLE] above.\n"
                f"     Sync cannot succeed until those files are available.  "
                f"Fix the gap first."
            )
            return False
        logger.info(
            f"  ✓  Report only — would copy "
            f"{len(recoverable_missing)} file(s), delete {len(to_delete)} "
            f"file(s), and remove {len(empty_dirs_preview)} empty dir(s).\n"
            f"     Run without --dry-run to perform the actual sync."
        )
        return True

    # ---- Real run -----------------------------------------------------------
    #
    # ORDER MATTERS: copy missing keepers FIRST, then delete extras.  If
    # anything fails mid-run, an over-full folder (extras still present) is
    # always safer than a truncated one (keepers gone because we deleted
    # before copying).  Unrecoverable misses pre-empt the run entirely.
    if unrecoverable_missing:
        logger.error(
            f"  ✗  Refusing to proceed: {len(unrecoverable_missing)} "
            f"unrecoverable keeper(s) — no source for them anywhere.\n"
            f"     See the [MISSING — UNRECOVERABLE] entries above.  Sync\n"
            f"     would leave the folder permanently out of contract."
        )
        return False

    # 1. Copy missing keepers from source
    copied_count   = 0
    failed_copies  = 0
    if recoverable_missing:
        logger.info(
            f"  Copying {len(recoverable_missing)} missing keeper(s) "
            f"from source…"
        )
        copied_count, failed_copies, _ = _copy_missing_keepers(
            existing_sources, target, source_index, recoverable_missing,
            dry_run=False, logger=logger,
        )
        if failed_copies:
            logger.error(
                f"  ✗  {failed_copies} copy failure(s) — refusing to proceed "
                f"to deletion.  The folder is currently in a partially-synced "
                f"state; re-run after the underlying issue is fixed."
            )
            return False

    # 2. Delete extras
    deleted  = 0
    failures: list[tuple[Path, str]] = []
    if to_delete:
        logger.info(f"  Deleting {len(to_delete)} non-maintrack file(s)…")
        for p in to_delete:
            try:
                p.unlink()
                logger.info(f"  [DELETED] {p.relative_to(target)}")
                deleted += 1
            except Exception as exc:
                logger.error(f"  [FAILED]  {p.relative_to(target)}: {exc}")
                failures.append((p, str(exc)))

    # 3. Remove empty dirs (only after real deletion)
    removed_dirs = _remove_empty_dirs(target, logger) if to_delete else []
    remaining_mp3s = len(_find_mp3_files(target))

    logger.info(
        f"\n  ─── Step 13 summary ─────────────────────────────────\n"
        f"    Keepers copied from source:  {copied_count}\n"
        f"    Copy failures:               {failed_copies}\n"
        f"    Unrecoverable missing:       {len(unrecoverable_missing)}\n"
        f"    Deleted files:               {deleted}\n"
        f"    Failed deletions:            {len(failures)}\n"
        f"    Empty dirs removed:          {len(removed_dirs)}\n"
        f"    Remaining MP3 files:         {remaining_mp3s}"
    )
    if failures:
        logger.error(
            f"  ✗  {len(failures)} deletion failure(s) — see the [FAILED] "
            f"lines above.  Other operations completed; re-running will "
            f"retry the failed files."
        )
        return False

    logger.info(
        "  ✓  Non-maintrack cleanup complete — target folder is in sync "
        "with CSV."
    )
    return True


# ---------------------------------------------------------------------------
# Step 14 — Rename NBC Music Files  (refactored from 7-RenameFilenames.py)
# ---------------------------------------------------------------------------

# Characters allowed in final filenames (besides the extension dot)
_SAFE_CHARS_PATTERN = re.compile(r"[^A-Za-z0-9_ ]")


def clean_filename(name: str) -> str:
    """
    Remove characters outside [A-Za-z0-9_ ] from a filename's basename.
    Preserves the extension and whitespace.

    Examples:
        "3M_740_1_Climbing&Silence.wav" → "3M_740_1_ClimbingSilence.wav"
        "Track (1).mp3"                 → "Track 1.mp3"
    """
    base, ext = name.rsplit(".", 1) if "." in name else (name, "")
    clean_base = _SAFE_CHARS_PATTERN.sub("", base)
    return f"{clean_base}.{ext}" if ext else clean_base


def rename_nbc_music_files(
    ctx: ReleaseContext,
    dry_run: bool,
    logger: logging.Logger,
) -> bool:
    """
    Step 14 — Clean filenames under the NBC Music directory.

    Refactored from the legacy 7-RenameFilenames.py.  Walks the NBC Music
    tree and removes any character outside [A-Za-z0-9_ ] from each file's
    basename, preserving the extension and any whitespace.

    Behaviour (matches the legacy tool's configured options):
      - Recursive:              ON  (walk all subfolders)
      - Files only:             ON  (directories are never renamed)
      - Remove special chars:   ON  (strip outside [A-Za-z0-9_ ])
      - Remove whitespace:      OFF (spaces are kept)
      - Preserve extensions:    yes (only the basename is cleaned)

    SCOPE GUARD: this only ever operates under
        …/3-FINAL PACKAGING/Universal Production Music {mdf} Release - NBC/Music
    (ctx.partner_dirs["nbc_music_root"]).  The function refuses to run if
    that path can't be resolved, doesn't match the expected NBC Music
    structure, or doesn't exist — so a misconfigured ctx can't send it
    walking somewhere unintended.

    Honours dry_run (logs intended renames, changes nothing).  Returns True
    on success (including a clean no-op), False on a hard error.
    """
    nbc_root = ctx.partner_dirs.get("nbc_music_root")

    logger.info("─── Step 14 — Rename NBC Music filenames ──────────────────")
    logger.info(f"  Target root: {nbc_root}")

    # --- Scope / safety guards ---------------------------------------------
    if nbc_root is None:
        logger.error(
            "  ✗  nbc_music_root is not defined in ctx.partner_dirs; refusing "
            "to run."
        )
        return False

    # Defensive: confirm the resolved path really is the NBC Music folder we
    # expect, so a future config change can't silently repoint this at, say,
    # a whole volume.  We require the exact trailing structure.
    expected_tail = (
        Path("3-FINAL PACKAGING")
        / f"Universal Production Music {ctx.month_display_folder} Release - NBC"
        / "Music"
    )
    if not str(nbc_root).endswith(str(expected_tail)):
        logger.error(
            "  ✗  nbc_music_root does not match the expected NBC Music path "
            "structure:\n"
            f"     resolved: {nbc_root}\n"
            f"     expected to end with: {expected_tail}\n"
            "     Refusing to run to avoid renaming files outside the NBC "
            "Music tree."
        )
        return False

    if not nbc_root.exists():
        msg = (
            f"  NBC Music directory not found:\n     {nbc_root}\n"
            "     Run Step 12 (Soundminer mirror + WAV→MP3) first so the "
            "Music tree exists."
        )
        if dry_run:
            logger.warning("  ⚠ " + msg.strip())
            logger.info(
                "  [DRY RUN] Skipping rename preview "
                "(NBC Music tree not present yet — produced by Step 12)."
            )
            return True
        logger.error("  ✗ " + msg)
        return False

    if not nbc_root.is_dir():
        logger.error(f"  ✗  NBC Music path is not a directory:\n     {nbc_root}")
        return False

    # --- Walk + rename ------------------------------------------------------
    scanned    = 0
    renamed    = 0
    skipped    = 0          # already clean
    collisions = 0
    errors     = 0

    # rglob("*") yields files and directories; we act on FILES ONLY
    # (Rename directories: OFF).
    for path in sorted(nbc_root.rglob("*")):
        if not path.is_file():
            continue
        scanned += 1

        new_name = clean_filename(path.name)
        if new_name == path.name:
            skipped += 1
            continue

        target = path.parent / new_name

        # Guard against collisions — never clobber an existing file.
        if target.exists():
            collisions += 1
            logger.warning(
                f"  ⚠ Skipping (target exists): {path.name}\n"
                f"      would become: {new_name}\n"
                f"      in: {path.parent}"
            )
            continue

        if dry_run:
            logger.info(f"  [DRY RUN] {path.name}  →  {new_name}")
            renamed += 1
            continue

        try:
            path.rename(target)
            logger.info(f"  ✎ {path.name}  →  {new_name}")
            renamed += 1
        except OSError as exc:
            errors += 1
            logger.error(f"  ✗ Failed to rename {path.name}: {exc}")

    # --- Summary ------------------------------------------------------------
    logger.info("  ─── Step 14 summary ───")
    logger.info(f"    Files scanned:        {scanned}")
    logger.info(
        f"    {'Would rename' if dry_run else 'Renamed'}:         {renamed}"
    )
    logger.info(f"    Already clean:        {skipped}")
    if collisions:
        logger.info(f"    Skipped (collision):  {collisions}")
    if errors:
        logger.info(f"    Errors:               {errors}")

    if errors:
        logger.error(
            f"  ✗  Step 14 finished with {errors} rename error(s) — see above."
        )
        return False

    if dry_run:
        logger.info("  ✓  Step 14 dry-run complete (no files changed).")
    else:
        logger.info("  ✓  Step 14 complete — NBC Music filenames cleaned.")
    return True


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Step 13 — remove MP3 files from a target folder that are not "
            "listed in a 'keepers' metadata CSV (default: Tunesat Music + "
            "Tunesat Metadata CSV).  Deletes by default; pass --dry-run for a "
            "report that changes nothing."
        )
    )
    p.add_argument("--test",    action="store_true", required=True,
                   help="Required guard; this module only runs the cleanup "
                        "when invoked with --test.")
    p.add_argument("--year",    type=int)
    p.add_argument("--month",   type=int)
    p.add_argument("--part",    type=int, choices=[1, 2])
    p.add_argument(
        "--previous-month", action="store_true",
        help="Full-month run for the previous month "
             "(no Part split). Relative to today, or to "
             "--year/--month if given.")

    p.add_argument("--dry-run", action="store_true",
                   help="Report only — list what would be deleted/copied and "
                        "change nothing on disk.")
    p.add_argument("--delete-non-maintracks", action="store_true",
                   help="DEPRECATED / no-op.  Deletion now happens on any run "
                        "that isn't --dry-run; this flag is accepted but "
                        "ignored.")
    p.add_argument("--skip-non-maintrack-cleanup", action="store_true",
                   help="No-op for parity with the orchestrator's "
                        "--skip-non-maintrack-cleanup flag.")

    p.add_argument("--csv", default=None,
                   help="Override the keepers CSV path (default: "
                        "ctx.cleanup_metadata_csv).")
    p.add_argument("--target", default=None,
                   help="Override the target folder to clean (default: "
                        "ctx.cleanup_target_folder).")
    p.add_argument("--source", default=None,
                   help="Override the source folder used to auto-fill "
                        "missing keepers (default: "
                        "ctx.specials_dir/1-ORIGINAL/Music/MP3/MEDIA, "
                        "the Step 5 UniSync delivery).")

    p.add_argument("--rename", action="store_true",
                   help="Run Step 14 (clean NBC Music filenames) instead of "
                        "the Step 13 non-maintrack cleanup.  Honours --dry-run.")

    p.add_argument("--debug", action="store_true")
    return p


def _run_cli(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("cleanup")

    if args.skip_non_maintrack_cleanup:
        logger.info("--skip-non-maintrack-cleanup set; nothing to do.")
        return 0

    ctx = context_from_cli_args(args)
    logger.info(f"Release context: {ctx}")

    # Step 14 — NBC filename rename (standalone)
    if args.rename:
        ok = rename_nbc_music_files(ctx, dry_run=args.dry_run, logger=logger)
        return 0 if ok else 1

    csv_override    = Path(args.csv)    if args.csv    else None
    target_override = Path(args.target) if args.target else None
    source_override = Path(args.source) if args.source else None

    if args.delete_non_maintracks:
        logger.warning(
            "  !  --delete-non-maintracks is deprecated and ignored; deletion "
            "happens on any non-dry-run.  Use --dry-run for a report."
        )

    ok = remove_non_maintracks(
        ctx,
        dry_run=args.dry_run,
        actually_delete=not args.dry_run,
        logger=logger,
        metadata_csv=csv_override,
        target_folder=target_override,
        source_folder=source_override,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_run_cli())
