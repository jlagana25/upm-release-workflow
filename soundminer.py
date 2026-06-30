"""
soundminer.py — Step 12: Soundminer v5Pro NBC Workflow
========================================================
Drives Soundminer v5Pro through the NBC embed + mirror pipeline.

Substeps (matching the workflow spec):
  12.1  The NBC Metadata CSV is exported in Step 1 (domo_exports) and lands
        at ctx.nbc_metadata_csv.  We assume it exists by the time this
        module runs.

  12.2  Launch / activate Soundminer v5Pro and switch the toolbar database
        dropdown to "NBCUniversal" via keyboard shortcut ⌘5 (visible in the
        Soundminer_nbc.png reference: NBCUniversal has shortcut "⌘5").

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
        retains these settings between runs, so the dialog should already
        be configured.  By default we pause for human verification of the
        dialog contents (saved as a screenshot to the failure-log folder
        for the operator to inspect).  In --unattended mode the pause is
        skipped and Enter is sent to accept whatever Soundminer currently
        shows.

        Required settings (per Soundminer_mirror.png reference):
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

import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import ReleaseContext

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SOUNDMINER_APP = "Soundminer v5Pro"

# Paths are derived from this script's own location rather than hardcoded,
# so the module works wherever the repo lives — critical here because it
# runs on the REMOTE Soundminer Mac, whose layout
# (/Volumes/hdfuser/Documents/Scripts/Python/…) differs from the pipeline
# machine's.  soundminer.py sits at <repo>/files/soundminer.py, so the repo
# root is two parents up and the screenshots folder is its sibling.
_REPO_ROOT = Path(__file__).resolve().parent.parent

SCREENSHOTS_DIR = _REPO_ROOT / "screenshots"

# Diagnostics live under the repo too, so they're writable on whichever
# machine runs this and easy to find next to the code.
DEBUG_STEP_DIR = _REPO_ROOT / "_logs" / "soundminer_debug_steps"

FAILURE_SCREENSHOTS_DIR = _REPO_ROOT / "_logs" / "soundminer_failures"

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
MIRROR_STABILITY_WINDOW = 180      # seconds with no new files ⇒ mirror is done
MIRROR_STARTUP_GRACE    = 600      # seconds to see the FIRST output file before
                                   # concluding the mirror never started

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


class _SoundminerError(RuntimeError):
    """Raised when a Soundminer UI step doesn't reach its expected state."""


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
            f"  Create it and add the four PNG crops listed in the module docstring."
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
            "\n  Crop the missing screenshots from the Soundminer reference images\n"
            "  (Soundminer_nbc.png, Soundminer_dbselect.png, Soundminer_mirror.png)\n"
            f"  and save them to:\n  {SCREENSHOTS_DIR}"
        )
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
    manual_verify_mirror_settings:  Optional[bool] = None,
) -> bool:
    """
    Drive Soundminer v5Pro through the NBC embed + mirror workflow end-to-end.

    Parameters
    ----------
    ctx                  : Release context (paths, dates).
    dry_run              : Log the plan and return True without touching UI.
    logger               : Where to write step logs and warnings.
    unattended           : If True, skip all "Press ENTER to continue" prompts
                           and trust that Soundminer's persistent settings are
                           correct.  Use only after a fully-attended run has
                           confirmed the Mirror Settings dialog is configured
                           per spec; otherwise the operator can't catch a
                           settings drift between releases.
    skip_*               : Individual phase skips for restart/recovery.
    manual_verify_mirror_settings :
                           Override the default (which is `not unattended`).
                           When True, pause for human inspection of the
                           Mirror Settings dialog.  When False, send Enter
                           to accept whatever the dialog currently shows.

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

    # ---- Preflight: tooling -----------------------------------------------
    if not verify_screenshots(logger):
        return False
    if not _verify_pyautogui_installed(logger):
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

        if skip_import:
            logger.info("  ↩  Skipping 12.4 import metadata (per flag).")
        else:
            _import_metadata(csv_path, audio_folder, logger,
                             unattended=unattended)

        if skip_embed:
            logger.info("  ↩  Skipping 12.5 embed selected records (per flag).")
        else:
            _select_all_and_embed(logger, unattended=unattended)

        if skip_mirror:
            logger.info("  ↩  Skipping 12.6 mirror (per flag).")
            logger.info("  ✓  Step 12 partial — mirror skipped per flag.")
            return True

        _open_mirror_dialog(logger)
        _verify_mirror_settings_dialog(
            logger,
            manual_verify=manual_verify_mirror_settings,
        )
        _click_mirror_ok(logger)
        _navigate_mirror_destination(mirror_dest, logger)
        _wait_for_mirror_complete(mirror_dest, logger)

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

# Mirror Settings required for SourceAudio (per Soundminer_mirror_sa.png).
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
    "          Filename Scheme:                    Filename:1\n"
    "          Use mono(.M) extension:             ON\n"
    "          Filename Limit:                     255\n"
    "          Strip illegal characters:           ON\n"
    "          Use Source SR/Bit Depth:            ON\n"
    "          Sample Rate:                        Not Applicable\n"
    "          Bit Depth:                          Not Applicable"
)

SOURCEAUDIO_OUTPUT_EXTS = ("aif", "aiff")


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
    _open_panel_go_to_path(str(scan_folder), logger)
    time.sleep(2.0)
    _save_step_screenshot("11c_after_scan_folder", logger)

    # The scan can raise the same Unmatched Fields / Dupes dialogs the import
    # path does; auto-dismiss them (best-effort).
    _watch_and_dismiss_import_dialogs(logger)

    _wait_with_manual_handshake(
        phase_label  = "scan",
        soft_minutes = 2,
        hard_timeout = IMPORT_TIMEOUT,
        unattended   = unattended,
        logger       = logger,
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
) -> bool:
    """
    SourceAudio delivery (Step 11 — runs right before the NBC Soundminer step).

    For each (source folder → destination) pair:
        delete all records → Scan Sounds into Database → Mirror to AIFF.

      1. WAV w COVERS/MEDIA            → …Release - SourceAudio/Music
      2. 2-STAGING/SME WAV ExUS/MEDIA  → …Release - SourceAudio Ex-US/Music

    The mirror uses the SourceAudio settings (AIFF, Build Using Library then
    Volume, Filename:1, etc.).  Soundminer persists ONE set of mirror
    settings, so an attended verification pause lets the operator confirm /
    set them before OK on the first pass (and they carry to the second).

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
    if not _verify_pyautogui_installed(logger):
        return False
    if not verify_screenshots(logger):
        return False

    if manual_verify_mirror_settings is None:
        manual_verify_mirror_settings = not unattended

    DEBUG_STEP_DIR.mkdir(parents=True, exist_ok=True)
    FAILURE_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

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

            # Switch to the SourceAudio database, then clear it, then scan.
            # (Only the first pass needs the attended fallback if no shortcut.)
            _switch_to_sourceaudio(
                db_shortcut, logger,
                confirm=manual_verify_mirror_settings and idx == 0,
            )
            _delete_all_records(logger)
            _scan_sounds_into_database(src, logger, unattended=unattended)

            _open_mirror_dialog(logger)
            _verify_mirror_settings_dialog(
                logger,
                manual_verify=manual_verify_mirror_settings,
                expected_text=SOURCEAUDIO_MIRROR_SETTINGS,
            )
            _click_mirror_ok(logger)
            _navigate_mirror_destination(dest, logger)
            _wait_for_mirror_complete(dest, logger, output_exts=SOURCEAUDIO_OUTPUT_EXTS)
            logger.info(f"  ✓  SourceAudio {tag} mirror finished → {dest}")

            # Only the FIRST pass needs the attended settings check; the
            # settings (and DB selection) persist for the second pass.
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

