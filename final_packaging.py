"""
final_packaging.py — Step 10: Copy Originals to Final Packaging
===============================================================
Refactors 5-Copy-Originals2Finals_fixed.py.

Fans out the audio + cover assets in
``{specials_dir}/1-ORIGINAL`` to every partner-delivery and HD-master
location, taking advantage of ``ctx.partner_dirs`` so the destination
paths stay in lock-step with config.py.

Source-tree layout (all under ``{specials_dir}/1-ORIGINAL``):

    Covers/                  ← flat covers folder (one .jpg per album)
    Music/MP3/MEDIA/         ← {Label}/{AlbumNo - title}/{Filename}.mp3
    Music/WAV/MEDIA/         ← {Label}/{AlbumNo - title}/{Filename}.wav
    Music/WAV w COVERS/MEDIA/← WAV files alongside their album cover
    Music/Ex-US (MP3)/MEDIA/ ← {Label}/{AlbumNo - title}/{Filename}.mp3
    Music/Ex-US (WAV)/MEDIA/ ← {Label}/{AlbumNo - title}/{Filename}.wav
    Music/Japan/MEDIA/       ← Japan NTT-DATA-formatted WAVs

Each source feeds one or more destinations. All destinations are pre-built in
``ctx.partner_dirs`` (see config.py). Tunesat is materialized directly from its
authoritative metadata keep-list instead of copying every MP3 and deleting the
non-main tracks later.

Behaviour:
  * Walks each source tree and copies file-by-file (NOT shutil.copytree).
    This gives us per-file idempotency — re-running the step skips
    files that already exist at the destination, matching the rest of
    the pipeline's ``--overwrite`` semantics.
  * ``--dry-run``      : log every planned copy, write nothing.
  * ``--overwrite``    : re-copy files that already exist at the dest.
  * Per-destination failure is logged and the run continues to the next
    destination — one bad partner folder shouldn't block the others.
  * Logs progress every ``PROGRESS_EVERY`` files inside each op, and a
    full summary at the end with per-op + total copy/skip/error counts.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import ReleaseContext, context_from_cli_args


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROGRESS_EVERY = 100  # log a progress line every N files (per op)


# ---------------------------------------------------------------------------
# Result / op types
# ---------------------------------------------------------------------------

@dataclass
class CopyResult:
    """Outcome of one source → destination copy operation."""
    name: str
    src:  Path
    dst:  Path
    copied:        int = 0
    skipped:       int = 0
    errors:        int = 0
    source_missing: bool = False
    label_filter:  Optional[frozenset[str]] = None  # informational

    @property
    def ok(self) -> bool:
        # Continue running other destinations after a missing source, but do
        # not report the final-packaging phase as successful/integral.
        return self.errors == 0 and not self.source_missing

    def summary_line(self) -> str:
        tag = " (filtered)" if self.label_filter else ""
        if self.source_missing:
            return f"{self.name}{tag}: source missing — skipped"
        return (
            f"{self.name}{tag}: "
            f"{self.copied:>5} copied, "
            f"{self.skipped:>5} skipped, "
            f"{self.errors:>3} errors"
        )


@dataclass
class CopyOp:
    """A single source → destination copy unit, optionally label-filtered."""
    name: str
    src:  Path
    dst:  Path
    label_filter: Optional[frozenset[str]] = None  # None = copy everything


# ---------------------------------------------------------------------------
# Core copy helper
# ---------------------------------------------------------------------------

def _copy_tree_files(
    src: Path,
    dst: Path,
    *,
    dry_run: bool,
    overwrite: bool,
    logger: logging.Logger,
    label_filter: Optional[frozenset[str]] = None,
) -> tuple[int, int, int]:
    """
    Recursively copy every file under ``src`` into the mirror location
    under ``dst``.  Returns ``(copied, skipped, errors)``.

    Per-file idempotency: a file is skipped when ``dst/<rel>`` already
    exists, unless ``overwrite`` is True.

    ``label_filter`` (used for the Ex-US MP3 → Tunesat copy) restricts
    the walk to immediate-children directories of ``src`` whose name is
    in the set.  Files sitting directly at ``src`` (no label parent)
    are skipped when a filter is active.
    """
    copied = skipped = errors = 0

    if not src.exists():
        return (0, 0, 0)

    src_str = str(src)
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src_str)
        rel_parts: tuple[str, ...] = () if rel == "." else Path(rel).parts

        # Label filter — only walk into allowed top-level label folders.
        # Match on the whitespace-stripped folder name: source deliveries
        # occasionally carry a stray leading/trailing space (e.g. "BTV " with
        # a sub-"pitch" folder), and an exact match would silently skip that
        # whole label — dropping contractually-eligible tracks from Tunesat.
        if label_filter is not None:
            if not rel_parts:
                # At src root: prune dirs to those that are allowed.
                dirs[:] = sorted(d for d in dirs if d.strip() in label_filter)
                # Files at the root have no label parent, so they can't
                # be Tunesat-eligible — skip them in filtered mode.
                continue
            if rel_parts[0].strip() not in label_filter:
                # Defensive: a pruned subtree shouldn't appear, but bail
                # if it somehow does.
                dirs[:] = []
                continue
        else:
            # Stable ordering for reproducible logs.
            dirs.sort()

        dst_dir = dst if not rel_parts else dst.joinpath(*rel_parts)

        for fn in sorted(files):
            # Ignore macOS detritus.
            if fn == ".DS_Store" or fn.startswith("._"):
                continue

            src_file = Path(root) / fn
            dst_file = dst_dir / fn

            try:
                if dst_file.exists() and not overwrite:
                    skipped += 1
                else:
                    if not dry_run:
                        dst_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_file, dst_file)
                    copied += 1
            except Exception as exc:
                errors += 1
                logger.error(
                    f"      ✗  copy {src_file.name}: "
                    f"{type(exc).__name__}: {exc}"
                )

            done = copied + skipped + errors
            if done % PROGRESS_EVERY == 0 and done > 0:
                logger.info(
                    f"      … {done:>5} files: "
                    f"{copied} copied, {skipped} skipped, {errors} errors"
                )

    return (copied, skipped, errors)


def _run_op(
    op: CopyOp,
    *,
    dry_run: bool,
    overwrite: bool,
    logger: logging.Logger,
) -> CopyResult:
    """Run one CopyOp and return its CopyResult."""
    logger.info(f"\n  • {op.name}")
    logger.info(f"      from: {op.src}")
    logger.info(f"      to:   {op.dst}")
    if op.label_filter is not None:
        logger.info(
            f"      filter: {len(op.label_filter)} label(s) "
            f"(Tunesat-eligible Ex-US labels only)"
        )

    if not op.src.exists():
        logger.warning(
            "      ⚠  Source folder does not exist — skipping this "
            "destination (a previous step may not have produced it yet)."
        )
        return CopyResult(
            name=op.name, src=op.src, dst=op.dst,
            source_missing=True, label_filter=op.label_filter,
        )

    if dry_run:
        logger.info("      [DRY RUN] no files will be written")

    copied, skipped, errors = _copy_tree_files(
        op.src, op.dst,
        dry_run=dry_run,
        overwrite=overwrite,
        logger=logger,
        label_filter=op.label_filter,
    )

    res = CopyResult(
        name=op.name, src=op.src, dst=op.dst,
        copied=copied, skipped=skipped, errors=errors,
        label_filter=op.label_filter,
    )
    logger.info(f"      result: {res.summary_line().split(': ', 1)[1]}")
    return res


def _run_grouped_ops(
    ops: list[CopyOp],
    *,
    dry_run: bool,
    overwrite: bool,
    logger: logging.Logger,
) -> list[CopyResult]:
    """Run copy operations with one source-tree traversal per filter group."""
    groups: dict[tuple[Path, Optional[frozenset[str]]], list[CopyOp]] = {}
    for op in ops:
        groups.setdefault((op.src, op.label_filter), []).append(op)

    results: dict[str, CopyResult] = {
        op.name: CopyResult(
            op.name, op.src, op.dst, label_filter=op.label_filter
        )
        for op in ops
    }
    for (src, label_filter), group in groups.items():
        logger.info(
            f"\n  • Source fan-out: {src} → {len(group)} destination(s)"
        )
        for op in group:
            logger.info(f"      {op.name}: {op.dst}")
        if not src.exists():
            for op in group:
                results[op.name].source_missing = True
            logger.warning("      ⚠ Source tree is missing; fan-out blocked.")
            continue

        src_str = str(src)
        seen = 0
        for root, dirs, files in os.walk(src):
            rel = os.path.relpath(root, src_str)
            rel_parts = () if rel == "." else Path(rel).parts
            if label_filter is not None:
                if not rel_parts:
                    dirs[:] = sorted(
                        d for d in dirs if d.strip() in label_filter
                    )
                    continue
                if rel_parts[0].strip() not in label_filter:
                    dirs[:] = []
                    continue
            else:
                dirs.sort()

            for filename in sorted(files):
                if filename == ".DS_Store" or filename.startswith("._"):
                    continue
                seen += 1
                source_file = Path(root) / filename
                for op in group:
                    result = results[op.name]
                    dest_dir = (
                        op.dst if not rel_parts
                        else op.dst.joinpath(*rel_parts)
                    )
                    dest_file = dest_dir / filename
                    try:
                        if dest_file.exists() and not overwrite:
                            result.skipped += 1
                        else:
                            if not dry_run:
                                dest_dir.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(source_file, dest_file)
                            result.copied += 1
                    except Exception as exc:
                        result.errors += 1
                        logger.error(
                            f"      ✗ {op.name}: copy {filename}: "
                            f"{type(exc).__name__}: {exc}"
                        )
                if seen % PROGRESS_EVERY == 0:
                    logger.info(
                        f"      … scanned {seen:>5} source files for "
                        f"{len(group)} destination(s)"
                    )
    return [results[op.name] for op in ops]


# ---------------------------------------------------------------------------
# Op-list builder
# ---------------------------------------------------------------------------

def _build_ops(ctx: ReleaseContext) -> list[CopyOp]:
    """Construct every source → destination CopyOp for this release."""
    originals = ctx.specials_dir / "1-ORIGINAL"
    music     = originals / "Music"
    covers    = originals / "Covers"
    pd        = ctx.partner_dirs

    mp3_src         = music / "MP3"           / "MEDIA"
    wav_src         = music / "WAV"           / "MEDIA"
    wavcov_src      = music / "WAV w COVERS"  / "MEDIA"
    exus_wav_src    = music / "Ex-US (WAV)"   / "MEDIA"
    japan_src       = music / "Japan"         / "MEDIA"

    return [
        # ---- MP3 (Tunesat is materialized separately from its keep-list) ----
        CopyOp("MP3 → Discovery",         mp3_src,    pd["discovery_mp3"]),
        CopyOp("MP3 → HD UDrive master",  mp3_src,    pd["hd_mp3_media"]),

        # ---- WAV (4 destinations) ----
        CopyOp("WAV → ESPN",              wav_src,    pd["espn_wav"]),
        CopyOp("WAV → SynchTank",         wav_src,    pd["synchtank_wav"]),
        CopyOp("WAV → Discovery",         wav_src,    pd["discovery_wav"]),
        CopyOp("WAV → HD UDrive master",  wav_src,    pd["hd_wav_media"]),

        # ---- Covers (1 destination — paired with WAV/SynchTank) ----
        CopyOp("Covers → SynchTank",      covers,     pd["synchtank_covers"]),

        # ---- WAV → NBC staging (plain WAV folder copy, no covers) ----
        CopyOp("WAV → NBC staging",          wav_src,    pd["nbc_staging_media"]),

        # ---- WAV w COVERS → Netmix (covers ride along) ----
        CopyOp("WAV w COVERS → Netmix",      wavcov_src, pd["netmix_music"]),

        # ---- Ex-US WAV (Tunesat MP3 is built from its keep-list above) ----
        CopyOp("Ex-US WAV → ExUS staging", exus_wav_src, pd["exus_staging_media"]),
        # ---- Japan (1 destination) ----
        CopyOp("Japan → UPM Japan NTT DATA", japan_src, pd["japan_final_media"]),
    ]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def copy_originals_to_finals(
    ctx: ReleaseContext,
    dry_run: bool,
    logger: logging.Logger,
    overwrite: bool = False,
) -> bool:
    """
    Execute every Step 10 copy operation.

    Returns True only if every source exists and every copy completed without
    I/O errors. All operations are still attempted so a partial problem does
    not prevent independent destinations from making progress.
    """
    ops = _build_ops(ctx)

    logger.info(f"  Final packaging: {len(ops)} copy operations queued")
    logger.info(f"    Specials root: {ctx.specials_dir}")
    logger.info(f"    HD final root: {ctx.hd_final_dir}")
    logger.info(f"    Month folder:  {ctx.month_display_folder}")
    if overwrite:
        logger.info("    --overwrite is ON — existing destination files will be re-copied")
    if dry_run:
        logger.info("    --dry-run is ON — no files will be written")

    results = _run_grouped_ops(
        ops, dry_run=dry_run, overwrite=overwrite, logger=logger
    )

    # Tunesat is defined by its metadata keep-list. Build the destination from
    # the US + eligible Ex-US source union directly instead of copying every
    # MP3 and deleting non-main tracks in a later workflow phase.
    logger.info("\n  Tunesat filtered materialization:")
    from cleanup import remove_non_maintracks
    tunesat_target = ctx.partner_dirs["tunesat_mp3"]
    if not dry_run:
        tunesat_target.mkdir(parents=True, exist_ok=True)
    tunesat_ok = remove_non_maintracks(
        ctx,
        dry_run=dry_run,
        actually_delete=not dry_run,
        logger=logger,
        target_folder=tunesat_target,
    )

    # ---- Ex-US staging covers -----------------------------------------------
    # The "Ex-US WAV → ExUS staging" copy carries no cover art (unlike the
    # WAV w COVERS sources), so drop each album's cover from the flat
    # 1-ORIGINAL/Covers folder into its matching album folder — mirroring the
    # NBC staging layout.  Only runs when the staging audio actually landed, so
    # we never create cover-only folders.
    covers_ok = True
    exus_res = next(
        (r for r in results if r.name == "Ex-US WAV → ExUS staging"), None
    )
    if exus_res is not None and not exus_res.source_missing:
        from covers import distribute_covers_into_album_folders
        from config import MASTERS_COVERS_DIR
        logger.info("\n  Ex-US staging covers:")
        covers_ok = distribute_covers_into_album_folders(
            ctx,
            ctx.exus_tracklist_csv,
            ctx.partner_dirs["exus_staging_media"],
            dry_run,
            logger,
            what="SME WAV ExUS",
            src_dir=MASTERS_COVERS_DIR,   # Ex-US art lives here (Step 6),
            src_by_label=True,            # never in the US-only flat Covers folder
        )
    elif exus_res is not None:
        logger.info(
            "\n  ↩  Skipping Ex-US staging covers — Ex-US WAV source missing."
        )

    # Aggregate summary
    total_copied  = sum(r.copied  for r in results)
    total_skipped = sum(r.skipped for r in results)
    total_errors  = sum(r.errors  for r in results)
    n_missing     = sum(1 for r in results if r.source_missing)
    overall_ok = tunesat_ok and all(
        r.ok or (dry_run and r.source_missing) for r in results
    ) and covers_ok

    logger.info("\n  ─── Step 10 summary ─────────────────────────────────")
    for r in results:
        prefix = "  ⚠ " if r.source_missing else ("  ✗ " if not r.ok else "  ✓ ")
        logger.info(f"{prefix}{r.summary_line()}")
    logger.info("  ─────────────────────────────────────────────────────")
    logger.info(
        f"  Tunesat filtered sync: {'OK' if tunesat_ok else 'FAILED'}"
    )
    logger.info(
        f"  Totals: {total_copied} copied, {total_skipped} skipped, "
        f"{total_errors} errors, {n_missing} source(s) missing"
    )
    if overall_ok:
        logger.info("  ✓  Final packaging complete.")
    else:
        logger.error(
            f"  ✗  Final packaging finished with {total_errors} file error(s) "
            f"across {sum(1 for r in results if not r.ok)} destination(s).  "
            f"See the lines marked ✗ above; the rest of the destinations "
            f"copied successfully and can be re-run safely (defaults skip "
            f"existing files)."
        )
    return overall_ok


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Step 10 — copy originals into all partner / HD destinations."
    )
    p.add_argument("--test",     action="store_true", required=True,
                   help="Required guard; this module runs the copy fan-out only "
                        "when invoked with --test.")
    p.add_argument("--year",     type=int)
    p.add_argument("--month",    type=int)
    p.add_argument("--part",     type=int, choices=[1, 2])
    p.add_argument(
        "--previous-month", action="store_true",
        help="Full-month run for the previous month "
             "(no Part split). Relative to today, or to "
             "--year/--month if given.")
    p.add_argument("--dry-run",  action="store_true",
                   help="Walk the trees and log every planned copy without "
                        "writing any files.")
    p.add_argument("--overwrite", action="store_true",
                   help="Re-copy files that already exist at the destination "
                        "(default behaviour is skip-if-present, matching the "
                        "rest of the pipeline).")
    p.add_argument("--skip-final-packaging", action="store_true",
                   help="Convenience no-op for parity with the orchestrator's "
                        "--skip-final-packaging flag.")
    p.add_argument("--only", default=None,
                   help="Restrict the run to a single destination by name "
                        "substring (case-insensitive). --only Tunesat runs the "
                        "filtered keep-list materialization; other values match "
                        "copy-op names such as Japan.")
    p.add_argument("--debug",    action="store_true")
    return p


def _run_cli(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("final_packaging")

    if args.skip_final_packaging:
        logger.info("--skip-final-packaging set; nothing to do.")
        return 0

    ctx = context_from_cli_args(args)
    logger.info(f"Release context: {ctx}")

    if args.only:
        needle = args.only.lower()
        if "tunesat" in needle:
            from cleanup import remove_non_maintracks
            target = ctx.partner_dirs["tunesat_mp3"]
            if not args.dry_run:
                target.mkdir(parents=True, exist_ok=True)
            ok = remove_non_maintracks(
                ctx,
                dry_run=args.dry_run,
                actually_delete=not args.dry_run,
                logger=logger,
                target_folder=target,
            )
            return 0 if ok else 1
        ops = _build_ops(ctx)
        matches = [op for op in ops if needle in op.name.lower()]
        if not matches:
            logger.error(
                f"No copy op matched --only {args.only!r}.  Available ops:"
            )
            for op in ops:
                logger.error(f"  - {op.name}")
            return 1
        logger.info(f"Running {len(matches)} op(s) matching --only {args.only!r}:")
        for op in matches:
            logger.info(f"  - {op.name}")
        results = [
            _run_op(op, dry_run=args.dry_run, overwrite=args.overwrite, logger=logger)
            for op in matches
        ]
        overall_ok = all(
            r.ok or (args.dry_run and r.source_missing) for r in results
        )
        for r in results:
            prefix = "  ⚠ " if r.source_missing else ("  ✗ " if not r.ok else "  ✓ ")
            logger.info(f"{prefix}{r.summary_line()}")
        return 0 if overall_ok else 1

    ok = copy_originals_to_finals(
        ctx, args.dry_run, logger, overwrite=args.overwrite
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_run_cli())
