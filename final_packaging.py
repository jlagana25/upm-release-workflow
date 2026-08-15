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

Each source feeds 1-4 destinations.  All destinations are pre-built in
``ctx.partner_dirs`` (see config.py).  The one exception is the Ex-US MP3
→ Tunesat copy, which is filtered to the labels in
``TUNESAT_EXUS_LABELS`` — only those Bruton/BTV/Kosinus libraries are
contractually eligible for the Tunesat feed.

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
import csv
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import ReleaseContext, context_from_cli_args


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Ex-US MP3 → Tunesat is restricted to these label sub-folders.  The label
# name is the IMMEDIATE child of the Ex-US (MP3)/MEDIA folder; anything
# outside this set is skipped silently for the Tunesat destination.
TUNESAT_EXUS_LABELS: frozenset[str] = frozenset({
    "Bruton",
    "Bruton Classical Series",
    "Bruton Vaults",
    "Bruton Vaults Anthologies",
    "BTV",
    "Kosinus",
    "Kosinus Archives",
    "Kosinus Arts",
    "Kosinus Classical",
    "Kosinus Magazine",
    "Kosinus Trailers",
    "Kosinus World",
})

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
        # source_missing is treated as a soft skip, NOT a failure — if a
        # previous step didn't produce that source folder we want the
        # step to continue with the other destinations.
        return self.errors == 0

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
    """A single source → destination copy unit with optional filters."""
    name: str
    src:  Path
    dst:  Path
    label_filter: Optional[frozenset[str]] = None  # None = copy everything
    partner_key: str = ""
    filename_filter: Optional[frozenset[str]] = None  # normalized stems


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
    filename_filter: Optional[frozenset[str]] = None,
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
            if (
                filename_filter is not None
                and fn.rsplit(".", 1)[0].casefold() not in filename_filter
            ):
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
        filename_filter=op.filename_filter,
    )

    res = CopyResult(
        name=op.name, src=op.src, dst=op.dst,
        copied=copied, skipped=skipped, errors=errors,
        label_filter=op.label_filter,
    )
    logger.info(f"      result: {res.summary_line().split(': ', 1)[1]}")
    return res


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
    exus_mp3_src    = music / "Ex-US (MP3)"   / "MEDIA"
    exus_wav_src    = music / "Ex-US (WAV)"   / "MEDIA"
    japan_src       = music / "Japan"         / "MEDIA"

    return [
        # ---- MP3 (3 destinations) ----
        CopyOp("MP3 → Tunesat",           mp3_src,    pd["tunesat_mp3"], partner_key="tunesat"),
        CopyOp("MP3 → Discovery",         mp3_src,    pd["discovery_mp3"], partner_key="discovery"),
        CopyOp("MP3 → HD UDrive master",  mp3_src,    pd["hd_mp3_media"], partner_key="hd_updates"),

        # ---- WAV (4 destinations) ----
        CopyOp("WAV → ESPN",              wav_src,    pd["espn_wav"], partner_key="espn"),
        CopyOp("WAV → SynchTank",         wav_src,    pd["synchtank_wav"], partner_key="synchtank"),
        CopyOp("WAV → Discovery",         wav_src,    pd["discovery_wav"], partner_key="discovery"),
        CopyOp("WAV → HD UDrive master",  wav_src,    pd["hd_wav_media"], partner_key="hd_updates"),

        # ---- Covers (1 destination — paired with WAV/SynchTank) ----
        CopyOp("Covers → SynchTank",      covers,     pd["synchtank_covers"], partner_key="synchtank"),

        # ---- WAV → NBC staging (plain WAV folder copy, no covers) ----
        CopyOp("WAV → NBC staging",          wav_src,    pd["nbc_staging_media"], partner_key="nbc"),

        # ---- WAV w COVERS → Netmix (covers ride along) ----
        CopyOp("WAV w COVERS → Netmix",      wavcov_src, pd["netmix_music"], partner_key="netmix"),

        # ---- Ex-US (2 destinations; Tunesat is label-filtered) ----
        CopyOp("Ex-US WAV → ExUS staging", exus_wav_src, pd["exus_staging_media"], partner_key="exus_staging"),
        CopyOp(
            "Ex-US MP3 → Tunesat",
            exus_mp3_src,
            pd["tunesat_mp3"],
            label_filter=TUNESAT_EXUS_LABELS,
            partner_key="tunesat",
        ),

        # ---- Japan (1 destination) ----
        CopyOp("Japan → UPM Japan NTT DATA", japan_src, pd["japan_final_media"], partner_key="japan_ntt"),
    ]