def _activate_soundminer(logger: logging.Logger) -> None:
    """
    Bring Soundminer v5Pro to the foreground; launch it if necessary.

    A pair of Escapes after activation dismisses any stuck modal left over
    from a previous aborted run (same defensive pattern UniSync uses).
    """
    import pyautogui

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

    # Clear any stuck dialogs from a previous abort
    pyautogui.press("escape")
    time.sleep(0.3)
    pyautogui.press("escape")
    time.sleep(0.3)

    logger.debug(f"  {SOUNDMINER_APP} is active.")
    _save_step_screenshot("12_2a_activated", logger)


def _switch_to_nbcuniversal(logger: logging.Logger) -> None:
    """
    Switch the active database to NBCUniversal via the ⌘5 keyboard shortcut
    (visible in the Soundminer_nbc.png reference: NBCUniversal = "⌘5").

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
    _open_panel_go_to_path(str(audio_folder), logger)
    time.sleep(2.0)
    _save_step_screenshot("12_4c_after_audio", logger)

    # After the panels are confirmed, Soundminer raises the "Unmatched Fields"
    # and "Check for Dupes Warning" dialogs, which BLOCK the import until OK'd.
    # Auto-dismiss them (best-effort; operator handles any during the wait).
    _watch_and_dismiss_import_dialogs(logger)

    # Wait for the import to complete.  This is the noisiest part of the
    # workflow because we have no signal from Soundminer; we just have to
    # wait long enough for the indexing to finish.
    _wait_with_manual_handshake(
        phase_label  = "import",
        soft_minutes = 2,                   # log "still importing…" after this
        hard_timeout = IMPORT_TIMEOUT,
        unattended   = unattended,
        logger       = logger,
    )
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
    _activate_soundminer(logger)  # ensure focus before keystroke
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
    )

    # After embed, a log window may list files that were not scanned (e.g.
    # "Scan Failure (Unable to locate soundfile)").  Surface it with guidance
    # — these usually mean the audio files aren't where the CSV expects them.
    _report_embed_log_window(logger)
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
            "        --unattended: skipping settings-verification pause.  "
            "Trusting Soundminer's persistent settings."
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


def _click_mirror_ok(logger: logging.Logger) -> None:
    """
    Confirm the Mirror Settings dialog (trigger its default OK button).

    Approach: bring the dialog into focus, then press Return.  Return is
    bound to the dialog's default button (OK), but ONLY when the dialog —
    not one of its text fields, and not another window — has focus.  In
    testing, pressing Return blind sent it to the focused Filename-Limit
    field and the dialog stayed open.  So we first click the dialog's title
    bar (a neutral region that focuses the window without altering any
    setting), then press Return.

    We click the title bar by position (centred dialog, title near the top)
    rather than image-matching, and we still try the OK-button image match
    first in case it succeeds for a precise click.
    """
    import pyautogui

    # 1. Precise path: if we can image-match the OK button, click it directly.
    ok_img = _img(REQUIRED_SCREENSHOTS["mirror_ok"])
    loc = _locate_safe(ok_img)
    if loc is not None:
        center = pyautogui.center(loc)
        logger.info(f"  12.6c Clicking Mirror Settings OK at {center} (matched)…")
        pyautogui.click(center.x, center.y)
        time.sleep(POST_CLICK_WAIT)
        _save_step_screenshot("12_6c_after_mirror_ok", logger)
        return

    # 2. Focus-then-Return path.  Click the dialog title bar to focus the
    #    window (the Mirror Settings dialog is centred; its title sits near
    #    the top-centre — ~0.50w, ~0.199h, i.e. ~(1280, 287) on a 2560-wide
    #    display).  This region carries no control, so the click only
    #    changes focus.  Then Return triggers the default OK button.
    sw, sh = pyautogui.size()
    title_x = int(sw * 0.50)
    title_y = int(sh * 0.199)
    logger.info(
        "  12.6c Could not match OK button; focusing dialog title bar at "
        f"({title_x}, {title_y}) then pressing Return…"
    )
    pyautogui.click(title_x, title_y)
    time.sleep(0.5)
    pyautogui.press("enter")
    time.sleep(POST_CLICK_WAIT)
    _save_step_screenshot("12_6c_after_mirror_ok", logger)


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
    _open_panel_go_to_path(str(mirror_dest), logger)
    time.sleep(2.0)
    _save_step_screenshot("12_6d_after_dest", logger)


# ---------------------------------------------------------------------------
# 12.7 — Wait for mirror completion
# ---------------------------------------------------------------------------

def _wait_for_mirror_complete(
    mirror_dest: Path,
    logger:      logging.Logger,
    output_exts: tuple[str, ...] = ("wav",),
) -> None:
    """
    Poll ``mirror_dest`` for new output files until the count stabilises
    for MIRROR_STABILITY_WINDOW seconds.  Same completion-detection
    pattern UniSync's _wait_for_job_output uses, generalised to "any file
    with one of ``output_exts`` appearing under the dest tree" (NBC mirrors
    to .wav; SourceAudio mirrors to .aif/.aiff).
    """
    exts = tuple(e.lower().lstrip(".") for e in output_exts)
    logger.info(
        f"  Polling mirror destination for new "
        f"{'/'.join('.' + e for e in exts)} files…"
    )

    def _count_outputs() -> int:
        total = 0
        for e in exts:
            total += sum(1 for _ in mirror_dest.rglob(f"*.{e}"))
        return total

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
            count = _count_outputs()
        except Exception as exc:
            logger.warning(f"    Could not count dest files: {exc}")
            count = last_count

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
    """
    script = (
        f'tell application "System Events"\n'
        f'    tell process "{SOUNDMINER_APP}"\n'
        f'        set frontmost to true\n'
        f'        click menu item "{item_title}" of menu "{menu_title}" '
        f'of menu bar 1\n'
        f'    end tell\n'
        f'end tell'
    )
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise _SoundminerError(
            f"Menu click failed: {menu_title} → {item_title}\n"
            f"  osascript stderr: {result.stderr.strip()}\n"
            f"  Common causes:\n"
            f"  - Terminal not granted Automation access for '{SOUNDMINER_APP}'\n"
            f"    or 'System Events' (System Settings → Privacy & Security\n"
            f"    → Automation).\n"
            f"  - The menu item label has changed in Soundminer's UI.\n"
            f"  - Soundminer wasn't running."
        )
    logger.debug(f"  Menu: {menu_title} → {item_title}")


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
) -> None:
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
    while time.monotonic() < end:
        hit = False
        # Order: unmatched-fields typically precedes the dupes confirm.
        if _dismiss_dialog_if_present("unmatched_fields", "Unmatched Fields", logger):
            hit = True; seen += 1
        if _dismiss_dialog_if_present("dupes_warning", "Check for Dupes Warning", logger):
            hit = True; seen += 1
        # Once the progress bar is up, the gating dialogs are done — stop early.
        if not hit and _locate_safe(_img(OPTIONAL_DIALOG_SCREENSHOTS["importing_text"])) is not None:
            logger.info("        Import progress bar visible — dialogs cleared.")
            break
        time.sleep(0.5 if hit else 2.0)
    if seen:
        logger.info(f"        Auto-dismissed {seen} import dialog(s).")


