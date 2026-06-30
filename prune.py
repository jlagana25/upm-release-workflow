"""
prune.py — Reconcile the 1-ORIGINAL/Music trees against the current tracklists.

This is the inverse of verification.py.  Verification asks "is every file the
tracklist expects present?"; pruning asks "is every file present actually
expected?"  Anything in a Music tree that the current tracklist does NOT
reference is an EXTRA — left over from a previous month/run — and is a
candidate for removal, including whole stale album/label folders and the
duplicate-album folders that accumulate in copied trees like 'WAV w COVERS'.

Trees covered (all of {specials_dir}/1-ORIGINAL/Music):
    MP3/MEDIA           ← US tracklist      (.mp3)
    WAV/MEDIA           ← US tracklist      (.wav)
    WAV w COVERS/MEDIA  ← US tracklist      (.wav + AlbumCoverArt)
    Ex-US (MP3)/MEDIA   ← Ex-US tracklist   (.mp3)
    Ex-US (WAV)/MEDIA   ← Ex-US tracklist   (.wav)
    Japan/MEDIA         ← Japan metadata    (.wav)

Safety model (matches the rest of the pipeline):
  • Default mode is "report" — a dry-run that changes NOTHING and writes a
    full CSV of what it WOULD remove.
  • "archive" moves extras to a timestamped side folder
    ({specials_dir}/_PRUNED-<stamp>/...), preserving structure — recoverable.
  • "delete" hard-removes them.  Only this mode is destructive.
  • A tree whose tracklist CSV is missing/unreadable is SKIPPED entirely —
    we never prune without an authoritative keep-list.
  • Duplicate album folders are resolved without data loss: the folder with
    the most keepers (newest on a tie) is canonical; a file in another folder
    is removed only if the SAME name also exists in the canonical folder.
    A file unique to a non-canonical folder is KEPT and flagged for review.

Reuses verification's CSV/column helpers so the keep-list is derived exactly
the way verification derives its expected set.
"""

from __future__ import annotations

import csv as _csv
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import ReleaseContext, context_from_cli_args
from verification import _load_csv, _row_value

# Modes
PRUNE_REPORT  = "report"
PRUNE_ARCHIVE = "archive"
PRUNE_DELETE  = "delete"

# Filenames that are never "keepers" and are always safe to clear as junk.
_JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini", "icon\r"}

_SAMPLE_CAP = 12   # how many example paths to echo per category per tree


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _leaf(filename: str, ext: str) -> str:
    """Filename as it should appear on disk (append .ext unless already there)."""
    suffix = f".{ext.lower()}"
    return filename if filename.lower().endswith(suffix) else f"{filename}.{ext}"


def _albumno_of(folder_name: str) -> str:
    """AlbumNo prefix of an album folder named '{AlbumNo} - {Title}'."""
    if " - " in folder_name:
        return folder_name.split(" - ", 1)[0].strip()
    return folder_name.strip()


def _tree_specs(ctx: ReleaseContext) -> list[tuple[str, Path, Path, str, bool]]:
    """(label, media_root, csv_path, ext, has_covers) for every Music tree."""
    music = ctx.specials_dir / "1-ORIGINAL" / "Music"
    return [
        ("MP3",          music / "MP3" / "MEDIA",          ctx.us_tracklist_csv,   "mp3", False),
        ("WAV",          music / "WAV" / "MEDIA",          ctx.us_tracklist_csv,   "wav", False),
        ("WAV w COVERS", music / "WAV w COVERS" / "MEDIA", ctx.us_tracklist_csv,   "wav", True),
        ("Ex-US (MP3)",  music / "Ex-US (MP3)" / "MEDIA",  ctx.exus_tracklist_csv, "mp3", False),
        ("Ex-US (WAV)",  music / "Ex-US (WAV)" / "MEDIA",  ctx.exus_tracklist_csv, "wav", False),
        ("Japan",        music / "Japan" / "MEDIA",        ctx.japan_metadata_csv, "wav", False),
    ]