def _expected_destination_files(ops: list[CopyOp]) -> Optional[set[Path]]:
    """Union of relative files expected from every op sharing a destination."""
    expected: set[Path] = set()
    for op in ops:
        if not op.src.is_dir():
            return None
        for root, dirs, files in os.walk(op.src):
            relative_root = Path(root).relative_to(op.src)
            if op.label_filter is not None:
                if relative_root == Path("."):
                    dirs[:] = [d for d in dirs if d.strip() in op.label_filter]
                    continue
                if relative_root.parts[0].strip() not in op.label_filter:
                    dirs[:] = []
                    continue
            for filename in files:
                if filename == ".DS_Store" or filename.startswith("._"):
                    continue
                if (
                    op.filename_filter is not None
                    and filename.rsplit(".", 1)[0].casefold()
                    not in op.filename_filter
                ):
                    continue
                expected.add(relative_root / filename)
    return expected


def _remove_destination_extras(
    ops: list[CopyOp],
    *,
    dry_run: bool,
    logger: logging.Logger,
) -> tuple[int, int]:
    """Remove stale files from pending destinations after additive copying."""
    removed = errors = 0
    by_destination: dict[Path, list[CopyOp]] = {}
    for op in ops:
        by_destination.setdefault(op.dst, []).append(op)

    for destination, destination_ops in by_destination.items():
        if not destination.is_dir():
            continue
        expected = _expected_destination_files(destination_ops)
        if expected is None:
            logger.warning(
                f"  ⚠ Refusing to remove extras from {destination}: a source tree is missing."
            )
            continue
        extras = [
            path for path in destination.rglob("*")
            if path.is_file()
            and path.name != ".DS_Store"
            and not path.name.startswith("._")
            and path.relative_to(destination) not in expected
        ]
        for path in extras:
            try:
                if not dry_run:
                    path.unlink()
                removed += 1
            except OSError as exc:
                errors += 1
                logger.error(f"  ✗ Could not remove stale delivery file {path}: {exc}")
        if not dry_run:
            for directory in sorted(
                (path for path in destination.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                try:
                    if not any(directory.iterdir()):
                        directory.rmdir()
                except OSError:
                    pass
        if extras:
            verb = "Would remove" if dry_run else "Removed"
            logger.info(f"  {verb} {len(extras)} stale file(s) from {destination}")
    return removed, errors


def _files_for_op(op: CopyOp) -> dict[Path, Path]:
    """Return the exact relative-file manifest for one copy operation."""
    if not op.src.is_dir():
        return {}
    manifest: dict[Path, Path] = {}
    for root, dirs, files in os.walk(op.src):
        relative_root = Path(root).relative_to(op.src)
        if op.label_filter is not None:
            if relative_root == Path("."):
                dirs[:] = [d for d in dirs if d.strip() in op.label_filter]
                continue
            if relative_root.parts[0].strip() not in op.label_filter:
                dirs[:] = []
                continue
        for filename in files:
            if filename == ".DS_Store" or filename.startswith("._"):
                continue
            if (
                op.filename_filter is not None
                and filename.rsplit(".", 1)[0].casefold()
                not in op.filename_filter
            ):
                continue
            relative = relative_root / filename
            manifest[relative] = Path(root) / filename
    return manifest


def _archive_correction_dir(path: Path, logger: logging.Logger) -> None:
    if not path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = path.with_name(f"{path.name}-archived-{stamp}")
    counter = 2
    while archive.exists():
        archive = path.with_name(f"{path.name}-archived-{stamp}-{counter}")
        counter += 1
    path.replace(archive)
    logger.info(f"  Archived prior correction package → {archive.name}")


def _reconcile_uploaded_copy_op(
    op: CopyOp,
    *,
    dry_run: bool,
    logger: logging.Logger,
) -> bool:
    """Build an incremental Missing package for an already-uploaded tree.

    Netmix is a direct mirror of ``WAV w COVERS``, so relative paths provide a
    deterministic delta. New files go into a sibling ``Missing`` tree;
    removals leave the local original tree and are listed for manual removal
    from the partner system.
    """
    expected = _files_for_op(op)
    if not expected and not op.src.is_dir():
        logger.error(f"  ✗ Uploaded {op.partner_key} source is missing: {op.src}")
        return False
    actual = {
        path.relative_to(op.dst): path
        for path in op.dst.rglob("*")
        if path.is_file() and path.name != ".DS_Store" and not path.name.startswith("._")
    } if op.dst.is_dir() else {}
    additions = sorted(set(expected) - set(actual), key=lambda p: str(p).casefold())
    removals = sorted(set(actual) - set(expected), key=lambda p: str(p).casefold())
    missing_dir = op.dst.parent / "Missing"
    logger.info(
        f"  {op.partner_key} uploaded refresh: {len(additions)} addition(s), "
        f"{len(removals)} removal(s)."
    )
    if dry_run:
        logger.info(f"  [DRY RUN] Would rebuild correction package: {missing_dir}")
        return True
    if not additions and not removals:
        logger.info(f"  ✓ Uploaded {op.partner_key} media already matches refreshed source.")
        return True

    try:
        _archive_correction_dir(missing_dir, logger)
        missing_dir.mkdir(parents=True, exist_ok=False)
        rows: list[dict[str, str]] = []
        for relative in additions:
            destination = missing_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(expected[relative], destination)
            rows.append({
                "Action": "ADDITION_UPLOAD",
                "Relative Path": str(relative),
                "Local Result": f"Prepared in {missing_dir.name}",
                "Required Manual Action": "Upload this file to Netmix",
            })
        # Copy completion is the safety boundary: do not remove an obsolete
        # local item until every replacement/addition is ready.
        for relative in removals:
            actual[relative].unlink()
            rows.append({
                "Action": "REMOVE_FROM_NETMIX",
                "Relative Path": str(relative),
                "Local Result": "Removed from original Music folder",
                "Required Manual Action": "Remove this file from Netmix",
            })
        report = missing_dir / "Netmix Missing Audit.csv"
        with report.open("w", encoding="utf-8-sig", newline="") as handle:
            fields = [
                "Action", "Relative Path", "Local Result", "Required Manual Action"
            ]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        logger.info(f"  ✓ Netmix correction package ready: {missing_dir}")
        return True
    except OSError as exc:
        logger.error(f"  ✗ Could not build Netmix correction package: {exc}")
        return False


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

    Returns True if every op completed without I/O errors (missing source
    folders are treated as a soft skip, not a failure).  Returns False if
    any op produced an error — the orchestrator can then decide whether
    to stop or continue.
    """
    ops = _build_ops(ctx)
    from delivery_state import (
        partner_is_delivered,
        partner_needs_correction_package,
    )

    correction_ops = [
        op for op in ops
        if op.partner_key == "netmix"
        and partner_needs_correction_package(ctx.specials_dir, op.partner_key)
    ]
    if correction_ops:
        ops = [op for op in ops if op not in correction_ops]

    delivered_keys = {
        op.partner_key for op in ops
        if (
            op.partner_key
            and partner_is_delivered(ctx.specials_dir, op.partner_key)
            and not partner_needs_correction_package(
                ctx.specials_dir, op.partner_key
            )
        )
    }
    if delivered_keys:
        logger.warning(
            "  Delivered partner destinations will not be changed by this "
            f"refresh: {', '.join(sorted(delivered_keys))}"
        )
        ops = [op for op in ops if op.partner_key not in delivered_keys]

    # Tunesat's metadata is the exact contract for its smaller subset. Filter
    # before copying so a refresh does not temporarily copy thousands of
    # non-Tunesat tracks only to delete them again in Step 13.
    tunesat_ops = [op for op in ops if op.partner_key == "tunesat"]
    if tunesat_ops:
        from cleanup import _basename_key, _load_keep_filenames

        tunesat_filenames = _load_keep_filenames(ctx.cleanup_metadata_csv, logger)
        if not tunesat_filenames:
            logger.error(
                "  ✗ Tunesat metadata did not yield a safe filename keep-list; "
                "refusing to package or exact-sync its Music folder."
            )
            return False
        normalized = frozenset(_basename_key(name) for name in tunesat_filenames)
        for op in tunesat_ops:
            op.filename_filter = normalized

    logger.info(f"  Final packaging: {len(ops)} copy operations queued")
    logger.info(f"    Specials root: {ctx.specials_dir}")
    logger.info(f"    HD final root: {ctx.hd_final_dir}")
    logger.info(f"    Month folder:  {ctx.month_display_folder}")
    if overwrite:
        logger.info("    --overwrite is ON — existing destination files will be re-copied")
    if dry_run:
        logger.info("    --dry-run is ON — no files will be written")

    results: list[CopyResult] = []
    for op in ops:
        res = _run_op(op, dry_run=dry_run, overwrite=overwrite, logger=logger)
        results.append(res)

    corrections_ok = all(
        _reconcile_uploaded_copy_op(op, dry_run=dry_run, logger=logger)
        for op in correction_ops
    )

    removed_extras, sync_errors = _remove_destination_extras(
        ops, dry_run=dry_run, logger=logger
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
    overall_ok    = (
        all(r.ok for r in results)
        and covers_ok
        and corrections_ok
        and sync_errors == 0
    )

    logger.info("\n  ─── Step 10 summary ─────────────────────────────────")
    for r in results:
        prefix = "  ⚠ " if r.source_missing else ("  ✗ " if not r.ok else "  ✓ ")
        logger.info(f"{prefix}{r.summary_line()}")
    logger.info("  ─────────────────────────────────────────────────────")
    logger.info(
        f"  Totals: {total_copied} copied, {total_skipped} skipped, "
        f"{total_errors} errors, {n_missing} source(s) missing"
        f", {removed_extras} stale destination file(s) removed"
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
                   help="Restrict the run to a single op by name substring "
                        "(case-insensitive), e.g. --only 'Tunesat' or "
                        "--only 'Japan'.  Useful for re-running just one "
                        "destination after a partial failure.")
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
        overall_ok = all(r.ok for r in results)
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