def _report_embed_log_window(logger: logging.Logger) -> None:
    """
    After embed, Soundminer shows a log window listing any files that were
    NOT scanned during embedding.  We don't auto-resolve this (it needs the
    operator to confirm the files exist and decide whether to re-embed), but
    if the log-window crop is on screen we surface clear guidance.
    """
    fn = OPTIONAL_DIALOG_SCREENSHOTS.get("log_window")
    if not fn or not Path(_img(fn)).exists():
        return
    if _locate_safe(_img(fn)) is None:
        return
    _save_step_screenshot("embed_log_window", logger)
    logger.warning(
        "        ⚠ Embed log window detected — it lists files that were NOT\n"
        "          scanned during embedding.  Before continuing:\n"
        "            1. Review the listed files; confirm each exists in the\n"
        "               audio source folder.\n"
        "            2. Re-run the embed (12.5) for the missing ones.\n"
        "            3. Repeat until no further files are reported.  If the\n"
        "               list stops shrinking across several tries, the\n"
        "               metadata CSV is likely incorrect — investigate that\n"
        "               rather than retrying further.\n"
        "          A snapshot was saved to the soundminer step-screenshots dir."
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
) -> None:
    """
    Inside an open macOS NSOpenPanel, navigate to `path` via Cmd+Shift+G,
    paste via clipboard, then two Enters (navigate + confirm).

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

    # Deliver the path via clipboard paste (immune to keyboard-layout drift)
    if _set_clipboard(path, logger):
        time.sleep(0.15)
        pyautogui.hotkey("command", "v")
    else:
        logger.warning(
            "    Clipboard unavailable — falling back to typing the path "
            "(special characters may be unreliable)."
        )
        pyautogui.write(path, interval=0.04)
    time.sleep(0.5)
    _save_step_screenshot("dlg_03_after_paste", logger)

    # First Enter — navigate to pasted path
    pyautogui.press("enter")
    time.sleep(0.9)
    _save_step_screenshot("dlg_04_after_first_enter", logger)

    # Second Enter — click Open / confirm
    pyautogui.press("enter")
    time.sleep(0.9)
    _save_step_screenshot("dlg_05_after_second_enter", logger)

    logger.debug(f"    NSOpenPanel → {path}")


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

def _wait_with_manual_handshake(
    *,
    phase_label:  str,
    soft_minutes: int,
    hard_timeout: int,
    unattended:   bool,
    logger:       logging.Logger,
) -> None:
    """
    Block for a phase whose completion we can't poll (import, embed).

    Behaviour:
      unattended=True  → log progress every PROGRESS_DOT_INTERVAL seconds,
                         return after `soft_minutes` minutes (operator
                         has confirmed previously that this is enough).
      unattended=False → log progress dots, then prompt the operator to
                         press Enter when the phase completes.

    `hard_timeout` is a safety ceiling — even in unattended mode we won't
    block longer than this; if hit, _SoundminerError is raised.
    """
    start = time.monotonic()
    if unattended:
        soft_seconds = soft_minutes * 60
        logger.info(
            f"        --unattended: waiting up to {soft_seconds}s for "
            f"{phase_label} to settle…"
        )
        next_log = start + PROGRESS_DOT_INTERVAL
        while True:
            now = time.monotonic()
            elapsed = now - start
            if elapsed >= soft_seconds:
                logger.info(
                    f"        Soft wait complete ({int(elapsed)}s).  "
                    f"Continuing."
                )
                return
            if elapsed > hard_timeout:
                raise _SoundminerError(
                    f"{phase_label} exceeded hard timeout of {hard_timeout}s."
                )
            if now >= next_log:
                logger.info(f"        … still waiting for {phase_label} ({int(elapsed)}s)")
                next_log = now + PROGRESS_DOT_INTERVAL
            time.sleep(POLL_INTERVAL)
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
            "Drive Soundminer v5Pro: the NBC embed + mirror workflow (Step 12, "
            "default) or the SourceAudio scan + AIFF mirror (Step 11, "
            "--sourceaudio).  Use --dry-run to preview without touching the UI."
        ),
    )
    p.add_argument("--sourceaudio", action="store_true",
                   help="Run the SourceAudio scan + AIFF mirror workflow "
                        "(Step 11) instead of the NBC embed + mirror (Step 12).")
    p.add_argument("--sourceaudio-db-shortcut", default="8", metavar="KEY",
                   help="Database shortcut digit for the SourceAudio DB "
                        "(default '8' = ⌘8). Only used with --sourceaudio.")
    p.add_argument("--previous-month", action="store_true",
                   help="Full-month (previous-month) run.  --year/--month "
                        "optional: omit both to target the month before today, "
                        "or pass both to pin the reference month.")
    p.add_argument("--year",  type=int, default=None)
    p.add_argument("--month", type=int, default=None)
    p.add_argument("--part",  type=int, choices=[1, 2], default=None)

    p.add_argument("--dry-run", action="store_true",
                   help="Log the plan and exit without touching the UI.")
    p.add_argument("--skip-soundminer", action="store_true",
                   help="No-op for parity with the orchestrator's flag.")

    p.add_argument("--unattended", action="store_true",
                   help="Skip operator-handshake prompts (import, embed, "
                        "mirror settings).  Use only after a confirmed run.")
    p.add_argument("--skip-delete-records", action="store_true",
                   help="Skip 12.3 — assume the DB is already empty.")
    p.add_argument("--skip-import", action="store_true",
                   help="Skip 12.4 — assume metadata is already imported.")
    p.add_argument("--skip-embed", action="store_true",
                   help="Skip 12.5 — assume metadata is already embedded.")
    p.add_argument("--skip-mirror", action="store_true",
                   help="Skip 12.6 / 12.7 — stop after embed.")
    p.add_argument("--capture-steps", action="store_true",
                   help="Save numbered step screenshots to "
                        f"{DEBUG_STEP_DIR}.")
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
    logger.info(f"Release context: {ctx}")

    if args.sourceaudio:
        ok = run_soundminer_sourceaudio_workflow(
            ctx,
            dry_run=args.dry_run,
            logger=logger,
            unattended=args.unattended,
            db_shortcut=args.sourceaudio_db_shortcut,
        )
        return 0 if ok else 1

    ok = run_soundminer_nbc_workflow(
        ctx,
        dry_run=args.dry_run,
        logger=logger,
        unattended=args.unattended,
        skip_delete_records=args.skip_delete_records,
        skip_import=args.skip_import,
        skip_embed=args.skip_embed,
        skip_mirror=args.skip_mirror,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_run_cli())