def _norm(s: str) -> str:
    """Loose key for comparing album titles to folder names."""
    return "".join(ch.lower() for ch in s if ch.isalnum())


def _build_keepsets(
    csv_path: Path, ext: str, want_covers: bool, label_text: str,
    logger: logging.Logger,
) -> Optional[tuple[set, set, dict]]:
    """
    Build the keep-lists from a tracklist:
        keep_audio = {(label, albumno, leaf_lower)}
        keep_cover = {(label, albumno, cover_lower)}   (only if want_covers)
        titles     = {(label, albumno): current_album_title}  (if a title col exists)
    Returns None if the CSV can't be read or lacks the required columns —
    the caller must then SKIP that tree (never prune without a keep-list).
    """
    df, cols = _load_csv(csv_path, label_text, logger)
    if df is None or not (cols["label"] and cols["albumno"] and cols["filename"]):
        return None

    keep_audio: set[tuple[str, str, str]] = set()
    keep_cover: set[tuple[str, str, str]] = set()
    titles: dict[tuple[str, str], str] = {}
    title_col = cols.get("albumtitle")
    for _, row in df.iterrows():
        label    = _row_value(row, cols, "label")
        albumno  = _row_value(row, cols, "albumno")
        filename = _row_value(row, cols, "filename")
        if not (label and albumno and filename):
            continue
        keep_audio.add((label, albumno, _leaf(filename, ext).lower()))
        if want_covers and cols["cover"]:
            cover = _row_value(row, cols, "cover")
            if cover:
                keep_cover.add((label, albumno, cover.lower()))
        if title_col and (label, albumno) not in titles:
            t = str(row.get(title_col, "")).strip()
            if t:
                titles[(label, albumno)] = t
    return keep_audio, keep_cover, titles


def _scan_tree(root: Path, keep_audio: set, keep_cover: set, want_covers: bool):
    """
    Walk a tree and classify every file.  Returns:
        extras  : list[(path, reason)]      — not referenced anywhere
        junk    : list[path]                — .DS_Store etc.
        by_album: {(label, albumno): {folder: {leaf_lower: path}}}  — keepers
    """
    extras: list[tuple[Path, str]] = []
    junk: list[Path] = []
    by_album: dict[tuple[str, str], dict[Path, dict[str, Path]]] = {}

    if not root.is_dir():
        return extras, junk, by_album

    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        nm = p.name
        if nm.lower() in _JUNK_NAMES or nm.startswith("._"):
            junk.append(p)
            continue

        rel = p.relative_to(root).parts
        if len(rel) != 3:
            extras.append((p, f"unexpected location ({len(rel)} level(s) under MEDIA)"))
            continue

        label, album_folder, leaf = rel
        albumno = _albumno_of(album_folder)
        leaf_l = leaf.lower()

        is_keeper = ((label, albumno, leaf_l) in keep_audio) or (
            want_covers and (label, albumno, leaf_l) in keep_cover
        )
        if is_keeper:
            by_album.setdefault((label, albumno), {}).setdefault(p.parent, {})[leaf_l] = p
        else:
            extras.append((p, "not referenced by current tracklist"))

    return extras, junk, by_album


