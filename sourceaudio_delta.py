"""Reconcile refreshed SourceAudio metadata against an existing AIFF delivery.

It rebuilds a sibling ``Missing`` folder containing only files that must be
uploaded after a later Domo metadata refresh and writes an audit CSV covering
additions, filename changes, and metadata removals.  Once every required upload
file is ready, obsolete local AIFFs are removed from ``Music``; deletion from
the SourceAudio service remains a manual operation.

Track identity is the Domo External Id.  A trailing numeric filename token is
used only as a fallback for existing audio, whose metadata is not read here.
That distinction lets a filename revision remain the same track instead of
being misreported as an unrelated removal and addition.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

from config import ReleaseContext, context_from_cli_args
from tracklist_columns import (
    POSSIBLE_COVER_COLS,
    POSSIBLE_EXTERNAL_ID_COLS,
    POSSIBLE_FILENAME_COLS,
    POSSIBLE_LABEL_COLS,
    POSSIBLE_URL_COLS,
    _find_column,
)


_TRACK_ID_RE = re.compile(r"_(\d+)$")
_AUDIO_SUFFIXES = {".aif", ".aiff"}
_SOURCE_SUFFIXES = {".wav", ".wave", ".aif", ".aiff"}
_REPORT_NAME = "SourceAudio Missing Audit.csv"
_REPORT_FIELDS = [
    "Action",
    "External Id",
    "Existing Filename",
    "Expected Filename",
    "Local Result",
    "Required Manual Action",
]
_UNISYNC_HEADERS = [
    "Label", "AlbumNo", "AlbumTitle", "AlbumNoMasters", "ReleaseDate",
    "WorkTitle", "TrackNo", "workAudioId", "Filename", "PipsCode",
    "ComposerNames", "AlbumCoverArt", "CDNAlbumArt",
]


@dataclass(frozen=True)
class MetadataTrack:
    track_id: str
    filename: str


@dataclass
class DeltaResult:
    initial_delivery: bool = False
    additions: int = 0
    renames: int = 0
    removals: int = 0
    files_prepared: int = 0
    unavailable_sources: int = 0
    invalid_rows: int = 0
    preparation_errors: int = 0
    local_files_removed: int = 0
    local_removal_errors: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.additions or self.renames or self.removals)

    @property
    def ok(self) -> bool:
        return (
            self.invalid_rows == 0
            and self.unavailable_sources == 0
            and self.preparation_errors == 0
            and self.local_removal_errors == 0
        )


def _track_id(value: str) -> str:
    """Normalize spreadsheet-style IDs (including ``123.0``) to text."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d+\.0+", text):
        return text.split(".", 1)[0]
    return text


def _id_from_filename(path_or_name: str | Path) -> str:
    match = _TRACK_ID_RE.search(Path(path_or_name).stem)
    return match.group(1) if match else ""


