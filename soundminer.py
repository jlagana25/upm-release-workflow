"""
soundminer.py — Step 12: Soundminer v5Pro NBC Workflow
========================================================
Drives Soundminer v5Pro through the NBC embed + mirror pipeline.

Substeps (matching the workflow spec):
  12.1  The NBC Metadata CSV is exported in Step 1 (domo_exports) and lands
        at ctx.nbc_metadata_csv.  We assume it exists by the time this
        module runs.

  12.2  Launch / activate Soundminer v5Pro and switch the toolbar database
        dropdown to "NBCUniversal" via keyboard shortcut ⌘5 (in the toolbar
        database menu, NBCUniversal is bound to "⌘5").

  12.3  Database → Delete all records (AppleScript menu click).  If a
        confirmation alert appears, dismiss with Enter.

  12.4  Database → Import text into database.  Two NSOpenPanels appear:
        first for the metadata CSV, then for the audio source folder.
        Both are driven via the same Cmd+Shift+G pattern used in
        unisync_automation._open_panel_go_to_path().

  12.5  Cmd+A to select all records, then right-click in the file list to
        open the context menu, then click "Embed selected records".  Wait
        for embedding to complete (polled via UI status, with a long
        timeout safety net).

  12.6  Database → Mirror to open the Mirror Settings dialog.  Soundminer
        retains one global settings state between runs, so the workflow
        explicitly applies the complete NBC profile before every mirror.
        In --attended mode the operator may review the applied values before
        continuing; unattended mode uses the verified automated profile.

        Required settings for the NBC (Broadcast Wave) mirror:
          Final File Type: Broadcast Wave
          Interleaved: ON
          Sum to Mono: OFF
          Decode M/S → L/R: OFF
          Copy Markers Across: OFF
          Embed Metadata Into Mirrored Files: ON
          Destination Folder Structure: Mirror Source Folder Structure
          File Exists Behavior: Skip Existing
          CPU Usage: 1
          Filename Scheme: <Source:1>_<TrackTitle:2>
          Use mono(.M) extension: ON
          Filename Limit: 255
          Strip illegal characters: ON
          Use Source SR/Bit Depth: ON
          Sample Rate: Not Applicable    (greyed; controlled by Use Source)
          Bit Depth: Not Applicable      (greyed; controlled by Use Source)

  12.7  After OK, a destination-folder NSOpenPanel opens.  Navigate to
        ctx.partner_dirs["nbc_wav_music"] and accept.  Then poll that
        folder for new .wav files until the count stabilises — same
        completion-detection pattern UniSync uses, which has held up
        through hours-long jobs in production.

Reference screenshots needed in SCREENSHOTS_DIR (tight crops of the
unambiguous UI fragment each name describes):

    soundminer_db_nbc_selected.png   — "NBCUniversal" text inside the
                                       closed toolbar dropdown (so we can
                                       confirm the switch worked).
    soundminer_mirror_title.png      — the "Mirror Settings" title bar
                                       (proves the dialog is up).
    soundminer_mirror_ok.png         — the OK button inside Mirror Settings.
    soundminer_embed_menu.png        — "Embed selected records" row in
                                       the right-click context menu.

Crop each as small as possible — locateOnScreen is more reliable when
the search target is a tight, unambiguous fragment.  All four references
must exist before a real run; verify_screenshots() fails loudly otherwise.

Prerequisites:
    pip install pyautogui Pillow
    macOS:  System Settings → Privacy & Security →
              Accessibility   → allow Terminal
              Screen Recording → allow Terminal
              Automation       → allow Terminal to control
                                 "Soundminer v5Pro" and "System Events"
"""

from __future__ import annotations

import csv
import json
import logging
import math
import re
import subprocess
import time
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

from config import ReleaseContext, current_hostname

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SOUNDMINER_APP = "Soundminer v5Pro"

# Paths are derived from this script's own location rather than hardcoded,
# so the module works wherever the repo lives — critical here because it
# runs on the REMOTE Soundminer Mac, whose layout
# (/Volumes/hdfuser/Documents/Scripts/Python/…) differs from the pipeline
# machine's.  soundminer.py sits at <repo>/files/soundminer.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_FILES_DIR = Path(__file__).resolve().parent

# Reference crops live INSIDE the code folder so they're versioned in git and a
# fresh clone already has them.  They're split into a per-machine subfolder
# (by hostname) because a pixel crop only matches the screen it was captured on —
# so each Mac reads its own set and both sets coexist in git without conflict.
SCREENSHOTS_DIR = _FILES_DIR / "screenshots" / current_hostname()

# Diagnostics stay OUTSIDE the repo (writable, machine-local, not versioned).
DEBUG_STEP_DIR = _REPO_ROOT / "_logs" / "soundminer_debug_steps"

FAILURE_SCREENSHOTS_DIR = _REPO_ROOT / "_logs" / "soundminer_failures"
RUNTIME_DIR = _REPO_ROOT / "_logs" / "soundminer_runtime"

# Timeouts (seconds).  Soundminer operations vary widely with catalog size;
# these are generous ceilings rather than tight bounds.  Override per-run
# via the CLI flags or kwargs.
LAUNCH_WAIT             = 5.0
POST_MENU_WAIT          = 1.0      # after firing a menu click via AppleScript
POST_CLICK_WAIT         = 0.6
DIALOG_OPEN_WAIT        = 2.0      # NSOpenPanel animation
ALERT_DISMISS_WAIT      = 0.8
IMPORT_TIMEOUT          = 7200     # 2h ceiling for "Import text into database"
EMBED_TIMEOUT           = 14400    # 4h ceiling for "Embed selected records"
MIRROR_TIMEOUT          = 21600    # 6h ceiling for the actual mirror
MIRROR_STABILITY_WINDOW = 60       # seconds with no new files ⇒ mirror is done.
                                   # Lowered from 180: a several-thousand-file
                                   # mirror writes steadily, so ~1 min with no
                                   # new file reliably means done, and we stop
                                   # idling ~2 min sooner after the count settles.
MIRROR_STARTUP_GRACE    = 600      # seconds to see the FIRST output file before
                                   # concluding the mirror never started

# Screen-idle detection — the automated equivalent of an operator watching the
# Soundminer status bar settle before pressing Enter.  Used for the phases we
# can't poll on the filesystem (scan / import / embed) when running unattended:
# we sample a small grayscale snapshot of the screen and treat the phase as
# finished once the picture stops changing for SCREEN_IDLE_STABILITY seconds.
SCREEN_IDLE_STABILITY   = 20       # s of no on-screen change ⇒ phase complete
SCREEN_IDLE_DIFF        = 2.0      # mean |Δ| (0-255 grayscale) counted as motion
SCREEN_IDLE_POLL        = 3.0      # s between idle-detection snapshots

# Polling
LOCATE_CONFIDENCE       = 0.85     # pyautogui confidence threshold
LOCATE_RETRIES          = 12       # attempts before giving up
LOCATE_DELAY            = 2.0      # seconds between retries
POLL_INTERVAL           = 10       # seconds between progress checks

# Manual-handshake interval for operations we can't detect programmatically.
# When --unattended is False (the default), the operator sees a clearly
# labelled "Press ENTER when X completes" prompt during these phases.
PROGRESS_DOT_INTERVAL   = 30       # seconds between "still running…" log lines

# Status flags
STATUS_OK      = "ok"
STATUS_SKIPPED = "skipped"
STATUS_FAILED  = "failed"

# Reference screenshots that MUST exist for a real (non-dry-run) execution.
REQUIRED_SCREENSHOTS: dict[str, str] = {
    "db_nbc_selected":  "soundminer_db_nbc_selected.png",
    "mirror_title":     "soundminer_mirror_title.png",
    "mirror_ok":        "soundminer_mirror_ok.png",
    # Note: embedding (12.5) now uses the Database menu-bar item
    # "Embed Metadata for Selected Records" via AppleScript, so no
    # context-menu crop is needed.  db_nbc_selected is also only a
    # best-effort verify (12.2 trusts the ⌘5 hotkey).  The two mirror
    # crops remain load-bearing for 12.6.
}

# Conditional dialogs that appear during import/embed.  These are OPTIONAL:
# each only shows up in some runs (e.g. only when there are unmatched fields
# or duplicate records), so a missing crop is not a preflight failure — it
# just means that particular auto-dismissal is skipped and the operator
# handles it during the manual handshake.  Crop each tightly around a
# distinctive part of the dialog.
OPTIONAL_DIALOG_SCREENSHOTS: dict[str, str] = {
    # Progress bar shown while "Import text into database" runs.
    "importing_text":   "soundminer_importing_text.png",
    # "Some columns could not be mapped" notification during embed → OK.
    "unmatched_fields": "soundminer_unmatched_fields.png",
    # Duplicate-records warning during embed → OK.
    "dupes_warning":    "soundminer_dupes_warning.png",
    # Post-embed log window listing files that were not scanned.
    "log_window":       "soundminer_log_window.png",
}


# Diagnostic toggle — write a numbered step screenshot for every state
# transition.  Set via --capture-steps in the CLI.  Lives at module scope
# so the helpers can read it without parameter gymnastics.
CAPTURE_STEPS = False

# These three columns are intentionally absent from the NBCUniversal database
# today.  The import dialog may be acknowledged only when its unmatched-field
# set is a subset of this audited allowlist; any new field stops the run.
ALLOWED_UNMATCHED_FIELDS = frozenset({
    "is_SongBasedonLyrics",
    "HasVocals",
    "Is_Explicit",
})


class _SoundminerError(RuntimeError):
    """Raised when a Soundminer UI step doesn't reach its expected state."""


def _checkpoint_path(ctx: ReleaseContext, workflow: str) -> Path:
    return RUNTIME_DIR / f"{ctx.release_id}-{workflow}-checkpoint.json"


def _load_checkpoint(ctx: ReleaseContext, workflow: str) -> dict:
    path = _checkpoint_path(ctx, workflow)
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _reset_checkpoint(ctx: ReleaseContext, workflow: str) -> None:
    try:
        _checkpoint_path(ctx, workflow).unlink()
    except FileNotFoundError:
        pass