def _resolve_duplicates(by_album: dict, titles: dict, logger: logging.Logger):
    """
    For album numbers whose keepers are spread across >1 folder, pick a
    canonical folder and return:
        dup_prunes : list[(path, reason)]  — duplicate keepers safe to remove
        flagged    : list[(path, note)]    — keepers UNIQUE to a non-canonical
                                             folder (kept; needs human review)

    Canonical preference: (1) folder name matches the current tracklist album
    title, (2) most keeper files, (3) newest mtime.  A non-canonical keeper is
    removed only if the SAME name also exists in canonical (no data loss).
    """
    dup_prunes: list[tuple[Path, str]] = []
    flagged: list[tuple[Path, str]] = []

    for (label, albumno), folders in by_album.items():
        if len(folders) <= 1:
            continue

        want_title = _norm(titles.get((label, albumno), ""))

        def _score(item):
            folder, leaves = item
            name = folder.name
            folder_title = name.split(" - ", 1)[1] if " - " in name else ""
            title_match = bool(want_title) and _norm(folder_title) == want_title
            try:
                mtime = folder.stat().st_mtime
            except OSError:
                mtime = 0.0
            return (title_match, len(leaves), mtime)

        canonical, canon_leaves = max(folders.items(), key=_score)
        canon_keys = set(canon_leaves.keys())

        for folder, leaves in folders.items():
            if folder == canonical:
                continue
            for leaf_l, path in leaves.items():
                if leaf_l in canon_keys:
                    dup_prunes.append(
                        (path, f"duplicate of canonical '{canonical.name}'")
                    )
                else:
                    flagged.append(
                        (path,
                         f"only copy is in non-canonical folder '{folder.name}' "
                         f"(canonical '{canonical.name}') — review")
                    )
    return dup_prunes, flagged