def _expected_aif_filename(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or Path(raw).name != raw:
        return ""
    return f"{Path(raw).stem}.aif"


def _read_metadata(path: Path) -> tuple[dict[str, MetadataTrack], int]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        id_col = _find_column(columns, POSSIBLE_EXTERNAL_ID_COLS)
        filename_col = _find_column(columns, POSSIBLE_FILENAME_COLS)
        if not id_col or not filename_col:
            raise ValueError(
                "SourceAudio metadata must contain External Id and Filename "
                f"columns; found {columns!r}"
            )

        tracks: dict[str, MetadataTrack] = {}
        invalid = 0
        for row_number, row in enumerate(reader, start=2):
            track_id = _track_id(row.get(id_col, ""))
            filename = _expected_aif_filename(row.get(filename_col, ""))
            if not track_id:
                track_id = _id_from_filename(filename)
            if not track_id or not filename:
                invalid += 1
                continue
            current = tracks.get(track_id)
            if current and current.filename.casefold() != filename.casefold():
                raise ValueError(
                    f"External Id {track_id!r} has conflicting filenames on "
                    f"metadata row {row_number}: {current.filename!r} and "
                    f"{filename!r}"
                )
            tracks[track_id] = MetadataTrack(track_id, filename)
        return tracks, invalid


def _read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _derive_cover_url(example_url: str, cover_filename: str) -> str:
    """Reuse a tracklist CDNAlbumArt URL structure with a new cover token."""
    parsed = urlsplit(str(example_url or "").strip())
    cover = Path(str(cover_filename or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not cover.stem:
        return ""
    parent = parsed.path.rsplit("/", 1)[0]
    return urlunsplit((parsed.scheme, parsed.netloc, f"{parent}/{cover.stem}.webp", "", ""))


def _write_unisync_request(
    metadata_path: Path,
    track_ids: set[str],
) -> Path:
    """Write a known-good UniSync CSV whose BOM cannot mask workAudioId."""
    columns, rows = _read_csv_rows(metadata_path)
    id_col = _find_column(columns, POSSIBLE_EXTERNAL_ID_COLS)
    filename_col = _find_column(columns, POSSIBLE_FILENAME_COLS)
    if not id_col or not filename_col:
        raise ValueError("Cannot build UniSync request without External Id and Filename")

    handle = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".csv",
        prefix="sourceaudio_additions_",
        encoding="utf-8-sig",
        newline="",
        delete=False,
    )
    path = Path(handle.name)
    try:
        writer = csv.DictWriter(handle, fieldnames=_UNISYNC_HEADERS, lineterminator="\n")
        writer.writeheader()
        written = 0
        for row in rows:
            track_id = _track_id(row.get(id_col, ""))
            if track_id not in track_ids:
                continue
            writer.writerow({
                "workAudioId": track_id,
                "Filename": f"{Path(row.get(filename_col, '')).stem}.wav",
            })
            written += 1
    finally:
        handle.close()
    if written != len(track_ids):
        path.unlink(missing_ok=True)
        raise ValueError(
            f"UniSync request expected {len(track_ids)} rows but wrote {written}"
        )
    return path


def _files_with_suffixes(root: Path, suffixes: set[str]) -> Iterable[Path]:
    if not root.is_dir():
        return ()
    return (
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.casefold() in suffixes
    )


def _index_audio(root: Path, suffixes: set[str]) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = defaultdict(list)
    for path in _files_with_suffixes(root, suffixes):
        track_id = _id_from_filename(path.name)
        if track_id:
            index[track_id].append(path)
    return dict(index)


def _propagate_downloaded_audio(
    client_root: Path,
    destination_media: Path,
    track_ids: set[str],
    logger: logging.Logger,
) -> bool:
    """Copy newly downloaded canonical WAVs into the normal staging tree."""
    sources = _index_audio(client_root, {".wav", ".wave"})
    client_media = client_root / "MEDIA"
    copied = skipped = errors = 0
    for track_id in sorted(track_ids):
        matches = sources.get(track_id, [])
        if len(matches) != 1:
            logger.error(
                f"     ✗ Expected one canonical WAV for {track_id}; found {len(matches)}."
            )
            errors += 1
            continue
        source = matches[0]
        try:
            relative = source.relative_to(client_media)
        except ValueError:
            relative = source.relative_to(client_root)
        destination = destination_media / relative
        if destination.exists():
            skipped += 1
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied += 1
        except OSError as exc:
            logger.error(f"     ✗ Could not stage {source.name}: {exc}")
            errors += 1
    logger.info(
        f"     Canonical WAV propagation: {copied} copied, {skipped} already "
        f"present, {errors} error(s)."
    )
    return errors == 0


def _download_addition_covers(
    ctx: ReleaseContext,
    metadata_path: Path,
    source_media: Path,
    addition_ids: set[str],
    logger: logging.Logger,
) -> bool:
    """Download new US album covers using the current tracklist CDN pattern."""
    if not addition_ids:
        return True
    track_columns, track_rows = _read_csv_rows(ctx.us_tracklist_csv)
    url_col = _find_column(track_columns, POSSIBLE_URL_COLS)
    example_url = next(
        (str(row.get(url_col, "")).strip() for row in track_rows if url_col and row.get(url_col)),
        "",
    )
    if not example_url:
        logger.error("     ✗ No CDNAlbumArt URL pattern found in the US tracklist.")
        return False

    columns, rows = _read_csv_rows(metadata_path)
    id_col = _find_column(columns, POSSIBLE_EXTERNAL_ID_COLS)
    cover_col = _find_column(columns, POSSIBLE_COVER_COLS)
    label_col = _find_column(columns, POSSIBLE_LABEL_COLS)
    if not id_col or not cover_col or not label_col:
        logger.error(
            "     ✗ Refreshed SourceAudio metadata lacks External Id, Label, "
            "or Album Cover Art; cannot stage new covers."
        )
        return False

    source_index = _index_audio(source_media, {".wav", ".wave"})
    cover_specs: dict[str, tuple[str, Path]] = {}
    for row in rows:
        track_id = _track_id(row.get(id_col, ""))
        if track_id not in addition_ids:
            continue
        cover_name = str(row.get(cover_col, "")).strip()
        label = str(row.get(label_col, "")).strip()
        matches = source_index.get(track_id, [])
        if cover_name and label and len(matches) == 1:
            cover_specs.setdefault(cover_name, (label, matches[0].parent))

    from config import MASTERS_COVERS_DIR
    from covers import _sanitize_path_component
    import requests

    failures = 0
    for cover_name, (label, album_dir) in sorted(cover_specs.items()):
        url = _derive_cover_url(example_url, cover_name)
        if not url:
            logger.error(f"     ✗ Could not derive cover URL for {cover_name}")
            failures += 1
            continue
        destinations = [
            MASTERS_COVERS_DIR / _sanitize_path_component(label) / cover_name,
            ctx.specials_dir / "1-ORIGINAL" / "Covers" / cover_name,
            album_dir / cover_name,
        ]
        if all(path.is_file() and path.stat().st_size > 0 for path in destinations):
            continue
        try:
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            if not response.content:
                raise ValueError("empty response")
            for destination in destinations:
                destination.parent.mkdir(parents=True, exist_ok=True)
                temp = destination.with_name(f".{destination.name}.tmp")
                temp.write_bytes(response.content)
                temp.replace(destination)
            logger.info(f"     ✓ Staged new album cover: {cover_name}")
        except Exception as exc:
            logger.error(f"     ✗ Cover download failed for {cover_name}: {exc}")
            failures += 1
    if not cover_specs:
        logger.error("     ✗ No usable cover rows found for the added US tracks.")
        return False
    return failures == 0


def _acquire_missing_sources(
    ctx: ReleaseContext,
    territory: str,
    metadata_path: Path,
    destination_media: Path,
    track_ids: set[str],
    logger: logging.Logger,
) -> bool:
    """Run the same territory/cache/client UniSync job used by initial delivery."""
    job_name = "US WAV" if territory == "us" else "Ex-US WAV"
    base_job = next((job for job in ctx.unisync_jobs if job["name"] == job_name), None)
    if not base_job:
        logger.error(f"     ✗ No canonical UniSync job named {job_name!r}.")
        return False
    request_path = _write_unisync_request(metadata_path, track_ids)
    job = dict(base_job)
    job["name"] = f"{job_name} SourceAudio additions"
    job["csv"] = str(request_path)
    logger.info(
        f"     Fetching {len(track_ids)} SourceAudio addition(s) through the "
        f"canonical {job_name} route."
    )
    logger.info(f"       Territory: {job['territory']}")
    logger.info(f"       Cache:     {job['cache_path']}")
    logger.info(f"       Client:    {job['client_path']}")
    try:
        from unisync_automation import STATUS_FAILED, run_all_unisync_jobs

        results = run_all_unisync_jobs(
            SimpleNamespace(unisync_jobs=[job]), False, logger
        )
        if results.get(job["name"]) == STATUS_FAILED:
            return False
        return _propagate_downloaded_audio(
            Path(job["client_path"]), destination_media, track_ids, logger
        )
    finally:
        request_path.unlink(missing_ok=True)


def _archive_existing_missing(missing_dir: Path, logger: logging.Logger) -> None:
    if not missing_dir.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = missing_dir.with_name(f"{missing_dir.name}-archived-{stamp}")
    counter = 2
    while archive.exists():
        archive = missing_dir.with_name(
            f"{missing_dir.name}-archived-{stamp}-{counter}"
        )
        counter += 1
    missing_dir.replace(archive)
    logger.info(f"     Archived prior Missing folder → {archive.name}")


def _convert_to_aif(source: Path, destination: Path) -> tuple[bool, str]:
    """Convert a source master to AIFF while retaining PCM depth and metadata."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg:
        return False, "ffmpeg is not installed"

    bits = 16
    codec_name = ""
    if ffprobe:
        probe = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=bits_per_sample,codec_name",
                "-of", "default=noprint_wrappers=1", str(source),
            ],
            capture_output=True,
        )
        if probe.returncode == 0:
            values = {}
            for line in probe.stdout.decode("utf-8", "replace").splitlines():
                key, _, value = line.partition("=")
                values[key] = value
            codec_name = values.get("codec_name", "")
            try:
                bits = int(values.get("bits_per_sample") or 16)
            except ValueError:
                bits = 16

    if codec_name.startswith("pcm_f"):
        codec = "pcm_f32be" if bits <= 32 else "pcm_f64be"
    elif bits <= 16:
        codec = "pcm_s16be"
    elif bits <= 24:
        codec = "pcm_s24be"
    else:
        codec = "pcm_s32be"

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{destination.name}.tmp.aif")
    result = subprocess.run(
        [
            ffmpeg, "-y", "-v", "error", "-i", str(source),
            "-map_metadata", "0", "-codec:a", codec, str(temp),
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        temp.unlink(missing_ok=True)
        detail = result.stderr.decode("utf-8", "replace").strip()
        return False, detail[-500:] or f"ffmpeg exited {result.returncode}"
    temp.replace(destination)
    return True, "converted from source master"


def _write_report(path: Path, rows: list[dict[str, str]]) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def reconcile_sourceaudio_refresh(
    *,
    metadata_path: Path,
    media_dir: Path,
    source_dir: Path,
    logger: logging.Logger,
    dry_run: bool = False,
    correction_package: bool = True,
    converter: Optional[Callable[[Path, Path], tuple[bool, str]]] = None,
) -> DeltaResult:
    """Reconcile refreshed metadata in place or as a delivered correction.

    An empty/nonexistent AIFF delivery is treated as the initial workflow run,
    because Step 1 exports metadata before Step 11 creates the AIFF files.
    """
    metadata_path = Path(metadata_path)
    media_dir = Path(media_dir)
    source_dir = Path(source_dir)
    result = DeltaResult()

    if not metadata_path.is_file():
        raise FileNotFoundError(f"SourceAudio metadata not found: {metadata_path}")
    existing_files = list(_files_with_suffixes(media_dir, _AUDIO_SUFFIXES))
    if not existing_files:
        result.initial_delivery = True
        logger.info(
            "     SourceAudio delta: no existing AIFF delivery; treating this "
            "as the initial workflow export."
        )
        return result
    expected, result.invalid_rows = _read_metadata(metadata_path)
    existing = _index_audio(media_dir, _AUDIO_SUFFIXES)
    expected_ids = set(expected)
    existing_ids = set(existing)
    missing_dir = media_dir.parent / "Missing"
    report_path = (
        missing_dir / _REPORT_NAME
        if correction_package
        else metadata_path.parent / "SourceAudio Refresh Audit.csv"
    )

    additions = sorted(expected_ids - existing_ids)
    removals = sorted(existing_ids - expected_ids)
    renames: list[str] = []
    for track_id in sorted(expected_ids & existing_ids):
        desired = expected[track_id].filename
        present = {path.name for path in existing[track_id]}
        if desired not in present:
            renames.append(track_id)

    result.additions = len(additions)
    result.renames = len(renames)
    result.removals = len(removals)
    logger.info(
        "     SourceAudio refresh delta: "
        f"{result.additions} addition(s), {result.renames} filename change(s), "
        f"{result.removals} removal(s)."
    )
    if not result.has_changes and not result.invalid_rows:
        if correction_package and missing_dir.exists():
            if dry_run:
                logger.info(
                    f"     [DRY RUN] Would archive stale prior package: {missing_dir}"
                )
            else:
                _archive_existing_missing(missing_dir, logger)
        logger.info("     ✓ Refreshed metadata matches the existing AIFF delivery.")
        return result

    if dry_run:
        destination = missing_dir if correction_package else media_dir
        logger.info(f"     [DRY RUN] Would reconcile refreshed audio at {destination}")
        return result

    if correction_package:
        _archive_existing_missing(missing_dir, logger)
        missing_dir.mkdir(parents=True, exist_ok=False)
    else:
        report_path.parent.mkdir(parents=True, exist_ok=True)
    source_index = _index_audio(source_dir, _SOURCE_SUFFIXES) if additions else {}
    convert = converter or _convert_to_aif
    rows: list[dict[str, str]] = []
    obsolete_paths: dict[str, list[Path]] = {}

    for track_id in additions:
        desired = expected[track_id].filename
        matches = source_index.get(track_id, [])
        if len(matches) == 1:
            if correction_package:
                destination = missing_dir / desired
            else:
                try:
                    relative_parent = matches[0].parent.relative_to(source_dir)
                except ValueError:
                    relative_parent = Path()
                destination = media_dir / relative_parent / desired
            destination.parent.mkdir(parents=True, exist_ok=True)
            ok, detail = convert(matches[0], destination)
        elif not matches:
            ok, detail = False, f"No source master found under {source_dir}"
        else:
            names = "; ".join(str(path) for path in matches)
            ok, detail = False, f"Multiple source masters matched: {names}"
        if ok:
            result.files_prepared += 1
            local_result = f"Prepared {desired}: {detail}"
        else:
            result.unavailable_sources += 1
            local_result = f"NOT PREPARED: {detail}"
        rows.append({
            "Action": "ADDITION_UPLOAD" if correction_package else "ADDITION_IN_PLACE",
            "External Id": track_id,
            "Existing Filename": "",
            "Expected Filename": desired,
            "Local Result": local_result,
            "Required Manual Action": (
                "Upload prepared AIF and refreshed metadata to SourceAudio"
                if correction_package else "None — pending local delivery updated"
            ),
        })

    for track_id in renames:
        desired = expected[track_id].filename
        old_paths = sorted(existing[track_id], key=lambda path: str(path).casefold())
        obsolete_paths[track_id] = old_paths
        source = old_paths[0]
        destination = missing_dir / desired if correction_package else source.with_name(desired)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        result.files_prepared += 1
        old_names = "; ".join(path.name for path in old_paths)
        rows.append({
            "Action": (
                "RENAMED_FILENAME_UPLOAD" if correction_package
                else "RENAMED_FILENAME_IN_PLACE"
            ),
            "External Id": track_id,
            "Existing Filename": old_names,
            "Expected Filename": desired,
            "Local Result": f"Prepared {desired} from existing AIFF; obsolete local filename pending removal",
            "Required Manual Action": (
                "Upload the renamed AIF and refreshed metadata, then remove the "
                "obsolete filename from SourceAudio"
                if correction_package else "None — pending local delivery updated"
            ),
        })

    for track_id in removals:
        obsolete_paths[track_id] = sorted(
            existing[track_id], key=lambda path: str(path).casefold()
        )
        old_names = "; ".join(
            path.name for path in obsolete_paths[track_id]
        )
        rows.append({
            "Action": (
                "REMOVE_FROM_SOURCEAUDIO" if correction_package
                else "REMOVAL_IN_PLACE"
            ),
            "External Id": track_id,
            "Existing Filename": old_names,
            "Expected Filename": "",
            "Local Result": "Obsolete local AIFF pending removal",
            "Required Manual Action": (
                "Remove this obsolete track from SourceAudio"
                if correction_package else "None — pending local delivery updated"
            ),
        })

    if result.invalid_rows:
        rows.append({
            "Action": "INVALID_METADATA_ROWS",
            "External Id": "",
            "Existing Filename": "",
            "Expected Filename": "",
            "Local Result": f"{result.invalid_rows} metadata row(s) lacked a usable ID or filename",
            "Required Manual Action": "Correct the Domo export before uploading",
        })

    # Do not delete from the completed delivery unless the replacement package
    # is complete.  This keeps a missing/ambiguous source master from turning a
    # recoverable refresh problem into data loss.
    can_remove = result.unavailable_sources == 0 and result.invalid_rows == 0
    if can_remove:
        removal_results: dict[str, str] = {}
        for track_id, paths in obsolete_paths.items():
            removed: list[str] = []
            failures: list[str] = []
            for path in paths:
                try:
                    path.unlink()
                    result.local_files_removed += 1
                    removed.append(path.name)
                except OSError as exc:
                    result.local_removal_errors += 1
                    failures.append(f"{path.name}: {exc}")
            detail = f"Removed local AIFF(s): {'; '.join(removed)}" if removed else ""
            if failures:
                detail = (detail + "; " if detail else "") + (
                    f"LOCAL REMOVAL FAILED: {'; '.join(failures)}"
                )
            removal_results[track_id] = detail
        for row in rows:
            if row["Action"] in {
                "RENAMED_FILENAME_UPLOAD", "RENAMED_FILENAME_IN_PLACE",
                "REMOVE_FROM_SOURCEAUDIO", "REMOVAL_IN_PLACE",
            }:
                row["Local Result"] = (
                    row["Local Result"].split("; obsolete", 1)[0]
                    if row["Action"] in {
                        "RENAMED_FILENAME_UPLOAD", "RENAMED_FILENAME_IN_PLACE"
                    }
                    else ""
                )
                removed_detail = removal_results.get(row["External Id"], "")
                row["Local Result"] = "; ".join(
                    part for part in (row["Local Result"], removed_detail) if part
                )
    else:
        for row in rows:
            if row["Action"] in {
                "RENAMED_FILENAME_UPLOAD", "RENAMED_FILENAME_IN_PLACE",
                "REMOVE_FROM_SOURCEAUDIO", "REMOVAL_IN_PLACE",
            }:
                row["Local Result"] += "; removal deferred because the refresh package is incomplete"

    _write_report(report_path, rows)
    logger.info(f"     Audit report: {report_path}")
    if result.files_prepared:
        logger.info(f"     ✓ Prepared {result.files_prepared} AIF upload candidate(s).")
    if correction_package and (result.removals or result.renames):
        if can_remove:
            logger.warning(
                f"     ⚠ Removed {result.local_files_removed} obsolete local AIFF(s). "
                "SourceAudio service deletions are manual; review REMOVE/RENAME "
                "rows in the audit CSV."
            )
        else:
            logger.warning(
                "     ⚠ Obsolete local AIFF removal was deferred because the "
                "refresh package is incomplete."
            )
    elif not correction_package and (result.removals or result.renames):
        logger.info(
            f"     ✓ Pending delivery updated in place; removed "
            f"{result.local_files_removed} obsolete local AIFF(s)."
        )
    if result.unavailable_sources:
        logger.error(
            f"     ✗ {result.unavailable_sources} added track(s) could not be "
            "prepared because source audio was unavailable or ambiguous."
        )
    if result.local_removal_errors:
        logger.error(
            f"     ✗ {result.local_removal_errors} obsolete local AIFF(s) could "
            "not be removed; see the audit CSV."
        )
    return result


def _territory_paths(ctx: ReleaseContext, territory: str) -> tuple[Path, Path, Path]:
    if territory == "us":
        return (
            ctx.partner_metadata["sourceaudio"],
            ctx.partner_dirs["sourceaudio_music"],
            ctx.specials_dir / "1-ORIGINAL" / "Music" / "WAV w COVERS" / "MEDIA",
        )
    if territory == "exus":
        return (
            ctx.partner_metadata["sourceaudio_exus"],
            ctx.partner_dirs["sourceaudio_exus_music"],
            ctx.partner_dirs["exus_staging_media"],
        )
    raise ValueError(f"Unsupported SourceAudio territory: {territory!r}")


def reconcile_context_sourceaudio(
    ctx: ReleaseContext,
    territory: str,
    *,
    logger: logging.Logger,
    dry_run: bool = False,
) -> DeltaResult:
    metadata, media, source = _territory_paths(ctx, territory)
    from delivery_state import partner_is_delivered

    partner_key = "sourceaudio" if territory == "us" else "sourceaudio_exus"
    correction_package = partner_is_delivered(ctx.specials_dir, partner_key)
    logger.info(
        "     Refresh mode: "
        + (
            "DELIVERED — build a separate Missing correction package"
            if correction_package
            else "PENDING — update the existing delivery in place"
        )
    )
    preparation_errors = 0
    existing_files = list(_files_with_suffixes(media, _AUDIO_SUFFIXES))
    additions: set[str] = set()
    if metadata.is_file() and existing_files:
        expected, _invalid = _read_metadata(metadata)
        existing = _index_audio(media, _AUDIO_SUFFIXES)
        additions = set(expected) - set(existing)

    if additions and not dry_run:
        available_sources = _index_audio(source, _SOURCE_SUFFIXES)
        missing_sources = {
            track_id for track_id in additions
            if len(available_sources.get(track_id, [])) != 1
        }
        if missing_sources and not _acquire_missing_sources(
            ctx, territory, metadata, source, missing_sources, logger
        ):
            preparation_errors += 1
        if territory == "us" and not _download_addition_covers(
            ctx, metadata, source, additions, logger
        ):
            preparation_errors += 1

    result = reconcile_sourceaudio_refresh(
        metadata_path=metadata,
        media_dir=media,
        source_dir=source,
        logger=logger,
        dry_run=dry_run,
        correction_package=correction_package,
    )
    result.preparation_errors += preparation_errors
    return result


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Build SourceAudio Missing packages after refreshed Domo metadata."
    )
    parser.add_argument("--territory", choices=("us", "exus", "both"), default="both")
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--part", type=int, choices=(1, 2))
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--full-month-content", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("sourceaudio_delta")
    ctx = context_from_cli_args(args)
    territories = ("us", "exus") if args.territory == "both" else (args.territory,)
    ok = True
    for territory in territories:
        logger.info(f"── SourceAudio refresh reconciliation: {territory.upper()} ──")
        result = reconcile_context_sourceaudio(
            ctx, territory, logger=logger, dry_run=args.dry_run
        )
        ok = ok and result.ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