def _mark_checkpoint(
    ctx: ReleaseContext,
    workflow: str,
    phase: str,
    **details,
) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    path = _checkpoint_path(ctx, workflow)
    payload = _load_checkpoint(ctx, workflow)
    payload.update({
        "schema_version": 1,
        "release_id": ctx.release_id,
        "workflow": workflow,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    phases = payload.setdefault("completed_phases", {})
    phases[phase] = {
        "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        **details,
    }
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def _checkpoint_completed(checkpoint: dict, phase: str) -> bool:
    return phase in checkpoint.get("completed_phases", {})


def _normalise_audio_identity(value: str) -> str:
    """Soundminer comparison key, tolerant of extension and mono .M suffix."""
    name = Path(str(value).strip()).name
    stem = Path(name).stem
    if stem.casefold().endswith(".m"):
        stem = stem[:-2]
    return stem.casefold()


def _soundminer_filename_component(value: str) -> str:
    # Soundminer preserves spaces inside field values; the underscore visible
    # in NBC names is the literal separator in <Source:1>_<TrackTitle:2>.
    value = re.sub(r"\s+", " ", str(value).strip())
    # With "Strip illegal characters" enabled, v5Pro transliterates accents
    # and retains only ASCII letters/digits, spaces, and underscores.
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^A-Za-z0-9_ ]", "", value)
    return value.strip(" ._")


def _csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise _SoundminerError(f"CSV has no header row: {path}")
        return list(reader.fieldnames), [dict(row) for row in reader]


def _header(fields: list[str], *candidates: str) -> Optional[str]:
    normalised = {
        re.sub(r"[\s_]", "", field).casefold(): field for field in fields
    }
    for candidate in candidates:
        hit = normalised.get(re.sub(r"[\s_]", "", candidate).casefold())
        if hit:
            return hit
    return None


def _source_wav_identities(audio_folder: Path) -> Counter[str]:
    return Counter(
        _normalise_audio_identity(path.name)
        for path in audio_folder.rglob("*")
        if path.is_file() and path.suffix.lower() == ".wav"
    )


def _validate_nbc_source_manifest(
    csv_path: Path,
    audio_folder: Path,
    logger: logging.Logger,
) -> set[str]:
    fields, rows = _csv_rows(csv_path)
    filename_col = _header(fields, "Filename", "AudioFilename")
    source_col = _header(fields, "Source")
    title_col = _header(fields, "TrackTitle", "Track Title")
    if not filename_col:
        raise _SoundminerError(f"NBC metadata has no Filename column: {csv_path}")
    if not source_col or not title_col:
        raise _SoundminerError(
            "NBC metadata must contain Source and TrackTitle columns so the "
            "mirror filename manifest can be validated before delivery."
        )
    metadata_names: list[str] = []
    expected_outputs: list[str] = []
    for row in rows:
        filename = str(row.get(filename_col, "")).strip()
        if not filename or filename.casefold() == "grand total":
            continue
        metadata_names.append(_normalise_audio_identity(filename))
        output = (
            f"{_soundminer_filename_component(row.get(source_col, ''))}_"
            f"{_soundminer_filename_component(row.get(title_col, ''))}"
        ).strip("_")
        if not output:
            raise _SoundminerError(f"NBC metadata row has no output name: {filename}")
        expected_outputs.append(_normalise_audio_identity(output))

    metadata_counter = Counter(metadata_names)
    source_counter = _source_wav_identities(audio_folder)
    duplicate_metadata = sorted(name for name, count in metadata_counter.items() if count > 1)
    duplicate_sources = sorted(name for name, count in source_counter.items() if count > 1)
    missing = sorted(set(metadata_counter) - set(source_counter))
    if duplicate_metadata or duplicate_sources or missing:
        report = RUNTIME_DIR / f"{csv_path.stem}-source-preflight.csv"
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        with report.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Problem", "Audio identity"])
            writer.writerows(("Missing source WAV", value) for value in missing)
            writer.writerows(("Duplicate metadata filename", value) for value in duplicate_metadata)
            writer.writerows(("Duplicate source basename", value) for value in duplicate_sources)
        raise _SoundminerError(
            "NBC metadata/source preflight failed: "
            f"{len(missing)} missing, {len(duplicate_metadata)} duplicate metadata, "
            f"{len(duplicate_sources)} duplicate source basename(s). Report: {report}"
        )
    output_counter = Counter(expected_outputs)
    collisions = sorted(name for name, count in output_counter.items() if count > 1)
    if collisions:
        raise _SoundminerError(
            f"NBC mirror filename scheme creates {len(collisions)} collision(s); "
            f"first: {collisions[:5]}"
        )
    extras = len(set(source_counter) - set(metadata_counter))
    if extras:
        logger.warning(
            f"  NBC source contains {extras} extra WAV basename(s) not referenced "
            "by metadata; they will not be selected for the validated mirror."
        )
    logger.info(
        f"  ✓ NBC metadata/source preflight: {len(metadata_counter)} unique rows "
        "and WAVs; output filename manifest has no collisions."
    )
    return set(expected_outputs)


def _sourceaudio_manifest(source: Path) -> set[str]:
    counter = _source_wav_identities(source)
    duplicates = sorted(name for name, count in counter.items() if count > 1)
    if duplicates:
        raise _SoundminerError(
            f"SourceAudio source contains {len(duplicates)} duplicate WAV "
            f"basename(s); first: {duplicates[:5]}"
        )
    return set(counter)


def _destination_manifest(
    destination: Path,
    output_exts: tuple[str, ...],
) -> Counter[str]:
    manifest, _wrong = _scan_destination(destination, output_exts)
    return manifest


def _scan_destination(
    destination: Path,
    output_exts: tuple[str, ...],
) -> tuple[Counter[str], list[str]]:
    """Build the manifest and wrong-format list in one Pegasus traversal."""
    wanted = {f".{ext.lower().lstrip('.')}" for ext in output_exts}
    manifest: Counter[str] = Counter()
    wrong: list[str] = []
    if not destination.exists():
        return manifest, wrong
    audio_suffixes = {".wav", ".aif", ".aiff", ".mp3"}
    for path in destination.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in wanted:
            manifest[_normalise_audio_identity(path.name)] += 1
        elif suffix in audio_suffixes:
            wrong.append(str(path))
    return manifest, sorted(wrong)


def _normalize_nbc_nested_mirror(
    mirror_dest: Path,
    audio_source: Path,
    expected: set[str],
    logger: logging.Logger,
) -> bool:
    """Normalize Soundminer 5's volume-relative NBC mirror tree.

    HDF1 can preserve the source path from the mounted volume root, producing
    ``WAV/_Specials/.../SME WAV 48K NBC/MEDIA/...`` instead of the established
    ``WAV/MEDIA/...`` layout. Normalize only after the nested tree itself is a
    complete, duplicate-free match for the expected filename manifest. Missing
    files move into MEDIA; the duplicate wrapper remains recoverable in a
    sibling quarantine.
    """
    parts = audio_source.resolve().parts
    if len(parts) < 5 or parts[1] != "Volumes":
        return False
    nested_relative = Path(*parts[3:])
    nested_media = mirror_dest / nested_relative
    nested_top = mirror_dest / nested_relative.parts[0]
    if not nested_media.is_dir() or nested_media == mirror_dest / "MEDIA":
        return False

    nested_counter, wrong = _scan_destination(nested_media, ("wav",))
    nested_ids = set(nested_counter)
    duplicates = [name for name, count in nested_counter.items() if count > 1]
    if nested_ids != expected or duplicates or wrong:
        logger.warning(
            "    Nested NBC mirror is not yet safe to normalize: "
            f"{len(expected - nested_ids)} missing, "
            f"{len(nested_ids - expected)} unexpected, "
            f"{len(duplicates)} duplicate, {len(wrong)} wrong-format."
        )
        return False

    correct_media = mirror_dest / "MEDIA"
    present, present_wrong = _scan_destination(correct_media, ("wav",))
    if present_wrong:
        return False
    present_ids = set(present)
    moved = 0
    for source_file in sorted(nested_media.rglob("*")):
        if not source_file.is_file() or source_file.suffix.lower() != ".wav":
            continue
        identity = _normalise_audio_identity(source_file.name)
        if identity in present_ids:
            continue
        relative = source_file.relative_to(nested_media)
        target = correct_media / relative
        if target.exists():
            raise _SoundminerError(
                "NBC nested-mirror normalization found an occupied target "
                f"with a different audio identity: {target}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        source_file.replace(target)
        present_ids.add(identity)
        moved += 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    quarantine = mirror_dest.parent / f"_mirror_quarantine_{stamp}"
    suffix = 1
    while quarantine.exists():
        quarantine = mirror_dest.parent / f"_mirror_quarantine_{stamp}_{suffix}"
        suffix += 1
    quarantine.mkdir(parents=True)
    nested_top.replace(quarantine / nested_top.name)
    logger.warning(
        f"    Normalized Soundminer's volume-relative NBC tree: moved {moved} "
        f"missing file(s) into MEDIA; retained duplicate wrapper at {quarantine}."
    )
    return True


def _validate_destination_manifest(
    destination: Path,
    expected: set[str],
    output_exts: tuple[str, ...],
    logger: logging.Logger,
    label: str,
    *,
    allow_empty: bool = True,
    allow_partial: bool = False,
) -> str:
    actual_counter, wrong_format = _scan_destination(destination, output_exts)
    if not actual_counter and not wrong_format and allow_empty:
        return "empty"
    actual = set(actual_counter)
    missing = sorted(expected - actual)
    extras = sorted(actual - expected)
    duplicates = sorted(name for name, count in actual_counter.items() if count > 1)
    if missing and allow_partial and not (extras or duplicates or wrong_format):
        logger.info(
            f"  ↻ {label} is a valid partial destination: {len(actual)} "
            f"present, {len(missing)} missing. Continuing with Skip Existing."
        )
        return "partial"
    if missing or extras or duplicates or wrong_format:
        report = RUNTIME_DIR / f"{label.lower().replace(' ', '-')}-manifest.csv"
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        with report.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Problem", "Audio identity"])
            writer.writerows(("Missing output", value) for value in missing)
            writer.writerows(("Unexpected output", value) for value in extras)
            writer.writerows(("Duplicate output", value) for value in duplicates)
            writer.writerows(("Wrong-format output", value) for value in wrong_format)
        raise _SoundminerError(
            f"{label} destination manifest is not clean: {len(missing)} missing, "
            f"{len(extras)} unexpected, {len(duplicates)} duplicate, "
            f"{len(wrong_format)} wrong-format. Report: {report}"
        )
    logger.info(f"  ✓ {label} manifest matches all {len(expected)} expected files.")
    return "complete"


def _prepare_nbc_import_csv(
    csv_path: Path,
    logger: logging.Logger,
) -> Path:
    """Create a Soundminer-safe copy without Domo summary/footer rows.

    NBC's Domo export currently ends with ``GRAND TOTAL`` in the Filename
    column.  Soundminer interprets every data row as a soundfile, so importing
    the raw export produces a misleading scan-failure log after all real audio
    has loaded.  Preserve the canonical export and write a disposable runtime
    copy containing the header and real track rows only.
    """
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RUNTIME_DIR / f"{csv_path.stem}-soundminer.csv"
    temp_path = output_path.with_suffix(".csv.tmp")

    with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
        rows = list(csv.reader(source))
    if not rows:
        raise _SoundminerError(f"NBC metadata CSV is empty: {csv_path}")

    header = rows[0]
    try:
        filename_index = next(
            index for index, value in enumerate(header)
            if value.strip().casefold() == "filename"
        )
    except StopIteration as exc:
        raise _SoundminerError(
            f"NBC metadata CSV has no Filename column: {csv_path}"
        ) from exc

    kept_rows = [header]
    removed_lines: list[int] = []
    for line_number, row in enumerate(rows[1:], start=2):
        filename = (
            row[filename_index].strip()
            if filename_index < len(row)
            else ""
        )
        if filename.casefold() == "grand total":
            removed_lines.append(line_number)
            continue
        kept_rows.append(row)

    with temp_path.open("w", encoding="utf-8-sig", newline="") as target:
        csv.writer(target).writerows(kept_rows)
    temp_path.replace(output_path)

    track_count = len(kept_rows) - 1
    if removed_lines:
        logger.info(
            f"  Prepared Soundminer import CSV: {track_count} track row(s); "
            f"removed GRAND TOTAL footer at line(s) "
            f"{', '.join(map(str, removed_lines))}."
        )
    else:
        logger.info(
            f"  Prepared Soundminer import CSV: {track_count} track row(s); "
            "no summary footer found."
        )
    return output_path


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def verify_screenshots(logger: logging.Logger) -> bool:
    """
    Confirm every required reference screenshot exists.  Called from the
    public entry before any UI is touched.
    """
    if not SCREENSHOTS_DIR.exists():
        logger.error(
            f"Screenshots directory not found:\n"
            f"  {SCREENSHOTS_DIR}\n"
            f"  Capture the reference crops on this machine with:\n"
            f"    python3 make_soundminer_crops.py"
        )
        return False

    all_ok = True
    for key, filename in REQUIRED_SCREENSHOTS.items():
        path = SCREENSHOTS_DIR / filename
        if path.exists():
            logger.info(f"  ✓  {filename}")
        else:
            logger.error(f"  ✗  {filename}  ← MISSING\n     {path}")
            all_ok = False

    if not all_ok:
        logger.error(
            "\n  (Re)capture the missing crops on this machine with:\n"
            "    python3 make_soundminer_crops.py\n"
            "  It walks you through each one and saves them to:\n"
            f"  {SCREENSHOTS_DIR}"
        )
    if not all_ok:
        return False

    # Reject blank, near-uniform, or implausibly tiny crops before any
    # destructive database action. Existence alone did not catch bad captures.
    try:
        from PIL import Image, ImageStat
        for filename in REQUIRED_SCREENSHOTS.values():
            path = SCREENSHOTS_DIR / filename
            with Image.open(path) as image:
                grayscale = image.convert("L")
                extrema = grayscale.getextrema()
                deviation = ImageStat.Stat(grayscale).stddev[0]
                if image.width < 8 or image.height < 8 or not extrema or deviation < 2.0:
                    logger.error(
                        f"  ✗  {filename} is not a usable reference crop "
                        f"({image.width}x{image.height}, contrast={deviation:.1f})."
                    )
                    all_ok = False
    except Exception as exc:
        logger.error(f"  ✗  Could not validate screenshot crops: {exc}")
        all_ok = False
    return all_ok


def _verify_pyautogui_installed(logger: logging.Logger) -> bool:
    try:
        import pyautogui  # noqa: F401
        return True
    except ImportError:
        logger.error(
            "pyautogui is not installed.\n"
            "  Run:  pip install pyautogui Pillow"
        )
        return False


def run_soundminer_gui_preflight(logger: logging.Logger) -> bool:
    """Non-destructive HDF1 GUI/session diagnostic."""
    if not _verify_pyautogui_installed(logger) or not verify_screenshots(logger):
        return False
    try:
        import pyautogui
        _activate_soundminer(logger, clear_stale_dialogs=False)
        screenshot = pyautogui.screenshot().convert("L")
        extrema = screenshot.getextrema()
        if not extrema or extrema[1] - extrema[0] < 10:
            raise _SoundminerError(
                f"Screen Recording returned a blank/flat frame: {extrema}"
            )
        script = (
            f'tell application "System Events" to tell process "{SOUNDMINER_APP}" '
            f'to return (exists menu bar 1)'
        )
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=10,
        )
        if result.returncode or result.stdout.strip().lower() != "true":
            raise _SoundminerError(
                "Soundminer menu bar is unavailable through Accessibility: "
                + result.stderr.strip()
            )
        logger.info(
            f"  ✓ HDF1 GUI preflight: capture {screenshot.width}x{screenshot.height}, "
            "Accessibility menu available, reference crops valid."
        )
        return True
    except Exception as exc:
        logger.error(f"  ✗ Soundminer GUI preflight failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_soundminer_nbc_workflow(
    ctx:                            ReleaseContext,
    dry_run:                        bool,
    logger:                         logging.Logger,
    *,
    unattended:                     bool          = False,
    skip_delete_records:            bool          = False,
    skip_import:                    bool          = False,
    skip_embed:                     bool          = False,
    skip_mirror:                    bool          = False,
    resume:                         bool          = False,
    manual_verify_mirror_settings:  Optional[bool] = None,
) -> bool:
    """
    Drive Soundminer v5Pro through the NBC embed + mirror workflow end-to-end.

    Parameters
    ----------
    ctx                  : Release context (paths, dates).
    dry_run              : Log the plan and return True without touching UI.
    logger               : Where to write step logs and warnings.
    unattended           : If True (the DEFAULT when driven by the workflow),
                           run without any "press Enter" prompts: scan/import/
                           embed completion is detected by watching the
                           Soundminer UI settle, the blocking dupes / unmatched-
                           fields dialogs are auto-OK'd, and the complete NBC
                           Mirror Settings profile is applied automatically.
                           Pass attended (orchestrator:
                           --soundminer-attended, soundminer.py: --attended) to
                           restore the supervised pauses — useful for a first
                           run on a new machine to eyeball the mirror settings.
    skip_*               : Individual phase skips for restart/recovery.
    manual_verify_mirror_settings :
                           Override the default (which is `not unattended`).
                           When True, pause for human inspection after the
                           automated profile is applied.  When False, continue
                           with the automatically configured values.

    Returns True on full success; False on any hard failure.
    """
    logger.info("─── Step 12 — Soundminer NBC workflow ─────────────────────")

    # ---- Resolve paths ------------------------------------------------------
    csv_path     = ctx.nbc_metadata_csv
    audio_folder = (
        ctx.specials_dir / "2-STAGING" / "SME WAV 48K NBC" / "MEDIA"
    )
    mirror_dest  = ctx.partner_dirs["nbc_wav_music"]

    logger.info(f"  NBC Metadata CSV: {csv_path}")
    logger.info(f"  Audio source:     {audio_folder}")
    logger.info(f"  Mirror dest:      {mirror_dest}")

    # ---- Dry-run short-circuit ---------------------------------------------
    if dry_run:
        logger.info(
            "  [DRY RUN] Would launch Soundminer, switch to NBCUniversal "
            "database, delete records, import metadata + audio, embed, mirror.\n"
            "            Nothing touched."
        )
        return True

    # ---- Preflight: paths -------------------------------------------------
    if not csv_path.exists():
        logger.error(
            f"  ✗  NBC Metadata CSV not found: {csv_path}\n"
            f"     Run Step 1 (Domo exports) first."
        )
        return False
    if not audio_folder.exists():
        logger.error(
            f"  ✗  Audio source folder not found: {audio_folder}\n"
            f"     Run Step 10 (final_packaging) first to stage WAVs into\n"
            f"     2-STAGING/SME WAV 48K NBC/MEDIA."
        )
        return False
    # Mirror destination doesn't have to exist yet — Soundminer creates it on
    # demand — but its PARENT must (drive mounted, NBC release folder built).
    if not mirror_dest.parent.exists():
        logger.error(
            f"  ✗  Mirror destination parent missing: {mirror_dest.parent}\n"
            f"     The NBC release folder should have been created during\n"
            f"     Step 2 (folder_setup).  Re-run folder setup or check that\n"
            f"     the Pegasus volume is mounted."
        )
        return False
    mirror_dest.mkdir(parents=True, exist_ok=True)
    try:
        expected_manifest = _validate_nbc_source_manifest(
            csv_path, audio_folder, logger
        )
        destination_state = _validate_destination_manifest(
            mirror_dest, expected_manifest, ("wav",), logger, "NBC mirror",
            allow_partial=True,
        )
    except _SoundminerError as exc:
        logger.error(f"  ✗  {exc}")
        return False
    expected_wav_count = len(expected_manifest)
    if destination_state == "complete" and not skip_mirror:
        logger.info(
            "  ↩  NBC destination is already complete and exact; skipping "
            "database mutation/import/embed/mirror."
        )
        _mark_checkpoint(ctx, "nbc", "mirror", files=expected_wav_count)
        return True

    if not resume:
        _reset_checkpoint(ctx, "nbc")
    checkpoint = _load_checkpoint(ctx, "nbc") if resume else {}
    if resume and checkpoint:
        logger.info(f"  ↻ Resuming NBC from checkpoint: {_checkpoint_path(ctx, 'nbc')}")
        skip_delete_records = skip_delete_records or _checkpoint_completed(checkpoint, "delete")
        skip_import = skip_import or _checkpoint_completed(checkpoint, "import")
        skip_embed = skip_embed or _checkpoint_completed(checkpoint, "embed")

    # ---- Preflight: tooling -----------------------------------------------
    if not run_soundminer_gui_preflight(logger):
        return False

    # Resolve the "manual verify mirror settings" decision
    if manual_verify_mirror_settings is None:
        manual_verify_mirror_settings = not unattended

    # Prepare diagnostic dirs
    DEBUG_STEP_DIR.mkdir(parents=True, exist_ok=True)
    FAILURE_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    # ---- Drive the workflow -----------------------------------------------
    try:
        _activate_soundminer(logger)
        _switch_to_nbcuniversal(logger)

        if skip_delete_records:
            logger.info("  ↩  Skipping 12.3 delete-all-records (per flag).")
        else:
            _delete_all_records(logger)
            _mark_checkpoint(ctx, "nbc", "delete")

        if skip_import:
            logger.info("  ↩  Skipping 12.4 import metadata (per flag).")
        else:
            import_csv = _prepare_nbc_import_csv(csv_path, logger)
            _import_metadata(import_csv, audio_folder, logger,
                             unattended=unattended)
            _mark_checkpoint(ctx, "nbc", "import", records=expected_wav_count)

        if skip_embed:
            logger.info("  ↩  Skipping 12.5 embed selected records (per flag).")
        else:
            _select_all_and_embed(logger, unattended=unattended)
            _mark_checkpoint(ctx, "nbc", "embed", records=expected_wav_count)

        if skip_mirror:
            logger.info("  ↩  Skipping 12.6 mirror (per flag).")
            logger.info("  ✓  Step 12 partial — mirror skipped per flag.")
            return True

        _select_all_records(logger)
        _open_mirror_dialog(logger)
        mirror_bounds = _configure_mirror_settings("nbc", logger)
        _verify_mirror_settings_dialog(
            logger,
            manual_verify=manual_verify_mirror_settings,
        )
        _click_mirror_ok(logger, mirror_bounds)
        _navigate_mirror_destination(mirror_dest, logger)
        _wait_for_mirror_complete(
            mirror_dest,
            logger,
            output_exts=("wav",),
            reject_exts=("aif", "aiff"),
            expected_count=expected_wav_count,
            expected_manifest=expected_manifest,
            manifest_label="NBC mirror",
            normalize_nbc_source=audio_folder,
        )
        _mark_checkpoint(ctx, "nbc", "mirror", files=expected_wav_count)

        logger.info("  ✓  Step 12 complete — NBC mirror finished.")
        return True

    except _SoundminerError as exc:
        logger.error(f"  ✗  Soundminer UI step failed: {exc}")
        _capture_failure_screenshot("step12_fail", logger)
        return False
    except KeyboardInterrupt:
        logger.warning(
            "  ⚠  Step 12 cancelled by user (KeyboardInterrupt).  Soundminer\n"
            "     may be in a partial state — verify the database, mirror\n"
            "     destination, and re-run individual phases as needed using\n"
            "     --skip-* flags."
        )
        _capture_failure_screenshot("step12_cancelled", logger)
        return False
    except Exception as exc:
        logger.error(
            f"  ✗  Unexpected error in Step 12: "
            f"{type(exc).__name__}: {exc!r}"
        )
        _capture_failure_screenshot("step12_unexpected", logger)
        return False


# ---------------------------------------------------------------------------
# Step 11 — SourceAudio: scan folder → AIFF mirror (two source/dest pairs)
# ---------------------------------------------------------------------------

# Mirror Settings required for the SourceAudio (AIFF) mirror.
SOURCEAUDIO_MIRROR_SETTINGS = (
    "        Required SourceAudio Mirror Settings (verify all match before OK):\n"
    "          Final File Type:                    AIFF\n"
    "          Interleaved:                        ON\n"
    "          Sum to Mono:                        OFF\n"
    "          Decode M/S files to L/R:            OFF\n"
    "          Copy Markers Across:                OFF\n"
    "          Embed Metadata Into Mirrored Files: ON\n"
    "          Destination Folder Structure:       Build Using Library then Volume\n"
    "          File Exists Behavior:               Skip Existing\n"
    "          CPU Usage:                          1\n"
    "          Filename Scheme:                    <Filename:1>\n"
    "          Use mono(.M) extension:             ON\n"
    "          Filename Limit:                     255\n"
    "          Strip illegal characters:           ON\n"
    "          Use Source SR/Bit Depth:            ON\n"
    "          Sample Rate:                        Not Applicable\n"
    "          Bit Depth:                          Not Applicable"
)

SOURCEAUDIO_OUTPUT_EXTS = ("aif", "aiff")

# Soundminer has one global Mirror Settings state shared by every database.
# These profiles are therefore APPLIED before every mirror rather than merely
# documented or assumed to have persisted from a previous run.
MIRROR_PROFILES = {
    "sourceaudio": {
        "label": "SourceAudio AIFF",
        "final_file_type": "AIFF",
        "final_file_type_index": 2,
        "destination_structure": "Build Using Library then Volume",
        "destination_structure_index": 11,
        "filename_scheme": "<Filename:1>",
    },
    "nbc": {
        "label": "NBC Broadcast Wave",
        "final_file_type": "Broadcast Wave",
        "final_file_type_index": 1,
        "destination_structure": "Mirror Source Folder Structure",
        "destination_structure_index": 17,
        "filename_scheme": "<Source:1>_<TrackTitle:2>",
    },
}

# Normalized control centres measured relative to the Soundminer 5.0v560
# Mirror Settings window.  The dialog scales with the display, so normalized
# positions remain stable across the two HDF Macs' display modes.
_MIRROR_CONTROL_POINTS = {
    "final_file_type":       (0.66, 0.12),
    "interleaved":           (0.36, 0.162),
    "sum_to_mono":           (0.36, 0.203),
    "decode_ms":             (0.36, 0.245),
    "copy_markers":          (0.36, 0.286),
    "embed_metadata":        (0.36, 0.328),
    "destination_structure": (0.66, 0.370),
    "file_exists_behavior":  (0.66, 0.412),
    "cpu_usage":             (0.66, 0.454),
    "filename_scheme":       (0.66, 0.532),
    "mono_extension":        (0.36, 0.573),
    "filename_limit":        (0.66, 0.615),
    "strip_illegal":         (0.36, 0.656),
    "use_source_format":     (0.36, 0.735),
    "ok":                    (0.90, 0.954),
}

_MIRROR_CHECKBOX_STATES = {
    "interleaved":       True,
    "sum_to_mono":       False,
    "decode_ms":         False,
    "copy_markers":      False,
    "embed_metadata":    True,
    "mono_extension":    True,
    "strip_illegal":     True,
    "use_source_format": True,
}


def _switch_to_sourceaudio(
    shortcut: Optional[str],
    logger:   logging.Logger,
    *,
    confirm:  bool,
) -> None:
    """Switch the active Soundminer database to SourceAudio.

    Preferred: a ⌘<shortcut> global hotkey (e.g. ⌘8), exactly like NBC's ⌘5 —
    deterministic and idempotent.  Trusted without a hard image-match
    (same rationale as _switch_to_nbcuniversal).

    If no shortcut is configured we cannot switch programmatically.  In an
    attended run we then PAUSE so the operator selects the SourceAudio
    database by hand (critical — the next step deletes all of that database's
    records).  In an unattended run with no shortcut we refuse, because
    silently deleting records from whatever database happens to be active is
    dangerous.
    """
    import pyautogui

    _activate_soundminer(logger)
    time.sleep(0.6)

    if shortcut:
        logger.info(f"  11a Switching database to SourceAudio (⌘{shortcut})…")
        pyautogui.hotkey("command", str(shortcut))
        time.sleep(1.2)
        _save_step_screenshot("11a_after_db_switch", logger)
        return

    if not confirm:
        raise _SoundminerError(
            "SourceAudio needs the database selected before it deletes records, "
            "but no --sourceaudio-db-shortcut was given and the run is "
            "unattended.  Provide the ⌘-digit shortcut for the SourceAudio "
            "database, or run attended so you can select it."
        )

    logger.info("  11a No DB shortcut configured — asking operator to select "
                "SourceAudio.")
    print("")
    print("  ╔═══════════════════════════════════════════════════════════════╗")
    print("  ║  Select the SourceAudio database in Soundminer NOW.           ║")
    print("  ║  The next step DELETES ALL RECORDS in the active database,    ║")
    print("  ║  so it must be SourceAudio — not NBCUniversal or any DB you    ║")
    print("  ║  want to keep.                                                ║")
    print("  ╚═══════════════════════════════════════════════════════════════╝")
    try:
        input("  >>> Press ENTER once SourceAudio is the active database: ")
    except EOFError:
        raise _SoundminerError(
            "SourceAudio DB selection needs an interactive terminal; provide "
            "--sourceaudio-db-shortcut for unattended runs."
        )
    _save_step_screenshot("11a_after_db_switch", logger)


def _scan_sounds_into_database(
    scan_folder: Path,
    logger:      logging.Logger,
    *,
    unattended:  bool,
) -> None:
    """
    Database → "Scan Sounds into Database".  Drives the NSOpenPanel that
    follows (the folder to scan), auto-dismisses the post-scan Unmatched
    Fields / Check-for-Dupes dialogs, then waits for the scan to settle.
    """
    logger.info('  11b Database → "Scan Sounds into Database"…')
    _menu_click("Database", "Scan Sounds into Database", logger)
    time.sleep(DIALOG_OPEN_WAIT)
    _save_step_screenshot("11b_scan_dialog", logger)

    logger.info(f"        → Selecting folder to scan: {scan_folder}")
    _open_panel_go_to_path(str(scan_folder), logger, select_directory=True)
    time.sleep(2.0)
    _save_step_screenshot("11c_after_scan_folder", logger)

    # The scan can raise the same Unmatched Fields / Dupes dialogs the import
    # path does; auto-dismiss them (best-effort).
    scan_start_observed = _watch_and_dismiss_import_dialogs(logger)

    _wait_with_manual_handshake(
        phase_label  = "scan",
        soft_minutes = 2,
        hard_timeout = IMPORT_TIMEOUT,
        unattended   = unattended,
        logger       = logger,
        on_poll      = lambda: _dismiss_import_dialogs_once(logger),
        initial_activity = scan_start_observed,
    )
    logger.info("        ✓ Scan into database complete.")


def run_soundminer_sourceaudio_workflow(
    ctx:                            ReleaseContext,
    dry_run:                        bool,
    logger:                         logging.Logger,
    *,
    unattended:                     bool           = False,
    manual_verify_mirror_settings:  Optional[bool] = None,
    db_shortcut:                    Optional[str]  = None,
    resume:                         bool           = False,
) -> bool:
    """
    SourceAudio delivery (Step 11 — runs right before the NBC Soundminer step).

    For each (source folder → destination) pair:
        delete all records → Scan Sounds into Database → Mirror to AIFF.

      1. WAV w COVERS/MEDIA            → …Release - SourceAudio/Music
      2. 2-STAGING/SME WAV ExUS/MEDIA  → …Release - SourceAudio Ex-US/Music

    The mirror uses the SourceAudio settings (AIFF, Build Using Library then
    Volume, <Filename:1>, etc.).  Soundminer persists ONE set of mirror settings
    globally, including the incompatible Broadcast Wave settings used by Step
    12, so Step 11 explicitly applies its complete profile before every mirror.
    Before each mirror we ⌘A select-all so the whole scanned database is
    mirrored.  This step runs inline on the Soundminer machine and, in a full
    run, is followed immediately by Step 12 (NBC) with no hand-off.

    Returns True on full success; False on any hard failure.
    """
    logger.info("─── Step 11 — Soundminer SourceAudio (AIFF) workflow ────")

    pairs = [
        ("US",
         ctx.specials_dir / "1-ORIGINAL" / "Music" / "WAV w COVERS" / "MEDIA",
         ctx.partner_dirs["sourceaudio_music"]),
        ("Ex-US",
         ctx.specials_dir / "2-STAGING" / "SME WAV ExUS" / "MEDIA",
         ctx.partner_dirs["sourceaudio_exus_music"]),
    ]

    for tag, src, dest in pairs:
        logger.info(f"  SourceAudio {tag}: {src}")
        logger.info(f"             → {dest}")

    if dry_run:
        logger.info(
            "  [DRY RUN] Would launch Soundminer and, for each pair, delete "
            "records,\n"
            '            "Scan Sounds into Database" on the source folder, then '
            "mirror\n"
            "            to AIFF at the destination.  Nothing touched."
        )
        return True

    # Preflight: tooling + source presence
    if not run_soundminer_gui_preflight(logger):
        return False

    if manual_verify_mirror_settings is None:
        manual_verify_mirror_settings = not unattended

    DEBUG_STEP_DIR.mkdir(parents=True, exist_ok=True)
    FAILURE_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    if not resume:
        _reset_checkpoint(ctx, "sourceaudio")
    checkpoint = _load_checkpoint(ctx, "sourceaudio") if resume else {}
    if resume and checkpoint:
        logger.info(
            f"  ↻ Resuming SourceAudio from checkpoint: "
            f"{_checkpoint_path(ctx, 'sourceaudio')}"
        )

    overall_ok = True
    try:
        _activate_soundminer(logger)

        for idx, (tag, src, dest) in enumerate(pairs):
            logger.info(f"\n  ── SourceAudio pair {idx + 1}/2: {tag} ──")
            if not src.exists():
                logger.error(
                    f"  ✗  Source folder not found: {src}\n"
                    f"     ({'WAV w COVERS build (Step 5 tail)' if tag == 'US' else 'Step 10 Ex-US staging'} "
                    f"must run first.)"
                )
                overall_ok = False
                continue
            if not dest.parent.exists():
                logger.error(
                    f"  ✗  Destination parent missing: {dest.parent}\n"
                    f"     The SourceAudio{' Ex-US' if tag == 'Ex-US' else ''} "
                    f"release folder should exist (Step 2 folder setup)."
                )
                overall_ok = False
                continue
            dest.mkdir(parents=True, exist_ok=True)
            try:
                expected_manifest = _sourceaudio_manifest(src)
                destination_state = _validate_destination_manifest(
                    dest, expected_manifest, SOURCEAUDIO_OUTPUT_EXTS,
                    logger, f"SourceAudio {tag}", allow_partial=True,
                )
            except _SoundminerError as exc:
                logger.error(f"  ✗  {exc}")
                overall_ok = False
                continue
            phase_key = f"{tag}.mirror"
            if destination_state == "complete" or (
                resume and _checkpoint_completed(checkpoint, phase_key)
            ):
                # Never trust the checkpoint alone: destination_state was
                # validated immediately above before this skip.
                if destination_state != "complete":
                    logger.error(
                        f"  ✗  {tag} checkpoint says complete but destination "
                        "does not match; refusing to skip."
                    )
                    overall_ok = False
                    continue
                logger.info(f"  ↩  SourceAudio {tag} already complete; skipping pair.")
                continue

            # Switch to the SourceAudio database, then clear it, then scan.
            # (Only the first pass needs the attended fallback if no shortcut.)
            _switch_to_sourceaudio(
                db_shortcut, logger,
                confirm=manual_verify_mirror_settings and idx == 0,
            )
            _delete_all_records(logger)
            _scan_sounds_into_database(src, logger, unattended=unattended)

            _select_all_records(logger)
            _open_mirror_dialog(logger)
            mirror_bounds = _configure_mirror_settings("sourceaudio", logger)
            _verify_mirror_settings_dialog(
                logger,
                manual_verify=manual_verify_mirror_settings,
                expected_text=SOURCEAUDIO_MIRROR_SETTINGS,
            )
            _click_mirror_ok(logger, mirror_bounds)
            _navigate_mirror_destination(dest, logger)
            _wait_for_mirror_complete(
                dest,
                logger,
                output_exts=SOURCEAUDIO_OUTPUT_EXTS,
                reject_exts=("wav",),
                expected_count=sum(
                    1 for path in src.rglob("*")
                    if path.is_file() and path.suffix.lower() == ".wav"
                ),
                expected_manifest=expected_manifest,
                manifest_label=f"SourceAudio {tag}",
            )
            _mark_checkpoint(ctx, "sourceaudio", phase_key, files=len(expected_manifest))
            logger.info(f"  ✓  SourceAudio {tag} mirror finished → {dest}")

            # Only the FIRST pass needs an optional attended review.  The full
            # SourceAudio profile is still explicitly re-applied above for the
            # second pass rather than trusting persisted state.
            manual_verify_mirror_settings = False

        if overall_ok:
            logger.info("  ✓  Step 11 complete — SourceAudio AIFF mirrors finished.")
        else:
            logger.warning("  ⚠  Step 11 finished with one or more pair failures.")
        return overall_ok

    except _SoundminerError as exc:
        logger.error(f"  ✗  SourceAudio UI step failed: {exc}")
        _capture_failure_screenshot("step11_fail", logger)
        return False
    except KeyboardInterrupt:
        logger.warning(
            "  ⚠  Step 11 cancelled by user.  Soundminer may be mid-scan or\n"
            "     mid-mirror — verify the database and destination before re-running."
        )
        _capture_failure_screenshot("step11_cancelled", logger)
        return False
    except Exception as exc:
        logger.error(
            f"  ✗  Unexpected error in Step 11: {type(exc).__name__}: {exc!r}"
        )
        _capture_failure_screenshot("step11_unexpected", logger)
        return False


# ---------------------------------------------------------------------------
# 12.2 — Activate Soundminer + switch to NBCUniversal database
# ---------------------------------------------------------------------------

def _activate_soundminer(
    logger: logging.Logger,
    *,
    clear_stale_dialogs: bool = True,
) -> None:
    """
    Bring Soundminer v5Pro to the foreground; launch it if necessary.

    A pair of Escapes after activation normally dismisses any stuck modal left
    over from a previous aborted run (same defensive pattern UniSync uses).
    Read-only diagnostics pass ``clear_stale_dialogs=False`` so inspection can
    never cancel or dismiss the very state it is trying to capture.
    """
    import pyautogui

    # GUI automation cannot operate through the macOS lock screen.  Screen
    # capture returns only the desktop wallpaper in that state, which used to
    # look like endless progress to the pixel-change watcher when a dynamic
    # wallpaper was active.
    _assert_soundminer_gui_available(logger, require_window=False)

    script = f'tell application "{SOUNDMINER_APP}" to activate'
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.info(f"  Launching {SOUNDMINER_APP}…")
        subprocess.Popen(["open", "-a", SOUNDMINER_APP])
        time.sleep(LAUNCH_WAIT)
    else:
        time.sleep(1.5)

    if clear_stale_dialogs:
        # Clear abandoned pickers from a previous abort. Soundminer can put an
        # OK-only "open file operation failed" alert above the picker; Escape
        # cannot dismiss that alert, so accept only that exact known error and
        # then use Escape to cancel the stale panel underneath. Never click a
        # generic unknown OK/Yes dialog here.
        for _attempt in range(4):
            snapshot = _dialog_accessibility_snapshot(logger)
            if "The open file operation failed" in snapshot:
                logger.warning(
                    "  Clearing stale Soundminer open-panel failure from a "
                    "previous aborted run."
                )
                if _click_known_dialog_ok(logger, "OK"):
                    continue
            pyautogui.press("escape")
            time.sleep(0.3)

    logger.debug(f"  {SOUNDMINER_APP} is active.")
    _assert_soundminer_gui_available(logger, require_window=True)
    _save_step_screenshot("12_2a_activated", logger)


def _restart_soundminer(logger: logging.Logger) -> None:
    """Gracefully restart a stuck Soundminer UI for validated recovery only."""
    logger.warning("  Recovery: gracefully restarting Soundminer v5Pro…")
    result = subprocess.run(
        ["osascript", "-e", f'tell application "{SOUNDMINER_APP}" to quit'],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode:
        raise _SoundminerError(
            "Soundminer did not accept a graceful quit request: "
            + result.stderr.strip()
        )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        running = subprocess.run(
            ["pgrep", "-x", SOUNDMINER_APP],
            capture_output=True,
            text=True,
        )
        if running.returncode != 0:
            break
        time.sleep(1)
    else:
        raise _SoundminerError(
            "Soundminer remained running after a 30-second graceful quit; "
            "refusing to force-kill it."
        )
    opened = subprocess.run(
        ["open", "-a", SOUNDMINER_APP],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if opened.returncode:
        raise _SoundminerError(
            "Soundminer relaunch failed: " + opened.stderr.strip()
        )
    time.sleep(LAUNCH_WAIT)
    _assert_soundminer_gui_available(logger, require_window=True)
    logger.info("  ✓ Soundminer restarted cleanly.")


def _switch_to_nbcuniversal(logger: logging.Logger) -> None:
    """
    Switch the active database to NBCUniversal via the ⌘5 keyboard shortcut
    (in Soundminer's toolbar database menu, NBCUniversal is bound to "⌘5").

    ⌘5 is a deterministic, idempotent global hotkey in Soundminer — it
    switches to NBCUniversal from any other database and is a no-op if
    NBCUniversal is already active.  Because of that we TRUST the keystroke
    rather than gating on a brittle image-match: we still attempt to confirm
    via the toolbar crop, but a non-match only logs a warning and proceeds
    (the crop can fail to match on scale/rendering differences even when the
    correct database is selected, which would otherwise abort the whole run
    for no real reason).

    The keystroke must reach Soundminer, so we explicitly bring it frontmost
    first (Terminal needs Accessibility permission for this to work).
    """
    import pyautogui

    logger.info("  12.2  Switching database to NBCUniversal (⌘5)…")

    # Bring Soundminer frontmost before sending the keystroke.  Without this
    # the hotkey can land on whatever window is active (Terminal, etc.).
    _activate_soundminer(logger)
    time.sleep(0.6)

    pyautogui.hotkey("command", "5")
    time.sleep(1.2)
    _save_step_screenshot("12_2b_after_cmd5", logger)

    # Best-effort confirmation.  ⌘5 is reliable, so a failed match is treated
    # as a warning, not a hard error.
    if _wait_for_image(
        REQUIRED_SCREENSHOTS["db_nbc_selected"],
        timeout=LOCATE_DELAY * 2,   # short — we're not blocking on this
        logger=logger,
    ):
        logger.info("        ✓ NBCUniversal selected (verified).")
    else:
        logger.warning(
            "        ⚠ Could not visually confirm NBCUniversal in the toolbar "
            "after ⌘5.\n"
            "          Proceeding anyway — ⌘5 deterministically selects "
            "NBCUniversal,\n"
            "          and the verify crop can miss on minor rendering "
            "differences.\n"
            "          (Check the 12_2b_after_cmd5 step screenshot if a later "
            "step\n"
            "          behaves as though the wrong database is active.)"
        )


# ---------------------------------------------------------------------------
# 12.3 — Delete all records
# ---------------------------------------------------------------------------

def _delete_all_records(logger: logging.Logger) -> None:
    """
    Database → Delete all records, then dismiss any confirmation alert.

    Implemented via AppleScript System Events menu-item click.  More
    reliable than image matching for menu-bar items, and survives minor
    UI rearrangements.
    """
    logger.info("  12.3  Database → Delete all records…")
    _menu_click("Database", "Delete all records", logger)
    time.sleep(POST_MENU_WAIT)

    # Soundminer typically shows a confirm dialog.  Default button is the
    # destructive action ("Delete" / "OK") in macOS standard alerts, so
    # pressing Return dismisses it the way the operator would.
    _dismiss_alert_if_present(logger, default_action="Enter")
    time.sleep(ALERT_DISMISS_WAIT)
    _save_step_screenshot("12_3_after_delete", logger)
    logger.info("        ✓ Records cleared.")


# ---------------------------------------------------------------------------
# 12.4 — Import metadata + audio
# ---------------------------------------------------------------------------

def _import_metadata(
    csv_path:     Path,
    audio_folder: Path,
    logger:       logging.Logger,
    *,
    unattended:   bool,
) -> None:
    """
    Database → Import text into database.  Drives the two NSOpenPanels
    that follow (first for the CSV, then for the audio folder), then
    waits for the import to complete.

    Completion detection: Soundminer doesn't expose a programmatic "done"
    signal that we can read from outside, so we use a configurable wait
    with progress logging.  In --unattended mode we use the full
    IMPORT_TIMEOUT as the hard ceiling; otherwise the operator is asked
    to press Enter when the status bar settles.
    """
    logger.info("  12.4  Database → Import text into database…")
    _menu_click("Database", "Import text into database", logger)
    time.sleep(DIALOG_OPEN_WAIT)
    _save_step_screenshot("12_4a_import_dialog1", logger)

    logger.info(f"        → Selecting CSV: {csv_path}")
    _open_panel_go_to_path(str(csv_path), logger)
    time.sleep(2.5)  # Soundminer transitions to the audio-folder dialog
    _save_step_screenshot("12_4b_after_csv", logger)

    logger.info(f"        → Selecting audio folder: {audio_folder}")
    _open_panel_go_to_path(str(audio_folder), logger, select_directory=True)
    time.sleep(2.0)
    _save_step_screenshot("12_4c_after_audio", logger)

    # After the panels are confirmed, Soundminer raises the "Unmatched Fields"
    # and "Check for Dupes Warning" dialogs, which BLOCK the import until OK'd.
    # Auto-dismiss them (best-effort; operator handles any during the wait).
    import_start_observed = _watch_and_dismiss_import_dialogs(logger)

    # Soundminer does not expose a machine-readable imported-record count.
    # Its canvas can also remain visually static while records are still being
    # processed, which made the old two-minute idle fallback advance through a
    # partial import.  Give unattended imports a conservative floor scaled to
    # the staged file count; the exact mirror-count gate remains the final
    # correctness check.
    expected_records = sum(
        1 for path in audio_folder.rglob("*")
        if path.is_file() and path.suffix.lower() == ".wav"
    )
    safe_wait_minutes = min(30, max(3, math.ceil(expected_records / 480)))
    minimum_runtime = min(
        15 * 60,
        max(60, math.ceil(expected_records / 1200) * 60),
    )

    def _import_poll_guard() -> bool:
        dismissed = _dismiss_import_dialogs_once(logger)
        _raise_if_soundminer_log_window(logger, phase="import")
        return dismissed

    # Wait for the import to complete and keep auto-OK'ing the two expected
    # confirmation dialogs.  Any Soundminer Log Window is a hard stop: it must
    # be investigated rather than silently continuing into embed/mirror.
    _wait_with_manual_handshake(
        phase_label  = "import",
        soft_minutes = safe_wait_minutes,
        hard_timeout = IMPORT_TIMEOUT,
        unattended   = unattended,
        logger       = logger,
        on_poll      = _import_poll_guard,
        initial_activity = import_start_observed,
        minimum_runtime = minimum_runtime,
    )
    _raise_if_soundminer_log_window(logger, phase="import")
    logger.info("        ✓ Import complete.")


# ---------------------------------------------------------------------------
# 12.5 — Select all + embed selected records
# ---------------------------------------------------------------------------

def _select_all_and_embed(
    logger: logging.Logger,
    *,
    unattended: bool,
) -> None:
    """
    ⌘A to select every record, right-click in the file list to open the
    context menu, then click "Embed selected records".  Waits for the
    embed to complete via the same manual-handshake / hard-timeout
    pattern used for import.
    """
    import pyautogui

    logger.info("  12.5  ⌘A select all → Database → Embed Metadata for Selected Records…")
    _activate_soundminer(logger)
    _focus_record_list(logger)
    pyautogui.hotkey("command", "a")
    time.sleep(1.0)
    _save_step_screenshot("12_5a_after_select_all", logger)

    # Embed via the MENU BAR rather than the right-click context menu.
    # Soundminer exposes this as Database → "Embed Metadata for Selected
    # Records", so we use the same AppleScript System Events menu click that
    # makes 12.3 (Delete all records) reliable — no coordinates, no image
    # match, no dependence on a context menu opening over a specific row.
    _menu_click("Database", "Embed Metadata for Selected Records", logger)
    time.sleep(POST_MENU_WAIT)
    _save_step_screenshot("12_5c_after_embed_click", logger)

    _wait_with_manual_handshake(
        phase_label  = "embed",
        soft_minutes = 5,
        hard_timeout = EMBED_TIMEOUT,
        unattended   = unattended,
        logger       = logger,
        on_poll      = lambda: _raise_if_soundminer_log_window(
            logger, phase="embed"
        ),
    )

    _raise_if_soundminer_log_window(logger, phase="embed")
    logger.info("        ✓ Embed complete.")


# ---------------------------------------------------------------------------
# 12.6 — Mirror dialog: open, verify, OK, navigate destination
# ---------------------------------------------------------------------------

def _open_mirror_dialog(logger: logging.Logger) -> None:
    """Database → Mirror, then (best-effort) confirm the dialog opened.

    The menu click is deterministic, and the operator visually verifies the
    dialog in the next step (_verify_mirror_settings_dialog) before OK is
    clicked.  So a failed title-crop match is treated as a warning, not a
    fatal error — the crop can miss on minor rendering differences even when
    the Mirror Settings dialog is clearly open (as happened in testing,
    where the dialog was fully visible but the match returned nothing).
    """
    logger.info("  12.6a Database → Mirror…")
    _menu_click("Database", "Mirror", logger)
    time.sleep(DIALOG_OPEN_WAIT)

    if _wait_for_image(
        REQUIRED_SCREENSHOTS["mirror_title"],
        timeout=LOCATE_DELAY * 2,   # short — not blocking on this
        logger=logger,
    ):
        logger.info("        ✓ Mirror Settings dialog open (verified).")
    else:
        logger.warning(
            "        ⚠ Could not visually confirm the Mirror Settings dialog "
            "via image match.\n"
            "          Proceeding — Database → Mirror is deterministic and the\n"
            "          settings are verified by the operator in the next step\n"
            "          before OK is clicked.  (Check 12_6a / 12_6b step "
            "screenshots\n"
            "          if the dialog did not actually open.)"
        )
    _save_step_screenshot("12_6a_mirror_dialog_open", logger)


def _mirror_dialog_bounds(
    logger: logging.Logger,
) -> tuple[int, int, int, int]:
    """Return ``(left, top, width, height)`` for Mirror Settings.

    Prefer the Accessibility window geometry.  Soundminer 5's modal is not
    exposed as a separate window on every macOS build, so retain a guarded
    proportional fallback: its predicted title-bar area must visibly look like
    the light dialog chrome before any control is touched.
    """
    import pyautogui

    script = (
        f'tell application "System Events"\n'
        f'  tell process "{SOUNDMINER_APP}"\n'
        f'    repeat with w in windows\n'
        f'      if name of w contains "Mirror Settings" then\n'
        f'        set p to position of w\n'
        f'        set s to size of w\n'
        f'        return (item 1 of p as text) & "," & '
        f'(item 2 of p as text) & "," & (item 1 of s as text) & "," & '
        f'(item 2 of s as text)\n'
        f'      end if\n'
        f'    end repeat\n'
        f'  end tell\n'
        f'end tell\n'
        f'return "none"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        values = [int(v.strip()) for v in result.stdout.strip().split(",")]
        if result.returncode == 0 and len(values) == 4:
            left, top, width, height = values
            sw, sh = pyautogui.size()
            if (
                0 <= left < sw and 0 <= top < sh
                and sw * 0.25 <= width <= sw * 0.50
                and sh * 0.45 <= height <= sh * 0.75
            ):
                logger.debug(
                    f"        Mirror Settings bounds via Accessibility: "
                    f"{left},{top} {width}x{height}"
                )
                return left, top, width, height
    except Exception as exc:
        logger.debug(f"        (Mirror Settings bounds lookup failed: {exc})")

    # Soundminer centres a ~31.3% x 55.6% modal.  This fallback matches both
    # display modes used on HDF1, but it is allowed only when the predicted
    # title bar is visibly a light neutral region (not the blue record grid).
    sw, sh = pyautogui.size()
    width = int(sw * 0.313)
    height = int(sh * 0.556)
    left = int((sw - width) / 2)
    top = int(sh * 0.186)
    screenshot = pyautogui.screenshot()
    sample_y = top + int(height * 0.025)
    scale_x = screenshot.width / sw
    scale_y = screenshot.height / sh
    pixels = []
    for dx in range(-20, 21, 5):
        px = int((left + width // 2 + dx) * scale_x)
        py = int(sample_y * scale_y)
        r, g, b = screenshot.getpixel((px, py))[:3]
        pixels.append((r, g, b))
    light_neutral = sum(
        1 for r, g, b in pixels
        if min(r, g, b) >= 155 and max(r, g, b) - min(r, g, b) <= 45
    )
    if light_neutral < max(3, len(pixels) // 2):
        raise _SoundminerError(
            "Mirror Settings dialog geometry could not be verified; refusing "
            "to click controls blindly."
        )
    logger.debug(
        f"        Mirror Settings bounds via guarded fallback: "
        f"{left},{top} {width}x{height}"
    )
    return left, top, width, height


def _mirror_point(
    bounds: tuple[int, int, int, int],
    control: str,
) -> tuple[int, int]:
    left, top, width, height = bounds
    rx, ry = _MIRROR_CONTROL_POINTS[control]
    return left + int(width * rx), top + int(height * ry)


def _assert_mirror_dialog_visible(
    bounds: tuple[int, int, int, int],
) -> None:
    """Fail closed unless Soundminer's three dark section headers are visible."""
    import pyautogui
    left, top, width, height = bounds
    screenshot = pyautogui.screenshot()
    sw, sh = pyautogui.size()
    scale_x = screenshot.width / sw
    scale_y = screenshot.height / sh
    # Transfer Preferences, Filenaming, and Sample Rate / Bit Depth headers.
    # Sample toward the right side of each header, away from its light text.
    markers = ((0.80, 0.083), (0.80, 0.495), (0.80, 0.699))
    dark = 0
    for rx, ry in markers:
        px = int((left + width * rx) * scale_x)
        py = int((top + height * ry) * scale_y)
        r, g, b = screenshot.getpixel((px, py))[:3]
        if max(r, g, b) <= 85:
            dark += 1
    if dark < 3:
        raise _SoundminerError(
            "Mirror Settings dialog closed or changed unexpectedly while "
            "applying its profile; refusing further clicks."
        )


def _set_mirror_popup(
    bounds: tuple[int, int, int, int],
    control: str,
    value: str,
    option_index: int,
    logger: logging.Logger,
) -> None:
    """Choose a Soundminer popup row without pressing Enter.

    Soundminer's custom popups do not reliably implement macOS menu
    type-to-select.  Pressing Enter after typing can activate the Mirror
    Settings dialog's default OK button, prematurely opening the destination
    chooser.  The v5.0v560 popup rows have a stable 20-point height and open
    immediately below the control; click the known exact row instead.
    """
    import pyautogui
    x, y = _mirror_point(bounds, control)
    pyautogui.click(x, y)
    time.sleep(0.35)

    _left, _top, width, height = bounds
    row_height = height * 0.03345
    first_row_offset = height * 0.0410
    item_x = x
    item_y = int(y + first_row_offset + option_index * row_height)
    logger_text = f"{control}={value} (popup row {option_index})"
    pyautogui.click(item_x, item_y)
    time.sleep(0.35)

    # A second click at the chosen row must never be needed; if the menu did
    # not open, the coordinates would land outside the original control.  Keep
    # the detail available in captured screenshots/log-level diagnostics.
    logger.debug(f"        Set {logger_text}")


def _set_mirror_text(
    bounds: tuple[int, int, int, int],
    control: str,
    value: str,
    logger: logging.Logger,
) -> None:
    """Replace a Mirror Settings text field using clipboard-safe input."""
    import pyautogui
    x, y = _mirror_point(bounds, control)
    pyautogui.click(x, y)
    pyautogui.hotkey("command", "a")
    if _set_clipboard(value, logger):
        pyautogui.hotkey("command", "v")
    else:
        pyautogui.write(value, interval=0.02)
    time.sleep(0.2)


def _mirror_checkbox_is_checked(
    bounds: tuple[int, int, int, int],
    control: str,
) -> bool:
    """Read Soundminer's gold-vs-grey checkbox fill from a small pixel patch."""
    import pyautogui
    x, y = _mirror_point(bounds, control)
    screenshot = pyautogui.screenshot()
    sw, sh = pyautogui.size()
    scale_x = screenshot.width / sw
    scale_y = screenshot.height / sh
    px = int(x * scale_x)
    py = int(y * scale_y)
    patch_x = max(1, int(round(2 * scale_x)))
    patch_y = max(1, int(round(2 * scale_y)))
    warm_pixels = 0
    total = 0
    for dx in range(-int(5 * scale_x), int(5 * scale_x) + 1, patch_x):
        for dy in range(-int(5 * scale_y), int(5 * scale_y) + 1, patch_y):
            r, g, b = screenshot.getpixel((px + dx, py + dy))[:3]
            total += 1
            if r >= 125 and r - b >= 18 and g - b >= 5:
                warm_pixels += 1
    return warm_pixels >= max(4, total // 5)


def _set_mirror_checkbox(
    bounds: tuple[int, int, int, int],
    control: str,
    desired: bool,
) -> None:
    """Normalize one checkbox and verify its resulting visual state."""
    import pyautogui
    current = _mirror_checkbox_is_checked(bounds, control)
    if current != desired:
        pyautogui.click(*_mirror_point(bounds, control))
        time.sleep(0.25)
    actual = _mirror_checkbox_is_checked(bounds, control)
    if actual != desired:
        raise _SoundminerError(
            f"Could not set Mirror Settings checkbox '{control}' to "
            f"{'ON' if desired else 'OFF'}."
        )


def _configure_mirror_settings(
    profile_name: str,
    logger: logging.Logger,
) -> tuple[int, int, int, int]:
    """Apply and verify the complete SourceAudio or NBC mirror profile."""
    if profile_name not in MIRROR_PROFILES:
        raise _SoundminerError(f"Unknown mirror profile: {profile_name}")
    profile = MIRROR_PROFILES[profile_name]
    bounds = _mirror_dialog_bounds(logger)
    _assert_mirror_dialog_visible(bounds)
    logger.info(f"  12.6b Applying {profile['label']} Mirror Settings…")

    _set_mirror_popup(
        bounds,
        "final_file_type",
        profile["final_file_type"],
        profile["final_file_type_index"],
        logger,
    )
    _assert_mirror_dialog_visible(bounds)
    _save_step_screenshot(f"12_6b_{profile_name}_file_type", logger)
    _set_mirror_popup(
        bounds,
        "destination_structure",
        profile["destination_structure"],
        profile["destination_structure_index"],
        logger,
    )
    _assert_mirror_dialog_visible(bounds)
    _save_step_screenshot(f"12_6b_{profile_name}_folder_structure", logger)
    # File Exists Behavior is shared by both profiles and is already the
    # baseline/persisted "Skip Existing" value.  Do not reopen this custom
    # popup: unlike the two profile-varying dropdowns above, changing it adds
    # no value and previously risked firing the dialog's default OK action.
    _set_mirror_text(bounds, "cpu_usage", "1", logger)
    _set_mirror_text(
        bounds, "filename_scheme", profile["filename_scheme"], logger
    )
    _set_mirror_text(bounds, "filename_limit", "255", logger)

    for control, desired in _MIRROR_CHECKBOX_STATES.items():
        _set_mirror_checkbox(bounds, control, desired)

    _save_step_screenshot(f"12_6b_{profile_name}_settings_applied", logger)
    logger.info(
        f"        ✓ Applied {profile['final_file_type']} / "
        f"{profile['destination_structure']} / "
        f"{profile['filename_scheme']} and verified all checkbox states."
    )
    return bounds


def _verify_mirror_settings_dialog(
    logger: logging.Logger,
    *,
    manual_verify: bool,
    expected_text: Optional[str] = None,
) -> None:
    """
    Capture a screenshot of the Mirror Settings dialog and (if
    `manual_verify` is True) pause for operator confirmation that the
    visible settings match the required spec.

    `expected_text` is the block of required settings echoed to the log and
    shown to the operator.  Defaults to the NBC spec; the SourceAudio
    workflow passes its own (AIFF / Build Using Library then Volume / etc.).

    Soundminer retains these settings between runs, so the dialog should
    already be configured correctly.  But because the dialog has 15+
    controls and image-matching each one is fragile, we offload the
    correctness check to a human eyeball.  --unattended skips the pause
    on the assumption a previous attended run confirmed the settings.
    """
    _save_step_screenshot("12_6b_mirror_settings", logger)
    snapshot_path = (
        FAILURE_SCREENSHOTS_DIR
        / f"mirror_settings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    )
    try:
        import pyautogui
        pyautogui.screenshot(str(snapshot_path))
        logger.info(f"        Mirror Settings snapshot: {snapshot_path}")
    except Exception as exc:
        logger.warning(f"        Could not save settings snapshot: {exc}")

    if expected_text is None:
        expected_text = (
            "        Required Mirror Settings (verify all match before OK):\n"
            "          Final File Type:                    Broadcast Wave\n"
            "          Interleaved:                        ON\n"
            "          Sum to Mono:                        OFF\n"
            "          Decode M/S files to L/R:            OFF\n"
            "          Copy Markers Across:                OFF\n"
            "          Embed Metadata Into Mirrored Files: ON\n"
            "          Destination Folder Structure:       Mirror Source Folder Structure\n"
            "          File Exists Behavior:               Skip Existing\n"
            "          CPU Usage:                          1\n"
            "          Filename Scheme:                    <Source:1>_<TrackTitle:2>\n"
            "          Use mono(.M) extension:             ON\n"
            "          Filename Limit:                     255\n"
            "          Strip illegal characters:           ON\n"
            "          Use Source SR/Bit Depth:            ON\n"
            "          Sample Rate:                        Not Applicable\n"
            "          Bit Depth:                          Not Applicable"
        )
    logger.info(expected_text)

    if not manual_verify:
        logger.info(
            "        Settings were applied automatically; continuing without "
            "a manual confirmation pause."
        )
        return

    # Manual confirmation — block until the operator presses Enter.
    print("")
    print("  ╔════════════════════════════════════════════════════════════════════╗")
    print("  ║  VERIFY MIRROR SETTINGS dialog matches the required values above.  ║")
    print("  ║  Adjust any setting in Soundminer if needed, THEN press ENTER      ║")
    print("  ║  in this terminal to continue (script will click OK).              ║")
    print("  ║                                                                    ║")
    print("  ║  Ctrl+C to abort.                                                  ║")
    print("  ╚════════════════════════════════════════════════════════════════════╝")
    try:
        input("  >>> Press ENTER to continue: ")
    except EOFError:
        # Non-interactive environment — fail closed.
        raise _SoundminerError(
            "Mirror Settings verification requires an interactive terminal.  "
            "Pass --unattended to bypass this check (only after a manual "
            "confirmation run)."
        )


def _click_mirror_ok(
    logger: logging.Logger,
    bounds: Optional[tuple[int, int, int, int]] = None,
) -> None:
    """
    Confirm the Mirror Settings dialog (trigger its default OK button).

    Use the verified Accessibility/logical dialog bounds from the settings
    configurator. Image matches are returned in Retina screenshot pixels on
    HDF1 while ``pyautogui.click`` consumes logical display coordinates; using
    a raw image-match centre can therefore click at twice the intended point.
    After clicking, explicitly prove the settings dialog closed before the
    destination path is allowed anywhere near the keyboard focus.
    """
    import pyautogui

    if bounds is None:
        bounds = _mirror_dialog_bounds(logger)
    ok_x, ok_y = _mirror_point(bounds, "ok")
    logger.info(
        "  12.6c Clicking verified Mirror Settings OK at "
        f"({ok_x}, {ok_y})…"
    )
    pyautogui.click(ok_x, ok_y)
    time.sleep(POST_CLICK_WAIT)
    _save_step_screenshot("12_6c_after_mirror_ok", logger)

    try:
        _assert_mirror_dialog_visible(bounds)
    except _SoundminerError:
        return

    # A custom Soundminer control can occasionally consume the first click as
    # focus-only. Retry the same verified control once, then fail closed.
    logger.warning("        Mirror Settings remained open; retrying verified OK once…")
    pyautogui.click(ok_x, ok_y)
    time.sleep(POST_CLICK_WAIT)
    _save_step_screenshot("12_6c_after_mirror_ok_retry", logger)
    try:
        _assert_mirror_dialog_visible(bounds)
    except _SoundminerError:
        return
    raise _SoundminerError(
        "Mirror Settings remained open after two verified OK clicks; refusing "
        "to type the destination path into the focused settings field."
    )


def _navigate_mirror_destination(
    mirror_dest: Path,
    logger:      logging.Logger,
) -> None:
    """
    After OK in Mirror Settings, Soundminer opens an NSOpenPanel asking
    for the destination folder.  Use the same Cmd+Shift+G pattern from
    UniSync to navigate to ``mirror_dest`` and confirm.
    """
    logger.info(f"  12.6d Selecting mirror destination: {mirror_dest}")
    time.sleep(DIALOG_OPEN_WAIT)
    _open_panel_go_to_path(str(mirror_dest), logger, select_directory=True)
    _confirm_mirror_destination_panel(logger)
    time.sleep(2.0)
    _save_step_screenshot("12_6d_after_dest", logger)


def _confirm_mirror_destination_panel(logger: logging.Logger) -> None:
    """Click the folder picker's final Open button and verify it closes.

    Soundminer's destination NSOpenPanel sometimes navigates to the requested
    folder after the two Return keys but leaves the final ``Open`` button
    waiting.  Accessibility is preferred; a guarded proportional click is the
    fallback for Soundminer builds that do not expose the panel hierarchy.
    """
    import pyautogui

    script = (
        f'tell application "System Events"\n'
        f'  tell process "{SOUNDMINER_APP}"\n'
        f'    repeat with buttonName in {{"Open", "Choose"}}\n'
        f'      repeat with w in windows\n'
        f'        try\n'
        f'          if exists button (buttonName as string) of w then\n'
        f'            click button (buttonName as string) of w\n'
        f'            return "clicked"\n'
        f'          end if\n'
        f'        end try\n'
        f'        try\n'
        f'          repeat with s in sheets of w\n'
        f'            if exists button (buttonName as string) of s then\n'
        f'              click button (buttonName as string) of s\n'
        f'              return "clicked"\n'
        f'            end if\n'
        f'          end repeat\n'
        f'        end try\n'
        f'      end repeat\n'
        f'    end repeat\n'
        f'  end tell\n'
        f'end tell\n'
        f'return "none"'
    )
    clicked = False
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        clicked = result.stdout.strip() == "clicked"
    except Exception as exc:
        logger.debug(f"        (Open-button Accessibility click failed: {exc})")

    if clicked:
        logger.info("        ✓ Confirmed mirror destination with Open.")
        time.sleep(1.0)
    else:
        # Guarded fallback for the centred macOS folder picker used on HDF1.
        # Only use it if the expected light panel covers the screen centre;
        # otherwise fail rather than click an unrelated application.
        screenshot = pyautogui.screenshot()
        sw, sh = pyautogui.size()
        scale_x = screenshot.width / sw
        scale_y = screenshot.height / sh
        cx, cy = int(sw * 0.50 * scale_x), int(sh * 0.50 * scale_y)
        r, g, b = screenshot.getpixel((cx, cy))[:3]
        panel_is_light = (
            min(r, g, b) >= 175 and max(r, g, b) - min(r, g, b) <= 35
        )
        if panel_is_light:
            open_x, open_y = int(sw * 0.769), int(sh * 0.663)
            logger.info(
                f"        Open button not exposed through Accessibility; "
                f"clicking verified folder-picker Open at ({open_x}, {open_y})…"
            )
            pyautogui.click(open_x, open_y)
            time.sleep(1.0)
        else:
            # _open_panel_go_to_path() already sends Return after selecting the
            # folder. Soundminer commonly accepts that Return and immediately
            # replaces the picker with its dark Processing Records modal, so
            # there is no Open button left to click. The exact destination
            # count/filename manifest below remains the authoritative proof
            # that this apparent start was real and complete.
            logger.info(
                "        ✓ Destination picker already closed; mirror processing "
                "has started."
            )

    # Fail immediately if the light folder panel is still covering the screen
    # centre.  This prevents the output poll from idling for ten minutes while
    # an unconfirmed Open button remains visible.
    screenshot = pyautogui.screenshot()
    sw, sh = pyautogui.size()
    scale_x = screenshot.width / sw
    scale_y = screenshot.height / sh
    cx, cy = int(sw * 0.50 * scale_x), int(sh * 0.50 * scale_y)
    r, g, b = screenshot.getpixel((cx, cy))[:3]
    if min(r, g, b) >= 175 and max(r, g, b) - min(r, g, b) <= 35:
        raise _SoundminerError(
            "Mirror destination folder picker is still open after confirming "
            "Open; mirror was not started."
        )


# ---------------------------------------------------------------------------
# 12.7 — Wait for mirror completion
# ---------------------------------------------------------------------------

def _wait_for_mirror_complete(
    mirror_dest: Path,
    logger:      logging.Logger,
    output_exts: tuple[str, ...] = ("wav",),
    reject_exts: tuple[str, ...] = (),
    expected_count: Optional[int] = None,
    expected_manifest: Optional[set[str]] = None,
    manifest_label: str = "Soundminer mirror",
    normalize_nbc_source: Optional[Path] = None,
) -> None:
    """
    Poll ``mirror_dest`` for new output files until the count stabilises
    for MIRROR_STABILITY_WINDOW seconds.  When ``expected_count`` is supplied,
    a stable short count is a hard failure rather than a false success.  Same completion-detection
    pattern UniSync's _wait_for_job_output uses, generalised to "any file
    with one of ``output_exts`` appearing under the dest tree" (NBC mirrors
    to .wav; SourceAudio mirrors to .aif/.aiff).
    """
    exts = tuple(e.lower().lstrip(".") for e in output_exts)
    rejected = tuple(e.lower().lstrip(".") for e in reject_exts)
    logger.info(
        f"  Polling mirror destination for new "
        f"{'/'.join('.' + e for e in exts)} files…"
    )

    def _count_extensions(wanted: tuple[str, ...]) -> int:
        # Compare suffixes case-insensitively: Soundminer/file-system settings
        # can yield .AIF/.AIFF as well as lowercase extensions.
        wanted_set = {f".{ext}" for ext in wanted}
        return sum(
            1 for path in mirror_dest.rglob("*")
            if path.is_file() and path.suffix.lower() in wanted_set
        )

    start              = time.monotonic()
    last_count         = -1
    last_change        = time.monotonic()
    first_seen_at      = None
    next_progress_log  = start + PROGRESS_DOT_INTERVAL

    while True:
        now = time.monotonic()
        elapsed = now - start

        # Hard timeout — Soundminer should not legitimately take this long
        # (MIRROR_TIMEOUT defaults to 6 hours).
        if elapsed > MIRROR_TIMEOUT:
            raise _SoundminerError(
                f"Mirror timed out after {MIRROR_TIMEOUT}s with no "
                f"completion signal.  Last file count: {last_count}.\n"
                f"  Check Soundminer for an error dialog or stalled job."
            )

        # Count output files under the destination tree
        try:
            count = _count_extensions(exts)
            rejected_count = _count_extensions(rejected) if rejected else 0
        except Exception as exc:
            logger.warning(f"    Could not count dest files: {exc}")
            count = last_count
            rejected_count = 0

        if rejected_count:
            raise _SoundminerError(
                f"Mirror produced {rejected_count} wrong-format "
                f"{('/'.join('.' + e for e in rejected))} file(s) in an "
                f"{('/'.join('.' + e for e in exts))} destination.\n"
                f"  Stop this delivery and correct the Mirror Settings "
                f"before retrying."
            )

        if count != last_count:
            if first_seen_at is None and count > 0:
                first_seen_at = now
                logger.info(
                    f"    First mirrored file detected after {int(elapsed)}s."
                )
            logger.debug(f"    Mirror progress: {count} file(s) so far.")
            last_count  = count
            last_change = now

        # Startup grace: if we still haven't seen a single file after
        # MIRROR_STARTUP_GRACE, the mirror probably never started.
        if first_seen_at is None and elapsed > MIRROR_STARTUP_GRACE:
            raise _SoundminerError(
                f"Mirror produced no output after {MIRROR_STARTUP_GRACE}s.\n"
                f"  Check Soundminer for an error dialog or for the\n"
                f"  destination-folder NSOpenPanel still being open."
            )

        # Stability window: count stopped changing — mirror is done.
        if first_seen_at is not None and (now - last_change) >= MIRROR_STABILITY_WINDOW:
            if (
                normalize_nbc_source is not None
                and expected_manifest is not None
                and expected_count is not None
                and last_count != expected_count
            ):
                if _normalize_nbc_nested_mirror(
                    mirror_dest,
                    normalize_nbc_source,
                    expected_manifest,
                    logger,
                ):
                    last_count = _count_extensions(exts)
            if expected_count is not None and last_count != expected_count:
                raise _SoundminerError(
                    f"Mirror stopped at {last_count} output file(s), but the "
                    f"source contains {expected_count}.\n"
                    f"  The import/selection may be incomplete, or a partial "
                    f"destination may have prevented Skip Existing from "
                    f"resuming. Do not continue until the counts match."
                )
            if expected_manifest is not None:
                _validate_destination_manifest(
                    mirror_dest,
                    expected_manifest,
                    exts,
                    logger,
                    manifest_label,
                    allow_empty=False,
                )
            logger.info(
                f"    File count stable at {last_count} for "
                f"{MIRROR_STABILITY_WINDOW}s — mirror complete."
            )
            return

        # Periodic progress logging
        if now >= next_progress_log:
            logger.info(
                f"    … mirror running ({int(elapsed)}s elapsed, "
                f"{last_count} file(s) so far)."
            )
            next_progress_log = now + PROGRESS_DOT_INTERVAL

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Low-level helpers — menus, alerts, file dialogs
# ---------------------------------------------------------------------------

def _menu_click(
    menu_title: str,
    item_title: str,
    logger:     logging.Logger,
) -> None:
    """
    Fire a menu-bar click on `menu_title` → `item_title` via AppleScript
    System Events.  Far more reliable than image-matching for items
    whose label text is stable.

    Soundminer's menu bar can take a moment to become available through the
    Accessibility API after activation/database switching.  Wait for it, then
    traverse the actual macOS hierarchy:

        menu bar 1 → menu bar item → menu 1 → menu item

    Addressing a named ``menu`` directly below ``menu bar 1`` can fail with
    "Invalid index" even while the menu is visibly present on screen.
    """
    script = (
        f'tell application "{SOUNDMINER_APP}" to activate\n'
        f'tell application "System Events"\n'
        f'    set menuReady to false\n'
        f'    repeat 40 times\n'
        f'        if exists process "{SOUNDMINER_APP}" then\n'
        f'            tell process "{SOUNDMINER_APP}"\n'
        f'                set frontmost to true\n'
        f'                if exists menu bar 1 then\n'
        f'                    set menuReady to true\n'
        f'                    exit repeat\n'
        f'                end if\n'
        f'            end tell\n'
        f'        end if\n'
        f'        delay 0.25\n'
        f'    end repeat\n'
        f'    if not menuReady then error "Soundminer menu bar did not become available through Accessibility"\n'
        f'    tell process "{SOUNDMINER_APP}"\n'
        f'        set frontmost to true\n'
        f'        tell menu bar item "{menu_title}" of menu bar 1\n'
        f'            click\n'
        f'            delay 0.25\n'
        f'            if exists menu item "{item_title}" of menu 1 then\n'
        f'                click menu item "{item_title}" of menu 1\n'
        f'            else\n'
        f'                set availableItems to name of every menu item of menu 1\n'
        f'                error "Requested item not found. Available Database items: " & (availableItems as text)\n'
        f'            end if\n'
        f'        end tell\n'
        f'    end tell\n'
        f'end tell'
    )
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise _SoundminerError(
            f"Menu click failed: {menu_title} → {item_title}\n"
            f"  osascript stderr: {result.stderr.strip()}\n"
            f"  Common causes:\n"
            f"  - Terminal not granted Accessibility access (System Settings\n"
            f"    → Privacy & Security → Accessibility).\n"
            f"  - The menu item label has changed in Soundminer's UI.\n"
            f"  - Soundminer wasn't running."
        )
    logger.debug(f"  Menu: {menu_title} → {item_title}")


def _dialog_accessibility_snapshot(logger: logging.Logger) -> str:
    """Return visible Soundminer window/sheet names and static text."""
    script = (
        f'tell application "System Events"\n'
        f'  tell process "{SOUNDMINER_APP}"\n'
        f'    set snapshot to ""\n'
        f'    repeat with w in windows\n'
        f'      try\n'
        f'        set snapshot to snapshot & (name of w as text) & " | "\n'
        f'      end try\n'
        f'      try\n'
        f'        repeat with t in static texts of w\n'
        f'          set snapshot to snapshot & (value of t as text) & " | "\n'
        f'        end repeat\n'
        f'      end try\n'
        f'      try\n'
        f'        repeat with s in sheets of w\n'
        f'          repeat with t in static texts of s\n'
        f'            set snapshot to snapshot & (value of t as text) & " | "\n'
        f'          end repeat\n'
        f'        end repeat\n'
        f'      end try\n'
        f'    end repeat\n'
        f'    return snapshot\n'
        f'  end tell\n'
        f'end tell'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        logger.debug(f"        (dialog Accessibility snapshot failed: {exc})")
        return ""
    return result.stdout.strip()


def _click_known_dialog_ok(logger: logging.Logger, label: str) -> bool:
    """Click OK only after Python has validated the visible dialog text."""
    script = (
        f'tell application "System Events"\n'
        f'  tell process "{SOUNDMINER_APP}"\n'
        f'    repeat with w in windows\n'
        f'      repeat with theName in {{"OK", "Continue", "Yes"}}\n'
        f'        try\n'
        f'          if exists button (theName as string) of w then\n'
        f'            click button (theName as string) of w\n'
        f'            return "clicked"\n'
        f'          end if\n'
        f'        end try\n'
        f'        try\n'
        f'          repeat with s in sheets of w\n'
        f'            if exists button (theName as string) of s then\n'
        f'              click button (theName as string) of s\n'
        f'              return "clicked"\n'
        f'            end if\n'
        f'          end repeat\n'
        f'        end try\n'
        f'      end repeat\n'
        f'    end repeat\n'
        f'  end tell\n'
        f'end tell\n'
        f'return "none"'
    )
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=10,
    )
    if result.stdout.strip() == "clicked":
        logger.info(f"        ↳ Accepted audited Soundminer dialog: {label}.")
        time.sleep(0.6)
        _save_step_screenshot("dialog_ok_clicked", logger)
        return True
    return False


def _validate_unmatched_dialog_text(snapshot: str) -> set[str]:
    """Return audited unmatched fields or raise for new/unreadable fields."""
    observed = {field for field in ALLOWED_UNMATCHED_FIELDS if field in snapshot}
    match = re.search(
        r"headers aren't in the database:\s*(.*?)\s*If you expected",
        snapshot,
        flags=re.IGNORECASE,
    )
    listed: set[str] = set()
    if match:
        listed = {
            value.strip().strip(".,|")
            for value in match.group(1).split(",") if value.strip()
        }
    unexpected = listed - ALLOWED_UNMATCHED_FIELDS
    fields = listed or observed
    if unexpected or not fields:
        raise _SoundminerError(
            "Unmatched Fields dialog contains an unaudited or unreadable "
            f"field set. Observed={sorted(fields)}; "
            f"allowed={sorted(ALLOWED_UNMATCHED_FIELDS)}."
        )
    return fields


def _dismiss_known_dialog_applescript(logger: logging.Logger) -> bool:
    snapshot = _dialog_accessibility_snapshot(logger)
    if not snapshot:
        return False
    if "Unmatched Fields" in snapshot:
        fields = _validate_unmatched_dialog_text(snapshot)
        logger.warning(
            "        Audited unmatched fields will not be imported: "
            + ", ".join(sorted(fields))
        )
        return _click_known_dialog_ok(logger, "Unmatched Fields")
    if "Check for Dupes Warning" in snapshot:
        return _click_known_dialog_ok(logger, "Check for Dupes Warning")
    # Do not click a generic OK/Yes button in an unknown modal.
    return False


def _dismiss_import_dialogs_once(logger: logging.Logger) -> bool:
    """
    One pass at clearing the blocking dialogs that gate an import / scan:
    the "Unmatched Fields" and "Check for Dupes Warning" prompts.  Tries the
    precise image-match dismissers first, then the AppleScript OK-clicker as
    a HiDPI-proof fallback.  Returns True if anything was dismissed.
    """
    hit = _dismiss_known_dialog_applescript(logger)
    # If Accessibility cannot expose the unmatched text, fail instead of
    # accepting fields we could not audit. The crop is only a presence signal.
    unmatched_crop = OPTIONAL_DIALOG_SCREENSHOTS.get("unmatched_fields")
    if (
        not hit and unmatched_crop
        and Path(_img(unmatched_crop)).exists()
        and _locate_safe(_img(unmatched_crop)) is not None
    ):
        raise _SoundminerError(
            "Unmatched Fields dialog is visible but its text is unavailable "
            "through Accessibility; refusing to accept it blindly."
        )
    if _dismiss_dialog_if_present("dupes_warning", "Check for Dupes Warning", logger):
        hit = True
    return hit


def _dismiss_dialog_if_present(
    crop_key:  str,
    label:     str,
    logger:    logging.Logger,
) -> bool:
    """
    If the dialog identified by OPTIONAL_DIALOG_SCREENSHOTS[crop_key] is on
    screen, dismiss it by pressing Return (the default button on these
    "press OK to continue" alerts).  Returns True if the dialog was found
    and dismissed.  Best-effort: a missing crop or a no-match returns False
    without raising, so the operator can still handle it manually.
    """
    import pyautogui

    fn = OPTIONAL_DIALOG_SCREENSHOTS.get(crop_key)
    if not fn:
        return False
    crop = _img(fn)
    if not Path(crop).exists():
        return False  # crop not created yet — operator handles manually
    loc = _locate_safe(crop)
    if loc is None:
        return False
    logger.info(f"        ↳ Dismissing dialog: {label} (OK).")
    # These are standard alerts whose default button is OK → Return triggers it.
    pyautogui.press("enter")
    time.sleep(0.6)
    _save_step_screenshot(f"dialog_{crop_key}_dismissed", logger)
    return True


def _watch_and_dismiss_import_dialogs(
    logger:        logging.Logger,
    watch_seconds: float = 45.0,
) -> bool:
    """
    When "Import text into database" runs with check-duplicates enabled, and
    when the CSV has headers the DB doesn't know, Soundminer raises two
    notifications that BLOCK the import until acknowledged:
      • "Unmatched Fields" — CSV headers not in the database  → OK
      • "Check for Dupes Warning" — confirm duplicate checking → OK
    Both have OK as the default (highlighted) button, so Return dismisses
    them.  They appear right after the CSV/audio panels are confirmed and
    before the "Importing Text" progress bar, so we poll for a short window
    and click OK as each appears.  Anything not auto-handled is dismissed by
    the operator during the manual handshake that follows.
    """
    end = time.monotonic() + watch_seconds
    seen = 0
    start_observed = False
    while time.monotonic() < end:
        # Use the same complete dismissal pass as the later unattended poll.
        # This includes the Accessibility-based OK click when image matching
        # misses because of display scaling.  Previously this initial watcher
        # tried only the screenshots, leaving both alerts blocked until the
        # later wait loop (or an operator) cleared them.
        hit = _dismiss_import_dialogs_once(logger)
        if hit:
            seen += 1
            start_observed = True
        # Once the progress bar is up, the gating dialogs are done — stop early.
        if not hit and _locate_safe(_img(OPTIONAL_DIALOG_SCREENSHOTS["importing_text"])) is not None:
            logger.info("        Import progress bar visible — dialogs cleared.")
            start_observed = True
            break
        time.sleep(0.5 if hit else 2.0)
    if seen:
        logger.info(f"        Auto-dismissed {seen} import dialog(s).")
    return start_observed


def _soundminer_log_window_visible(logger: logging.Logger) -> bool:
    """Return True when Soundminer's scan/import failure log is open."""
    script = (
        f'tell application "System Events"\n'
        f'  tell process "{SOUNDMINER_APP}"\n'
        f'    repeat with w in windows\n'
        f'      try\n'
        f'        if name of w contains "Soundminer Log Window" then '
        f'return "visible"\n'
        f'      end try\n'
        f'    end repeat\n'
        f'  end tell\n'
        f'end tell\n'
        f'return "none"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.stdout.strip() == "visible":
            return True
    except Exception as exc:
        logger.debug(f"        (log-window Accessibility check failed: {exc})")

    fn = OPTIONAL_DIALOG_SCREENSHOTS.get("log_window")
    return bool(
        fn
        and Path(_img(fn)).exists()
        and _locate_safe(_img(fn)) is not None
    )


def _raise_if_soundminer_log_window(
    logger: logging.Logger,
    *,
    phase: str,
) -> bool:
    """Fail closed when Soundminer reports any scan/import problem.

    The caller deliberately does not close the log.  It remains visible for
    diagnosis and the outer workflow captures a failure screenshot before
    returning a non-zero status.
    """
    if not _soundminer_log_window_visible(logger):
        return False
    _save_step_screenshot(f"{phase}_scan_failure_log", logger)
    raise _SoundminerError(
        f"Soundminer Log Window appeared during {phase}; processing stopped "
        f"before the next phase. Review the visible log and the captured "
        f"failure screenshot to identify the missing or invalid CSV row."
    )


def _dismiss_alert_if_present(
    logger:         logging.Logger,
    default_action: str = "Enter",
) -> None:
    """
    Best-effort dismissal of a modal alert that may have appeared.
    Pressing Enter triggers the default button in macOS standard alerts;
    Escape cancels.  For destructive confirmations like "Delete all
    records?" the default is the destructive action, which is what we
    want (we just asked for it).
    """
    import pyautogui

    if default_action.lower() == "enter":
        pyautogui.press("enter")
    elif default_action.lower() == "escape":
        pyautogui.press("escape")
    else:
        raise ValueError(f"unsupported default_action: {default_action}")


def _open_panel_go_to_path(
    path:   str,
    logger: logging.Logger,
    *,
    select_directory: bool = False,
) -> None:
    """
    Inside an open macOS NSOpenPanel, navigate to `path` via Cmd+Shift+G.

    File selection pastes the complete file path, then confirms it. Directory
    selection instead navigates to the parent and type-selects the directory
    itself before confirming. Pasting a directory path directly navigates
    *inside* it, where Soundminer's folder picker leaves the Open button
    disabled because no folder row is selected.

    Same shape as unisync_automation._open_panel_go_to_path() — see that
    function for the rationale on each step (clipboard vs typing, double
    Enter, etc.).  Copied here rather than imported to keep this module
    independent of UniSync.
    """
    import pyautogui

    # Let the open panel animate in and gain focus
    time.sleep(max(DIALOG_OPEN_WAIT, 2.5))
    _save_step_screenshot("dlg_01_open", logger)

    # Cmd+Shift+G opens the Go-to-Folder sheet
    pyautogui.hotkey("command", "shift", "g")
    time.sleep(1.2)
    _save_step_screenshot("dlg_02_after_cmd_shift_g", logger)

    # Clear any pre-filled text
    pyautogui.hotkey("command", "a")
    time.sleep(0.2)

    target = Path(path)
    navigation_path = str(target.parent if select_directory else target)

    # Deliver the navigation path via clipboard paste (immune to keyboard-layout drift)
    if _set_clipboard(navigation_path, logger):
        time.sleep(0.15)
        pyautogui.hotkey("command", "v")
    else:
        logger.warning(
            "    Clipboard unavailable — falling back to typing the path "
            "(special characters may be unreliable)."
        )
        pyautogui.write(navigation_path, interval=0.04)
    time.sleep(0.5)
    _save_step_screenshot("dlg_03_after_paste", logger)

    # First Enter — navigate to pasted path
    pyautogui.press("enter")
    time.sleep(0.9)
    _save_step_screenshot("dlg_04_after_first_enter", logger)

    if select_directory:
        # The list has focus after Go-to-Folder closes. Type-select the exact
        # child folder so Open becomes enabled, then confirm the selection.
        pyautogui.write(target.name, interval=0.04)
        time.sleep(0.7)
        _save_step_screenshot("dlg_05_directory_selected", logger)

    # Second Enter — click Open / confirm the file or selected directory.
    pyautogui.press("enter")
    time.sleep(0.9)
    _save_step_screenshot("dlg_05_after_second_enter", logger)

    logger.debug(
        f"    NSOpenPanel → {path}"
        + (" (directory selected from parent)" if select_directory else "")
    )


def _set_clipboard(text: str, logger: logging.Logger) -> bool:
    """Set the macOS clipboard via pbcopy. Returns False on failure."""
    try:
        proc = subprocess.Popen(
            ["pbcopy"], stdin=subprocess.PIPE, text=True,
        )
        proc.communicate(input=text)
        return proc.returncode == 0
    except Exception as exc:
        logger.warning(f"    pbcopy failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Manual handshake for operations we can't programmatically detect
# ---------------------------------------------------------------------------

def _screen_fingerprint():
    """
    Small grayscale snapshot of the whole screen, for change-detection.

    Downscaled hard so it's cheap to capture and compare and so trivial
    sub-pixel noise doesn't register as movement.  Returned as an int16 array
    (not uint8) so pixel subtraction can't wrap around.
    """
    import numpy as np
    import pyautogui
    img = pyautogui.screenshot().convert("L").resize((80, 45))
    return np.asarray(img, dtype="int16")


def _ioreg_reports_locked(output: str) -> bool:
    """Parse macOS IORegistry console-session output without GUI imports."""
    return bool(re.search(r'CGSSessionScreenIsLocked["\s=]+Yes', output))


def _assert_soundminer_gui_available(
    logger: logging.Logger,
    *,
    require_window: bool,
) -> None:
    """Fail closed when the Aqua console cannot currently be automated.

    A locked HDF1 session produces a valid-looking screenshot containing only
    the macOS wallpaper.  Checking the console state separately prevents that
    wallpaper (especially a dynamic one) from masquerading as Soundminer
    progress.  Once Soundminer has been activated, also require at least one
    application window so an app crash or unexpected quit is reported at once.
    """
    try:
        result = subprocess.run(
            ["/usr/sbin/ioreg", "-n", "Root", "-d1"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and _ioreg_reports_locked(result.stdout):
            raise _SoundminerError(
                "The HDF1 macOS console became locked during Soundminer "
                "automation. Unlock the hdfuser session before resuming; "
                "screen capture and dialog handling are unavailable while "
                "the session is locked."
            )
    except _SoundminerError:
        raise
    except Exception as exc:
        # The lock probe is an additional macOS guard.  Existing screenshot
        # and Accessibility checks remain authoritative if ioreg itself is
        # unavailable on a future host.
        logger.debug(f"        (console-lock probe failed: {exc})")

    if not require_window:
        return

    script = (
        'tell application "System Events"\n'
        f'  if not (exists process "{SOUNDMINER_APP}") then return "missing"\n'
        f'  tell process "{SOUNDMINER_APP}"\n'
        '    return ((count of windows) as text) & "|" & '
        '((count of menu bars) as text) & "|" & (frontmost as text)\n'
        '  end tell\n'
        'end tell'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.debug(
                "        (Soundminer window probe unavailable: %s)",
                result.stderr.strip(),
            )
            return
        state = result.stdout.strip().lower()
        if state == "missing":
            raise _SoundminerError(
                "Soundminer v5Pro exited during GUI automation. Review the "
                "Soundminer/macOS crash report before resuming."
            )
        parts = state.split("|")
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            logger.debug("        (unexpected Soundminer window probe: %r)", state)
            return
        window_count = int(parts[0])
        menu_count = int(parts[1])
        frontmost = parts[2] == "true"
        # During Embed Metadata (and some other modal progress operations),
        # Soundminer 5Pro temporarily exposes zero AX windows even though its
        # main window and progress sheet are visibly present.  Its frontmost
        # process and accessible menu bar remain reliable in that state.
        if window_count < 1 and menu_count >= 1 and frontmost:
            logger.debug(
                "        (Soundminer modal progress state: no AX window, "
                "frontmost menu bar present)"
            )
            return
        if window_count < 1:
            raise _SoundminerError(
                "Soundminer v5Pro is running but has no application window. "
                "Restore or relaunch it before resuming."
            )
    except _SoundminerError:
        raise
    except Exception as exc:
        logger.debug(f"        (Soundminer window probe failed: {exc})")


def _wait_for_screen_idle(
    *,
    phase_label:          str,
    stability:            int,
    hard_timeout:         int,
    no_activity_fallback: int,
    logger:               logging.Logger,
    on_poll:              "Optional[Callable[[], bool]]" = None,
    initial_activity:     bool = False,
    minimum_runtime:      int = 0,
) -> None:
    """
    Block until the Soundminer UI stops changing — the automated stand-in for
    an operator watching the status bar settle and then pressing Enter.

    While a phase (scan / import / embed) runs, Soundminer animates a progress
    bar / status text, so the screen fingerprint keeps changing.  When the
    phase finishes the picture goes static; once it's been static for
    ``stability`` seconds we treat the phase as complete.

    ``on_poll`` is an optional callback run once per poll (used to dismiss the
    blocking dupes / unmatched-fields dialogs during an import or scan).  If it
    reports it did something (returns True), that counts as activity — so a
    modal that we just cleared can't be mistaken for "the phase finished."

    Safeguards:
      • We only accept "idle ⇒ done" AFTER we've seen the screen change at
        least once (``saw_activity``) — so a brief static moment right after
        launching the phase, or a modal dialog sitting there BEFORE the work
        starts, can't be mistaken for completion.
      • If we never see any visible activity, the soft ceiling is a hard
        failure.  Advancing without positive evidence previously allowed a
        blocked dialog or failed menu action to masquerade as completion.
      • ``hard_timeout`` is an absolute safety ceiling.
    """
    import numpy as np

    logger.info(
        f"        Waiting for {phase_label} to finish — watching the Soundminer "
        f"UI settle (no Enter needed)…"
    )
    start        = time.monotonic()
    last_fp      = None
    last_change  = start
    # The import/scan dialog watcher runs before this generic idle loop.
    # Preserve its positive start signal so a short operation that finishes
    # inside that watcher is not later misclassified as "never started."
    saw_activity = initial_activity
    next_log     = start + PROGRESS_DOT_INTERVAL
    next_gui_guard = start

    while True:
        now     = time.monotonic()
        elapsed = now - start

        if now >= next_gui_guard:
            _assert_soundminer_gui_available(logger, require_window=True)
            next_gui_guard = now + 10

        if elapsed > hard_timeout:
            raise _SoundminerError(
                f"{phase_label} exceeded the hard timeout of {hard_timeout}s "
                f"with the UI never settling.  Check Soundminer for a stalled "
                f"job or an error dialog."
            )

        # Clear any blocking modal (dupes / unmatched fields) so the phase can
        # actually run; a dismissal counts as activity so we keep waiting for
        # the REAL completion rather than treating the frozen dialog as "done".
        if on_poll is not None:
            try:
                if on_poll():
                    last_change  = now
                    saw_activity = True
            except _SoundminerError:
                # Phase guards use this to stop immediately on a visible
                # Soundminer scan/import failure log.  Never downgrade that
                # correctness failure to a debug message and continue.
                raise
            except Exception as exc:
                logger.debug(f"        (dialog dismiss on_poll failed: {exc})")

        try:
            fp = _screen_fingerprint()
            if last_fp is not None:
                diff = float(np.mean(np.abs(fp - last_fp)))
                if diff > SCREEN_IDLE_DIFF:
                    last_change  = now
                    saw_activity = True
            last_fp = fp
        except Exception as exc:
            logger.debug(f"        (idle-detect snapshot failed: {exc})")

        idle_for = now - last_change

        # Normal completion: we saw the phase run, and it's now been still.
        if (
            saw_activity
            and idle_for >= stability
            and elapsed >= minimum_runtime
        ):
            logger.info(
                f"        {phase_label} finished — UI idle {int(stability)}s "
                f"({int(elapsed)}s total)."
            )
            return

        # Fail closed: no visible progress means there is no positive evidence
        # the requested phase ever started.
        if not saw_activity and elapsed >= no_activity_fallback:
            raise _SoundminerError(
                f"{phase_label} showed no detectable UI activity for "
                f"{int(elapsed)}s. Refusing to advance without a positive "
                "start/completion signal; check for a blocked dialog, stale "
                "menu command, or Screen Recording failure."
            )
            return

        if now >= next_log:
            if saw_activity and elapsed < minimum_runtime:
                state = (
                    f"minimum runtime {int(elapsed)}s/"
                    f"{int(minimum_runtime)}s"
                )
            else:
                state = (
                    f"UI idle {int(idle_for)}s/{int(stability)}s"
                    if saw_activity
                    else f"awaiting first activity ({int(elapsed)}s)"
                )
            logger.info(f"        … {phase_label} running ({int(elapsed)}s; {state})")
            next_log = now + PROGRESS_DOT_INTERVAL

        time.sleep(SCREEN_IDLE_POLL)


def _focus_record_list(logger: logging.Logger) -> None:
    """Put keyboard focus inside Soundminer's central record grid.

    Activating the app does not imply grid focus: after a relaunch Soundminer
    restores focus to Search Database, where Command-A merely selects the
    search text and leaves zero records selected. The grid occupies a stable
    central region on both supported HDF1 display modes; verify that the point
    is inside the active Soundminer window before clicking it.
    """
    import pyautogui

    _assert_soundminer_gui_available(logger, require_window=True)
    sw, sh = pyautogui.size()
    x, y = int(sw * 0.35), int(sh * 0.30)
    logger.debug(f"        Focusing Soundminer record grid at ({x}, {y}).")
    pyautogui.click(x, y)
    time.sleep(0.4)


def _select_all_records(logger: logging.Logger) -> None:
    """
    ⌘A in the Soundminer record list so the Mirror operates on EVERY record,
    not just whatever row happened to be selected after a scan or import.

    Mirrors the focus-then-keystroke pattern used by _select_all_and_embed:
    bring Soundminer to the front, click inside the record grid, then send ⌘A.
    Called immediately before opening the Mirror Settings dialog in both the
    NBC and SourceAudio flows.
    """
    import pyautogui
    logger.info("  Selecting all records (⌘A) before Mirror…")
    _activate_soundminer(logger)
    _focus_record_list(logger)
    pyautogui.hotkey("command", "a")
    time.sleep(0.8)
    _save_step_screenshot("select_all_before_mirror", logger)


def _wait_with_manual_handshake(
    *,
    phase_label:  str,
    soft_minutes: int,
    hard_timeout: int,
    unattended:   bool,
    logger:       logging.Logger,
    on_poll:      "Optional[Callable[[], bool]]" = None,
    initial_activity: bool = False,
    minimum_runtime: int = 0,
) -> None:
    """
    Block for a phase whose completion we can't poll (scan, import, embed).

    Behaviour:
      unattended=True  → watch the Soundminer UI settle and return when it's
                         been idle (the automated equivalent of the operator
                         watching the status bar settle), clearing any blocking
                         dialogs via ``on_poll`` while it waits.
      unattended=False → log progress dots, then prompt the operator to
                         press Enter when the phase completes.

    `on_poll` (unattended only) runs once per poll — used to auto-OK the
    dupes / unmatched-fields dialogs that gate an import or scan.

    `hard_timeout` is a safety ceiling — even in unattended mode we won't
    block longer than this; if hit, _SoundminerError is raised.
    """
    start = time.monotonic()
    if unattended:
        # Automated equivalent of "watch the status bar settle, then Enter":
        # wait until the Soundminer UI first changes and then settles. A phase
        # that never shows activity fails closed at the soft ceiling.
        _wait_for_screen_idle(
            phase_label          = phase_label,
            stability            = SCREEN_IDLE_STABILITY,
            hard_timeout         = hard_timeout,
            no_activity_fallback = soft_minutes * 60,
            logger               = logger,
            on_poll              = on_poll,
            initial_activity     = initial_activity,
            minimum_runtime      = minimum_runtime,
        )
        return
    else:
        # Interactive — print a clear handshake prompt
        print("")
        print(
            f"  ╔══════════════════════════════════════════════════════════════════╗"
        )
        print(
            f"  ║  Watch Soundminer until {phase_label.upper():<6} completes "
            f"(status bar settles).{' ' * (10 - max(0, len(phase_label) - 6))}║"
        )
        print(
            f"  ║  THEN press ENTER in this terminal to continue.                  ║"
        )
        print(
            f"  ║  Ctrl+C to abort.                                                ║"
        )
        print(
            f"  ╚══════════════════════════════════════════════════════════════════╝"
        )
        try:
            input(f"  >>> Press ENTER when {phase_label} is complete: ")
        except EOFError:
            raise _SoundminerError(
                f"{phase_label} requires manual confirmation in an "
                f"interactive terminal, or pass --unattended."
            )
        elapsed = time.monotonic() - start
        logger.info(f"        {phase_label} confirmed by operator after {int(elapsed)}s.")


# ---------------------------------------------------------------------------
# Screenshot helpers
# ---------------------------------------------------------------------------

def _img(filename: str) -> str:
    """Return absolute path string for a reference screenshot."""
    return str(SCREENSHOTS_DIR / filename)


def _locate_safe(image_path: str):
    """
    Wrap pyautogui.locateOnScreen so misses return None instead of raising
    (which newer pyautogui versions do).
    """
    import pyautogui
    try:
        return pyautogui.locateOnScreen(image_path, confidence=LOCATE_CONFIDENCE)
    except pyautogui.ImageNotFoundException:
        return None
    except Exception:
        return None


def _wait_for_image(
    image_key: str,
    timeout:   float,
    logger:    logging.Logger,
) -> bool:
    """
    Wait up to `timeout` seconds for the image referenced by `image_key`
    to appear on screen.  Returns True if found, False if timed out.

    `image_key` should be one of the filenames in REQUIRED_SCREENSHOTS.
    """
    target = _img(image_key)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _locate_safe(target) is not None:
            return True
        time.sleep(LOCATE_DELAY)
    return False


def _save_step_screenshot(label: str, logger: logging.Logger) -> None:
    """
    When CAPTURE_STEPS is on, save a full-screen PNG to DEBUG_STEP_DIR
    so step-by-step issues can be diagnosed after the fact.  Uses macOS
    /usr/sbin/screencapture rather than pyautogui to avoid permissions
    issues that sometimes prevent pyautogui from capturing while the
    target app is frontmost.
    """
    if not CAPTURE_STEPS:
        return
    try:
        ts = datetime.now().strftime("%H%M%S")
        path = DEBUG_STEP_DIR / f"step_{ts}_{label}.png"
        DEBUG_STEP_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["/usr/sbin/screencapture", "-x", str(path)],
            capture_output=True,
        )
    except Exception as exc:
        logger.warning(
            f"    Could not save step screenshot: "
            f"{type(exc).__name__}: {exc}"
        )


def _capture_failure_screenshot(label: str, logger: logging.Logger) -> None:
    """Full-screen capture for diagnostics on any failure path."""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = FAILURE_SCREENSHOTS_DIR / f"{label}_{ts}.png"
        FAILURE_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["/usr/sbin/screencapture", "-x", str(path)],
            capture_output=True,
        )
        logger.info(f"  📸  Failure screenshot saved: {path}")
    except Exception as exc:
        logger.warning(f"  Could not capture failure screenshot: {exc}")


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def _run_cli(argv: Optional[list[str]] = None) -> int:
    import argparse, sys

    p = argparse.ArgumentParser(
        description=(
            "Drive Soundminer v5Pro for the release's Soundminer steps.  With "
            "no step flag it runs BOTH Step 11 (SourceAudio scan + AIFF mirror) "
            "and Step 12 (NBC embed + mirror), in that order, exactly as the "
            "full workflow does on the Soundminer machine.  Pass --sourceaudio "
            "or --nbc to run just one.  Runs UNATTENDED by default (no Enter "
            "prompts); use --attended for a supervised first run.  --dry-run "
            "previews without touching the UI."
        ),
    )
    # Which step(s) to run — symmetric flags; none given ⇒ both, chained.
    p.add_argument("--sourceaudio", action="store_true",
                   help="Run only Step 11 (SourceAudio scan + AIFF mirror).")
    p.add_argument("--nbc", action="store_true",
                   help="Run only Step 12 (NBC embed + mirror).")

    p.add_argument("--sourceaudio-db-shortcut", default="8", metavar="KEY",
                   help="Database shortcut digit for the SourceAudio DB "
                        "(default '8' = ⌘8). Only used for Step 11.")
    p.add_argument("--previous-month", action="store_true",
                   help="Full-month (previous-month) run.  --year/--month "
                        "optional: omit both to target the month before today, "
                        "or pass both to pin the reference month.")
    p.add_argument("--year",  type=int, default=None)
    p.add_argument("--month", type=int, default=None)
    p.add_argument("--part",  type=int, choices=[1, 2], default=None)
    p.add_argument("--start-date")
    p.add_argument("--end-date")
    p.add_argument("--full-month-content", action="store_true")
    p.add_argument(
        "--specials-dir-override", default=None, metavar="PATH",
        help="Recovery-only: use an existing Specials release root whose "
             "internal content period differs from the client label.",
    )
    p.add_argument(
        "--client-label-override", default=None, metavar="LABEL",
        help="Recovery-only client folder label used with "
             "--specials-dir-override.",
    )
    p.add_argument(
        "--nbc-metadata-override", default=None, metavar="CSV",
        help="Recovery-only NBC metadata CSV used with a transitioned release.",
    )

    p.add_argument("--dry-run", action="store_true",
                   help="Log the plan and exit without touching the UI.")
    p.add_argument("--skip-soundminer", action="store_true",
                   help="No-op for parity with the orchestrator's flag.")

    p.add_argument("--attended", action="store_true",
                   help="Run ATTENDED: pause for Enter after each scan/import/"
                        "embed and to review the automatically applied Mirror "
                        "Settings before OK. Default is fully unattended.")
    p.add_argument("--unattended", action="store_true",
                   help="Deprecated / no-op: unattended is the default now. "
                        "Kept for backward compatibility (use --attended to "
                        "force the supervised prompts).")
    # Step 12 phase skips (restart/recovery)
    p.add_argument("--skip-delete-records", action="store_true",
                   help="Step 12: skip 12.3 — assume the DB is already empty.")
    p.add_argument("--skip-import", action="store_true",
                   help="Step 12: skip 12.4 — assume metadata is already imported.")
    p.add_argument("--skip-embed", action="store_true",
                   help="Step 12: skip 12.5 — assume metadata is already embedded.")
    p.add_argument("--skip-mirror", action="store_true",
                   help="Step 12: skip 12.6 / 12.7 — stop after embed.")
    p.add_argument(
        "--restart-app",
        action="store_true",
        help=(
            "Recovery-only: gracefully quit and relaunch Soundminer before "
            "the selected workflow. Never force-kills the app."
        ),
    )
    p.add_argument("--capture-steps", action="store_true",
                   help="Save numbered step screenshots to "
                        f"{DEBUG_STEP_DIR}.")
    p.add_argument("--resume", action="store_true",
                   help="Resume from the last validated phase checkpoint for "
                        "this release. Destination manifests are revalidated "
                        "before any phase is skipped.")
    p.add_argument("--preflight-only", action="store_true",
                   help="Run only the non-destructive GUI/crop/permission "
                        "preflight and exit.")
    p.add_argument("--debug", action="store_true")

    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("soundminer")

    if args.skip_soundminer:
        logger.info("--skip-soundminer set; nothing to do.")
        return 0

    # Apply --capture-steps to module global
    global CAPTURE_STEPS
    CAPTURE_STEPS = bool(args.capture_steps)

    from config import context_from_cli_args
    try:
        ctx = context_from_cli_args(args)
    except ValueError as e:
        logger.error(f"  ✗  {e}")
        return 2
    if args.specials_dir_override:
        from config import SPECIALS_BASE

        override = Path(args.specials_dir_override).expanduser().resolve()
        if override.parent != SPECIALS_BASE.resolve() or not override.name.startswith("UPM-"):
            logger.error(
                "  ✗  --specials-dir-override must be one UPM release folder "
                f"directly under {SPECIALS_BASE}."
            )
            return 2
        ctx.specials_dir = override
    if args.client_label_override:
        label = args.client_label_override.strip()
        if not label or "/" in label or "\\" in label:
            logger.error("  ✗  Invalid --client-label-override.")
            return 2
        ctx.client_delivery_label = label
    if args.specials_dir_override or args.client_label_override:
        ctx.partner_dirs = ctx._build_partner_dirs()
    if args.nbc_metadata_override:
        metadata_override = Path(args.nbc_metadata_override).expanduser().resolve()
        if (
            metadata_override.suffix.casefold() != ".csv"
            or ctx.specials_dir.resolve() not in metadata_override.parents
        ):
            logger.error(
                "  ✗  --nbc-metadata-override must be a CSV inside the "
                "selected Specials release folder."
            )
            return 2
        ctx.nbc_metadata_csv = metadata_override
    logger.info(f"Release context: {ctx}")

    if args.restart_app:
        try:
            _restart_soundminer(logger)
        except _SoundminerError as exc:
            logger.error(f"  ✗ Soundminer recovery restart failed: {exc}")
            return 1

    if args.preflight_only:
        return 0 if run_soundminer_gui_preflight(logger) else 1

    # Unattended is the default; --attended opts into the supervised prompts.
    unattended = not args.attended

    # Decide which steps to run.  Neither flag ⇒ both (11 then 12), chained.
    run_sa  = args.sourceaudio or not (args.sourceaudio or args.nbc)
    run_nbc = args.nbc         or not (args.sourceaudio or args.nbc)

    if run_sa and run_nbc:
        logger.info(
            "Running BOTH Soundminer steps in sequence: Step 11 (SourceAudio) "
            "→ Step 12 (NBC).  No hand-off, no prompts (unattended)."
            if unattended else
            "Running BOTH Soundminer steps in sequence: Step 11 (SourceAudio) "
            "→ Step 12 (NBC), attended."
        )

    overall_ok = True

    if run_sa:
        ok_sa = run_soundminer_sourceaudio_workflow(
            ctx,
            dry_run=args.dry_run,
            logger=logger,
            unattended=unattended,
            db_shortcut=args.sourceaudio_db_shortcut,
            resume=args.resume,
        )
        overall_ok = overall_ok and ok_sa
        if not ok_sa and run_nbc:
            logger.error(
                "  ✗  Step 11 (SourceAudio) failed — not continuing to Step 12. "
                "Fix the cause and re-run (add --nbc to run only Step 12 once "
                "Step 11 is done)."
            )
            return 1

    if run_nbc:
        ok_nbc = run_soundminer_nbc_workflow(
            ctx,
            dry_run=args.dry_run,
            logger=logger,
            unattended=unattended,
            skip_delete_records=args.skip_delete_records,
            skip_import=args.skip_import,
            skip_embed=args.skip_embed,
            skip_mirror=args.skip_mirror,
            resume=args.resume,
        )
        overall_ok = overall_ok and ok_nbc

    return 0 if overall_ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_run_cli())