def _remove_empty_dirs(root: Path, logger: logging.Logger, do_delete: bool) -> int:
    """Remove now-empty directories under root (never root itself). Returns count."""
    if not root.is_dir():
        return 0
    removed = 0
    for d in sorted((p for p in root.rglob("*") if p.is_dir()),
                    key=lambda p: len(p.parts), reverse=True):
        try:
            if d != root and not any(d.iterdir()):
                if do_delete:
                    d.rmdir()
                removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def prune_music_trees(
    ctx: ReleaseContext,
    mode: str,
    logger: logging.Logger,
) -> tuple[int, int]:
    """
    Reconcile every 1-ORIGINAL/Music tree against its tracklist.

    mode: "report" (preview, no changes), "archive" (move extras to a
    timestamped side folder), or "delete" (hard remove).

    Returns (total_removable, total_kept).
    """
    if mode not in (PRUNE_REPORT, PRUNE_ARCHIVE, PRUNE_DELETE):
        logger.error(f"  Unknown prune mode {mode!r}; defaulting to report.")
        mode = PRUNE_REPORT

    do_change = mode in (PRUNE_ARCHIVE, PRUNE_DELETE)
    do_delete = mode == PRUNE_DELETE

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_root = ctx.specials_dir / f"_PRUNED-{stamp}"
    report_path = (
        ctx.missing_report_csv.parent
        / f"UPM {ctx.month_display} Prune_{datetime.now().strftime('%m-%d-%Y')}.csv"
    )

    logger.info("  Reconciling 1-ORIGINAL/Music against the current tracklists.")
    logger.info(
        f"  Mode: {mode.upper()}"
        + ("  (preview only — nothing will be changed)" if not do_change else "")
        + (f"\n  Archive folder: {archive_root}" if mode == PRUNE_ARCHIVE else "")
    )

    report_rows: list[dict[str, str]] = []
    total_removable = 0
    total_kept = 0

    for tree_label, root, csv_path, ext, want_covers in _tree_specs(ctx):
        logger.info(f"\n  ── {tree_label} ── {root}")
        if not root.is_dir():
            logger.info("     (folder does not exist — skipping)")
            continue

        keepsets = _build_keepsets(csv_path, ext, want_covers, tree_label, logger)
        if keepsets is None:
            logger.warning(
                "     ⚠  Tracklist unreadable/missing — SKIPPING this tree "
                "(refusing to prune without a keep-list)."
            )
            continue
        keep_audio, keep_cover, titles = keepsets

        extras, junk, by_album = _scan_tree(root, keep_audio, keep_cover, want_covers)
        dup_prunes, flagged = _resolve_duplicates(by_album, titles, logger)

        kept_count = sum(len(leaves) for f in by_album.values() for leaves in f.values())
        kept_count -= len(dup_prunes)   # duplicates are not "kept"
        total_kept += kept_count

        # Assemble the removable set: extras + junk + duplicate keepers.
        removable: list[tuple[Path, str]] = []
        removable += [(p, r) for p, r in extras]
        removable += [(p, "junk/system file") for p in junk]
        removable += [(p, r) for p, r in dup_prunes]
        total_removable += len(removable)

        logger.info(
            f"     keepers: {kept_count} | extras: {len(extras)} | "
            f"junk: {len(junk)} | duplicate-folder copies: {len(dup_prunes)} | "
            f"flagged (kept, review): {len(flagged)}"
        )

        def _echo(title, items):
            if not items:
                return
            logger.info(f"     {title} ({len(items)}):")
            for p, *rest in items[:_SAMPLE_CAP]:
                note = f"  [{rest[0]}]" if rest else ""
                logger.info(f"       {p.relative_to(root)}{note}")
            if len(items) > _SAMPLE_CAP:
                logger.info(f"       … and {len(items) - _SAMPLE_CAP} more")

        _echo("extras (not in tracklist)", extras)
        if junk:
            _echo("junk", [(p,) for p in junk])
        _echo("duplicate-folder copies", dup_prunes)
        _echo("FLAGGED — unique file in stale folder, KEPT for review", flagged)

        # Record every decision in the report.
        for p, reason in removable:
            report_rows.append({
                "Tree": tree_label, "Action": ("delete" if do_delete else
                                               "archive" if do_change else "would-remove"),
                "Reason": reason, "Path": str(p),
            })
        for p, note in flagged:
            report_rows.append({
                "Tree": tree_label, "Action": "keep-flagged",
                "Reason": note, "Path": str(p),
            })

        # Apply changes for this tree.
        if do_change and removable:
            moved = deleted = errs = 0
            for p, _reason in removable:
                try:
                    if do_delete:
                        os.remove(p)
                        deleted += 1
                    else:
                        rel = p.relative_to(root)
                        dest = archive_root / tree_label / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(p), str(dest))
                        moved += 1
                except OSError as exc:
                    errs += 1
                    logger.error(f"       ✗ {p}: {exc}")
            empties = _remove_empty_dirs(root, logger, do_delete=True)
            logger.info(
                f"     applied: "
                + (f"{deleted} deleted" if do_delete else f"{moved} archived")
                + f", {empties} empty folder(s) removed"
                + (f", {errs} error(s)" if errs else "")
            )
        elif not do_change:
            empties = _remove_empty_dirs(root, logger, do_delete=False)
            if empties:
                logger.info(f"     would remove {empties} empty folder(s) afterward")

    # Write the full report.
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w", encoding="utf-8-sig", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=["Tree", "Action", "Reason", "Path"])
            w.writeheader()
            w.writerows(report_rows)
        logger.info(f"\n  Prune report: {report_path}")
    except Exception as exc:
        logger.error(f"  Could not write prune report: {exc}")

    verb = "removed" if do_delete else "archived" if do_change else "removable"
    logger.info(
        f"  Summary: {total_removable} {verb}, {total_kept} kept"
        + (" (preview — re-run with --prune-mode archive to apply)"
           if not do_change else "")
    )
    return total_removable, total_kept


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------
def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Prune extra/stale files & folders from 1-ORIGINAL/Music "
                    "by reconciling each tree against its tracklist."
    )
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--part", type=int, default=1)
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument(
        "--prune-mode", choices=[PRUNE_REPORT, PRUNE_ARCHIVE, PRUNE_DELETE],
        default=PRUNE_REPORT,
        help="report (default, no changes) | archive (move to side folder) | "
             "delete (hard remove).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
    logger = logging.getLogger("prune")

    ctx = context_from_cli_args(args)
    prune_music_trees(ctx, args.prune_mode, logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
