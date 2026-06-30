"""
unisync_automation.py — Step 5: UniSync Music Export
=======================================================
Automates UniSync (macOS native app, v1.0.43) using PyAutoGUI +
locateOnScreen() for all UI interaction.  UniSync has no usable
AppleScript dictionary.

Job sequence (repeated for each of six export jobs):
  1.  Bring UniSync to front (launch if not running).
  2.  Click the folder icon next to CACHE DRIVE PATH → navigate to path
      via Cmd+Shift+G in the NSOpenPanel → confirm.
  3.  Same for CLIENT DRIVE PATH.
  4.  Click the hamburger (≡) button (top-left of BUILD CHOICES panel).
  5.  Click "Choose a csv of workaudioids" in the dropdown.
  6.  Navigate to the CSV file via Cmd+Shift+G → confirm.  UniSync
      auto-starts the export the moment the CSV loads (no START button).
  7.  Watch the client OUTPUT folder until the CSV's expected files have
      landed (or delivery stalls), up to JOB_TIMEOUT seconds.
  8.  Move to the next job.  Stop all remaining jobs on any failure.

Required reference screenshots — all must exist in SCREENSHOTS_DIR:
    Filename                        What to crop
    ──────────────────────────────  ─────────────────────────────────────────
    unisync_cache_btn.png           Folder icon (📁) next to CACHE DRIVE PATH
    unisync_client_btn.png          Folder icon (📁) next to CLIENT DRIVE PATH
    unisync_hamburger_btn.png       ≡ button (circled red in Unisync_menu.png)
    unisync_choose_csv.png          "Choose a csv of workaudioids" menu row (crop
                                    TIGHT — just that row, ~20px tall)

NOTE: UniSync has NO START button in this workflow — it auto-starts the
export as soon as the chosen CSV finishes loading.  Selecting the CSV is
the trigger that begins the job.  Completion is detected by watching the
client output folder, NOT the on-screen FINISHED label (that label
persists from the previous job and never clears on auto-start).

NOTE: unisync_cache_btn.png and unisync_client_btn.png are the two small
folder icons in the SETTINGS panel of Unisync.png.  Crop each icon tightly
(~24×24 px) so locateOnScreen() can distinguish them reliably.

Prerequisites:
    pip install pyautogui Pillow
    macOS: System Settings → Privacy & Security → Accessibility → allow Terminal
    macOS: System Settings → Privacy & Security → Screen Recording → allow Terminal
"""

from __future__ import annotations

import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path

from config import ReleaseContext

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Screenshots live next to the code (sibling of files/), derived from this
# script's own location so the path is correct regardless of where the repo
# sits.  This previously hardcoded "/Users/hdfuser/Documents/Python/…" which
# was missing the "Scripts/" level — wrong on the actual pipeline machine
# (/Users/hdfuser/Documents/Scripts/Python/…).  Deriving from __file__ avoids
# that whole class of path bugs.
_REPO_ROOT = Path(__file__).resolve().parent.parent

SCREENSHOTS_DIR = _REPO_ROOT / "screenshots"

# Where to save full-screen captures whenever a job fails — invaluable for
# diagnosing what state UniSync was in when the locator timed out.
DEBUG_STEP_DIR = _REPO_ROOT / "_logs" / "unisync_debug_steps"

FAILURE_SCREENSHOTS_DIR = _REPO_ROOT / "_logs" / "unisync_failures"

# Reused wherever a screen-capture / image-match failure points at the macOS
# Screen Recording permission (the usual root cause on this setup).
_SCREEN_PERM_HINT = (
    "Grant macOS Screen Recording AND Accessibility to Terminal (or whatever\n"
    "     runs python): System Settings → Privacy & Security → Screen & System\n"
    "     Audio Recording (and → Accessibility).  Then FULLY quit and reopen\n"
    "     Terminal — toggling the permission isn't enough; the app must restart."
)

UNISYNC_APP        = "UniSync"
JOB_TIMEOUT        = 21600  # seconds per job (6 hours).  Big catalogs
                            # — especially WAV jobs with thousands of
                            # tracks — can legitimately run for several
                            # hours.  Override per-run with --timeout.
LOCATE_RETRIES     = 12     # attempts before giving up on a UI element
LOCATE_DELAY       = 2.0    # seconds between locate retries
LOCATE_CONFIDENCE  = 0.85   # pyautogui confidence threshold
LAUNCH_WAIT        = 4.0    # seconds after launching the app
DIALOG_OPEN_WAIT   = 1.5    # seconds for NSOpenPanel to animate open
POST_CLICK_WAIT    = 0.6    # seconds after most button clicks

# CSV picker open-verification.  Clicking "Choose a csv of workaudioids" can
# fail to open the file dialog (the hamburger menu is occasionally flaky,
# and the menu-item crop is large so a center-click can miss the row).
# After clicking, we compare a small screenshot against the pre-menu
# baseline: an open dialog is a big bright overlay → large mean pixel
# difference; "menu just closed, no dialog" looks ~identical to the main
# window → small difference → retry.
CSV_PICKER_MAX_ATTEMPTS = 3
DIALOG_OPEN_DIFF_THRESHOLD = 10.0  # mean abs grayscale diff (0-255)
PATH_ENTRY_ATTEMPTS = 3            # retries for entering a path into a Go-to sheet

# Diagnostic toggle: when True, _save_step_screenshot writes a PNG at each
# phase of path entry.  Set via the --capture-steps CLI flag.  Declared at
# module scope so functions can read it without import gymnastics.
CAPTURE_STEPS      = False
POST_CSV_SETTLE    = 5.0    # seconds after CSV load before watching output

# Supervised mode (set via --unisync-supervised).  When True:
#   • A UI failure during a job (usually accidental focus loss — the mouse or
#     keyboard was touched, or a window came forward) PAUSES and lets the user
#     press Enter to retry that pass instead of failing the run.
#   • After the automatic retry cap, if files are still undelivered, PAUSES and
#     lets the user press Enter to keep retrying ONLY the missing files until
#     they are all in, before continuing to the copy/packaging steps.
# When False (default, unattended), behaviour is unchanged: failures stop the
# job and leftovers after the cap are deferred to verification.
SUPERVISED         = False

# When True (set via --unisync-xml-setup), configure each job by writing its
# Territory/Cache/Client into UniSync.xml and relaunching UniSync, instead of
# driving the macOS path-entry UI (folder icons + Cmd+Shift+G).  UniSync reads
# these prefs only at launch — it does NOT pick up live edits — so this mode
# QUITS and RELAUNCHES UniSync on the first pass of each file type.  Same-type
# retries reuse the already-running app (no relaunch).  The UI path-entry
# remains the default/fallback.
XML_SETUP          = False
# Location of UniSync's preferences file (holds the userPrefs cache/client).
UNISYNC_XML_PATH   = "/Users/hdfuser/Library/SMUniSync/UniSync.xml"


def set_capture_steps(enabled: bool) -> None:
    """Enable/disable per-step UniSync screenshots (used by the orchestrator
    to pass through its --capture-steps flag)."""
    global CAPTURE_STEPS
    CAPTURE_STEPS = bool(enabled)


def set_supervised(enabled: bool) -> None:
    """Enable/disable supervised pause-and-retry (orchestrator passes through
    its --unisync-supervised flag)."""
    global SUPERVISED
    SUPERVISED = bool(enabled)


def set_xml_setup(enabled: bool) -> None:
    """Enable/disable configuring jobs by writing UniSync.xml + relaunching,
    instead of the UI path-entry (orchestrator passes through its
    --unisync-xml-setup flag)."""
    global XML_SETUP
    XML_SETUP = bool(enabled)


# Completion is detected by watching the client OUTPUT folder, not the
# FINISHED label.  UniSync auto-starts on CSV load and does NOT clear the
# previous job's FINISHED label, so that label is useless for telling when
# THIS job finishes.  Instead we count how many of the CSV's expected
# output files have landed in the client folder.
OUTPUT_POLL_INTERVAL   = 10   # seconds between client-folder scans
OUTPUT_STARTUP_GRACE   = 600  # seconds to see the FIRST delivered file when
                              # NOTHING is present yet (covers UniSync's cold
                              # pre-scan on a big catalog) before concluding the
                              # job never started
OUTPUT_STABILITY_WINDOW = 90  # seconds with NO increase in the delivered count
                              # ⇒ UniSync has stopped delivering for this CSV.
                              # Measured on the COUNT (not folder mtime), so
                              # phantom writes don't keep the job alive.  Active
                              # delivery comes in <30s gaps, so 90s is a safe
                              # "it's stopped" signal; a premature stop just
                              # triggers a reduced-CSV retry for the remainder.
OUTPUT_COMPLETE_SETTLE = 10   # seconds — once EVERY expected file is present,
                              # the job's output is complete by definition, so
                              # we don't sit through the full stability window;
                              # we just confirm the last write has settled this
                              # briefly (so an in-flight final file finishes),
                              # then move straight to the next job.
# Retry passes request a small, known set of files.  If UniSync is going to
# deliver them it starts within seconds, and (now that the download bug is
# fixed) a retry that delivers NOTHING means those tracks are absent from the
# UPM source — no point waiting out the full first-pass grace.  These tighter
# windows stop wasting minutes on not-found tracks.
OUTPUT_RETRY_STARTUP_GRACE   = 90   # seconds to see the FIRST delivery on a retry
OUTPUT_RETRY_STABILITY_WINDOW = 30  # seconds of no delivery on a retry ⇒ done
INTER_JOB_PAUSE    = 3.0    # seconds between successive jobs

# When a file type finishes with some files undelivered, re-run UniSync for
# just the missing ones (a reduced CSV) rather than the whole tracklist.
# Retries are UNLIMITED as long as each pass delivers at least one NEW file —
# UniSync stalls mid-transfer when its auth token / temporary AWS credentials
# expire, and every fresh CSV load re-authenticates and delivers another
# chunk, so we keep going until the client folder stops growing.  The loop
# stops only when a whole pass adds zero new files (the remainder is then
# genuinely absent from the source) or, in --unisync-supervised mode, when the
# user chooses to skip.  There is intentionally no fixed attempt cap.

STATUS_OK      = "ok"
STATUS_SKIPPED = "skipped"
STATUS_FAILED  = "failed"

# Every screenshot the automation needs, keyed by logical name
REQUIRED_SCREENSHOTS: dict[str, str] = {
    "cache_btn":          "unisync_cache_btn.png",
    "client_btn":         "unisync_client_btn.png",
    "hamburger_btn":      "unisync_hamburger_btn.png",
    "choose_csv":         "unisync_choose_csv.png",
    "territory_dropdown": "unisync_territory_dropdown.png",
    "terr_us":            "unisync_terr_united_states.png",
    "terr_us_mp3":        "unisync_terr_united_states_mp3.png",
    "terr_row":           "unisync_terr_rest_of_world.png",
    "terr_row_mp3":       "unisync_terr_rest_of_world_mp3.png",
    "terr_japan":         "unisync_terr_japan.png",
}

# Maps a job's territory value → the screenshot of that option in the
# open dropdown.  Add new entries here only if jobs are added; the script
# is intentionally constrained to the five territories UPM actually uses.
TERRITORY_SCREENSHOTS: dict[str, str] = {
    "United States":        "unisync_terr_united_states.png",
    "United States (MP3)":  "unisync_terr_united_states_mp3.png",
    "Rest of World":        "unisync_terr_rest_of_world.png",
    "Rest of World (MP3)":  "unisync_terr_rest_of_world_mp3.png",
    "Japan":                "unisync_terr_japan.png",
}


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def _probe_screen_capture(logger: logging.Logger) -> bool:
    """
    Verify the process can actually capture the screen, which both screenshots
    and pyautogui's image matching require.  Returns True if a capture comes
    back with real (non-blank) pixel content.

    Method: take a pyautogui screenshot and check that it isn't uniformly one
    color.  A missing Screen Recording permission on macOS yields either a
    None/!error capture or an all-black image, both of which we treat as a
    hard failure with actionable guidance — because in that state every
    locateOnScreen call will silently time out and look like a crop mismatch.
    """
    try:
        import pyautogui
        img = pyautogui.screenshot()
    except Exception as exc:
        logger.error(
            f"  ✗  Screen capture failed during preflight: {exc}\n"
            f"     {_SCREEN_PERM_HINT}"
        )
        return False

    if img is None:
        logger.error("  ✗  Screen capture returned nothing during preflight.\n"
                     f"     {_SCREEN_PERM_HINT}")
        return False

    # Detect an all-one-color (e.g. all-black) capture, the classic signature
    # of a denied Screen Recording permission.
    try:
        extrema = img.convert("RGB").getextrema()  # ((rmin,rmax),(g..),(b..))
        flat = all(lo == hi for (lo, hi) in extrema)
    except Exception:
        flat = False
    if flat:
        logger.error(
            "  ✗  Screen capture came back blank (single color).\n"
            f"     {_SCREEN_PERM_HINT}"
        )
        return False

    logger.info("  ✓  Screen capture works (Screen Recording permission OK).")
    return True


def verify_screenshots(logger: logging.Logger) -> bool:
    """
    Confirm every required reference screenshot exists.
    Returns True only if all are present.  Call during preflight.
    """
    if not SCREENSHOTS_DIR.exists():
        logger.error(
            f"Screenshots directory not found:\n"
            f"  {SCREENSHOTS_DIR}\n"
            f"  Create it and add the six PNG crops listed in the module docstring."
        )
        return False

    all_ok = True
    for key, filename in REQUIRED_SCREENSHOTS.items():
        path = SCREENSHOTS_DIR / filename
        if path.exists():
            logger.info(f"  ✓  {filename}")
        else:
            logger.error(
                f"  ✗  {filename}  ← MISSING\n"
                f"     {path}"
            )
            all_ok = False

    if not all_ok:
        logger.error(
            "\n  Crop the missing screenshots from the UniSync reference images\n"
            "  (Unisync.png, Unisync_menu.png, Unisync_csv.png) and save them to:\n"
            f"  {SCREENSHOTS_DIR}"
        )
    return all_ok


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_all_unisync_jobs(
    ctx: ReleaseContext,
    dry_run: bool,
    logger: logging.Logger,
    overwrite: bool = False,
) -> dict[str, str]:
    """
    Drive UniSync through all six export jobs sequentially.
    Returns a dict mapping job["name"] → STATUS_* constant.

    Stops on the first failed job and marks all remaining as failed.

    When `overwrite` is False (default) a job whose client folder already
    holds every expected output file is skipped entirely — no UniSync UI
    is touched.  This makes the workflow restartable and avoids racing
    ahead while UniSync is still busy on a previous CSV.  Pass
    `overwrite=True` to force every job to run regardless.
    """
    # Screenshot preflight
    if not verify_screenshots(logger):
        return {job["name"]: STATUS_FAILED for job in ctx.unisync_jobs}

    # pyautogui availability check
    try:
        import pyautogui  # noqa: F401
    except ImportError:
        logger.error(
            "pyautogui is not installed.\n"
            "  Run:  pip install pyautogui Pillow"
        )
        return {job["name"]: STATUS_FAILED for job in ctx.unisync_jobs}

    # Screen-recording capability probe.  Image matching (locateOnScreen) and
    # all screenshots depend on macOS Screen Recording permission.  Without
    # it, captures come back blank/None and EVERY locate silently times out —
    # which looks exactly like "crop doesn't match the UI".  Probe once and
    # fail fast with actionable guidance rather than burning 12 attempts per
    # element across six jobs.
    if not dry_run and not _probe_screen_capture(logger):
        return {job["name"]: STATUS_FAILED for job in ctx.unisync_jobs}

    results: dict[str, str] = {}

    for i, job in enumerate(ctx.unisync_jobs):
        logger.info(f"\n{'─' * 52}")
        logger.info(f"UniSync job {i + 1}/{len(ctx.unisync_jobs)}: {job['name']}")
        logger.info(f"  Cache:  {job['cache_path']}")
        logger.info(f"  Client: {job['client_path']}")
        logger.info(f"  CSV:    {job['csv']}")

        status = _run_single_job(job, dry_run, logger, overwrite=overwrite)
        results[job["name"]] = status

        if status == STATUS_FAILED:
            logger.error(
                f"  Job '{job['name']}' FAILED — stopping.\n"
                f"  Resolve the issue, then re-run with appropriate --skip flags."
            )
            for remaining in ctx.unisync_jobs[i + 1:]:
                results[remaining["name"]] = STATUS_FAILED
            break

        # Brief pause between jobs so UniSync settles before the next
        # paths and CSV are entered.  Skipped jobs don't touch the UI,
        # so no pause is needed in that case.
        if i < len(ctx.unisync_jobs) - 1 and status != STATUS_SKIPPED:
            logger.debug(f"  Inter-job pause ({INTER_JOB_PAUSE}s)…")
            time.sleep(INTER_JOB_PAUSE)

    return results


# ---------------------------------------------------------------------------
# Single-job driver
# ---------------------------------------------------------------------------

def _run_single_job(
    job: dict,
    dry_run: bool,
    logger: logging.Logger,
    overwrite: bool = False,
) -> str:
    """
    Execute one UniSync export job end-to-end.

    Unless `overwrite=True`, the job is skipped entirely (no UniSync UI is
    touched) when every file the CSV expects is already present in the
    client folder.  This makes restarted runs cheap and safe — there's no
    risk of starting a new UniSync job while a previous one is still
    settling, because we never touch UniSync for already-done work.
    """
    if dry_run:
        logger.info(f"  [DRY RUN] Would run UniSync job: {job['name']}")
        logger.info(f"    Cache:  {job['cache_path']}")
        logger.info(f"    Client: {job['client_path']}")
        logger.info(f"    CSV:    {job['csv']}")
        return STATUS_SKIPPED

    # Preflight — refuse to drive the UI if the CSV is missing
    csv_path = Path(job["csv"])
    if not csv_path.exists():
        logger.error(
            f"  ✗  CSV not found for '{job['name']}':\n"
            f"     {csv_path}\n"
            f"     Run Step 1 (Domo exports) to produce this file."
        )
        return STATUS_FAILED

    # Preflight — every path must be on a mounted volume.
    #
    # macOS exposes mount points under /Volumes/<drive>.  /Volumes itself
    # always exists, so a path under /Volumes/<X>/... is ONLY reachable
    # if /Volumes/<X> is also present (the drive is mounted).  If the
    # drive isn't mounted, UniSync's Go-to-Folder dialog will refuse to
    # navigate to the typed path and the popup gets stuck — which then
    # blocks every subsequent click.  Fail loudly here instead.
    #
    # CSV existence is checked above; cache and client roots just need
    # their volumes mounted (UniSync creates leaf directories on demand).
    def _required_volume(p: Path) -> Path | None:
        parts = p.parts  # ('/', 'Volumes', '<drive>', ...) on macOS
        if len(parts) >= 3 and parts[1] == "Volumes":
            return Path(parts[0]) / parts[1] / parts[2]
        return None

    for label, path_str in (
        ("cache",  job["cache_path"]),
        ("client", job["client_path"]),
        ("csv",    job["csv"]),
    ):
        vol = _required_volume(Path(path_str))
        if vol is None:
            continue  # path isn't under /Volumes — local path, nothing to check
        if not vol.exists():
            logger.error(
                f"  ✗  Volume not mounted for '{job['name']}':\n"
                f"     {vol}\n"
                f"     Required for {label} path: {path_str}\n"
                f"     Mount the drive in Finder and re-run."
            )
            return STATUS_FAILED

    import os

    ext = ".mp3" if "MP3" in job["name"].upper() else ".wav"
    expected = _expected_output_filenames(job["csv"], ext, logger)
    total = len(expected)
    if total == 0:
        logger.warning("  Tracklist has no usable rows for this job; nothing to do.")
        return STATUS_OK

    present0 = _present_filenames(job["client_path"], expected)
    have0 = len(present0)

    # Skip-if-already-present: every expected file is in the client folder.
    if have0 >= total and not overwrite:
        logger.info(
            f"  ↩  All {total} expected {ext} file(s) already present in client "
            f"folder."
        )
        logger.info("     Skipping UniSync run.  Pass --overwrite to force a re-run.")
        return STATUS_SKIPPED
    if have0 >= total and overwrite:
        logger.info(
            f"  All {total} expected file(s) already present, but --overwrite "
            f"was set — re-requesting everything."
        )
        present0, have0 = set(), 0

    missing0 = expected - present0
    if have0 > 0:
        logger.info(
            f"  {have0}/{total} {ext} file(s) already present; requesting ONLY "
            f"the {len(missing0)} missing one(s) — no re-delivery of what's there."
        )

    # We always hand UniSync a request for just the MISSING files (written to a
    # temp CSV, never beside the tracklist).  When nothing is present yet the
    # request is the whole list (we load the original directly); on a resume it's
    # only the gaps — so UniSync never re-delivers files that are already there,
    # which is what made partial jobs sit for minutes re-stamping existing files.
    fresh_full = (have0 == 0)
    temp_csvs: list[str] = []

    def _request_csv(missing_set: set[str], n: int) -> str:
        # Whole list missing → just load the original (no temp copy needed).
        if missing_set == expected:
            return job["csv"]
        red = _write_reduced_csv(job["csv"], ext, missing_set, n, logger)
        if red:
            temp_csvs.append(red)
            return red
        return job["csv"]   # fallback: original (rare — reduced build failed)

    try:
        to_request = missing0
        csv_to_use = _request_csv(to_request, 0)
        prev_missing_count = len(missing0)   # zero-delivery first pass = no progress
        pass_no = 0
        force_setup = False

        while True:
            # In XML-setup mode a relaunch is cheap (~7s) and always yields a
            # clean, known-position window, so we relaunch on EVERY pass.  Reusing
            # the already-running app proved fragile: a drifted/refocused window
            # made the hamburger click miss (e.g. landing at (240,403) instead of
            # (577,344)) and the CSV menu wasn't found.  In UI mode, "setup" means
            # slow path re-entry, so there we only set up on the first pass or
            # after a failure.
            do_setup = (pass_no == 0) or force_setup or XML_SETUP
            force_setup = False
            # Only the FRESH full first pass (a large from-scratch delivery) gets
            # the long first-pass wait windows AND treats "nothing delivered" as a
            # load failure.  Every partial/retry request uses the tight windows and
            # treats "nothing delivered" as not-found (the tracks aren't in UPM).
            is_retry = not (pass_no == 0 and fresh_full)

            if pass_no == 0:
                logger.info(
                    f"  Requesting {len(to_request)} {ext} file(s)"
                    + ("." if fresh_full else f" (skipping {have0} already present).")
                )
            else:
                if do_setup:
                    tag = "relaunch" if XML_SETUP else "re-doing setup"
                else:
                    tag = "Territory/Cache/Client unchanged"
                logger.info(
                    f"  ↻  Retry pass {pass_no} for '{job['name']}' — "
                    f"{len(to_request)} missing file(s) ({tag})."
                )

            status = _drive_unisync_for_csv(
                job, csv_to_use, logger, do_setup=do_setup, is_retry=is_retry
            )

            # --- UI failure (often accidental focus loss) --------------------
            if status == STATUS_FAILED:
                if SUPERVISED and _pause_retry_prompt(
                    f"A UniSync UI step failed for '{job['name']}'.\n"
                    f"This usually means the mouse/keyboard was touched or a window\n"
                    f"came forward mid-step.  Make sure nothing is covering UniSync.",
                    "press Enter to retry this pass (it will re-set "
                    "Territory/Cache/Client), or 's' to give up on this file type",
                    logger,
                ):
                    force_setup = True
                    continue
                return STATUS_FAILED

            pass_no += 1

            present = _present_filenames(job["client_path"], expected)
            missing = expected - present
            if not missing:
                logger.info(
                    f"  ✓  Job complete: {job['name']} ({total}/{total} delivered)."
                )
                return STATUS_OK

            made_progress = len(missing) < prev_missing_count
            prev_missing_count = len(missing)

            # --- New files arrived → keep going (unlimited while progress) ----
            if made_progress:
                logger.warning(
                    f"  ⚠  {len(missing)}/{total} {ext} file(s) still undelivered."
                )
                to_request = missing
                csv_to_use = _request_csv(to_request, pass_no)
                continue

            # --- Zero new files this pass → not in the CURRENT list -----------
            # (download bug is fixed; undelivered now means genuinely not found).
            _report_not_found(job, missing, ext, logger)

            # Supervised: pause so you can refresh / re-export the tracklist,
            # then press Enter.  We re-read it, diff against the destination, and
            # fetch ONLY the new + still-missing tracks.  's' continues.
            if SUPERVISED and _pause_retry_prompt(
                f"{len(missing)}/{total} {ext} track(s) for '{job['name']}' were "
                f"not found in UPM (workAudioIds listed above).\n"
                f"If you've refreshed / re-exported the tracklist, pressing Enter\n"
                f"re-checks it and downloads only the NEW and still-missing tracks.",
                "press Enter to fetch new + missing tracks, or 's' to continue "
                "to the next step",
                logger,
            ):
                refreshed = _expected_output_filenames(job["csv"], ext, logger)
                if refreshed:
                    if len(refreshed) != total:
                        logger.info(
                            f"     Tracklist now lists {len(refreshed)} {ext} "
                            f"track(s) (was {total})."
                        )
                    expected = refreshed
                    total = len(expected)
                present = _present_filenames(job["client_path"], expected)
                missing = expected - present
                if not missing:
                    logger.info(
                        f"  ✓  All {total} tracks now present after the refresh — "
                        f"job complete."
                    )
                    return STATUS_OK
                logger.info(
                    f"     Destination has {len(present)}/{total}; fetching the "
                    f"{len(missing)} new/missing track(s) only."
                )
                to_request = missing
                csv_to_use = _request_csv(to_request, pass_no)
                prev_missing_count = len(missing)   # zero-delivery next = no progress
                force_setup = True                  # clean reload after the pause
                continue

            return STATUS_OK
    finally:
        # Tidy up the transient request CSVs.
        for tc in temp_csvs:
            try:
                os.remove(tc)
            except OSError:
                pass


def _pause_retry_prompt(reason: str, instruction: str, logger: logging.Logger) -> bool:
    """
    Supervised-mode pause.  Print why we paused and what the choices are, then
    block on the user.  Returns True to retry, False to skip/defer.

    In a non-interactive context (no stdin, e.g. piped/automated), returns
    False immediately so a run can never hang waiting for input.
    """
    bar = "─" * 58
    logger.warning("")
    logger.warning(f"  ┌─ UniSync paused ─────────────────────────────────────────")
    for line in reason.splitlines():
        logger.warning(f"  │ {line}")
    logger.warning(f"  │")
    logger.warning(f"  │ → {instruction}")
    logger.warning(f"  └{bar}")
    try:
        ans = input("  >>> Enter = retry,  's' + Enter = skip: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        logger.warning("  (no interactive input available — skipping)")
        return False
    return ans not in ("s", "skip", "n", "no", "q", "quit")


def _drive_unisync_for_csv(
    job: dict, csv_path: str, logger: logging.Logger,
    do_setup: bool = True, is_retry: bool = False,
) -> str:
    """
    Run one UniSync pass for `job` using `csv_path` (the full tracklist or a
    reduced retry CSV of only-undelivered files), then wait for output to
    settle.

    do_setup:
      True  — set Territory + CACHE + CLIENT before loading the CSV (first
              pass of a file type, or recovering after a failure).
      False — skip those: UniSync keeps the territory and cache/client paths
              as long as the file type is unchanged, so same-type retries only
              need to load the new CSV.  Saves time and avoids re-running the
              path-entry UI unnecessarily.

    Returns STATUS_OK once UniSync goes idle (regardless of whether every file
    landed — the caller compares against the full expected set and decides
    whether to retry), or STATUS_FAILED on a UI/load failure.
    """
    try:
        if do_setup and XML_SETUP:
            # Configure via UniSync.xml + relaunch (no UI path-entry at all).
            if not _relaunch_unisync_with_xml(job, logger):
                _capture_failure_screenshot(job["name"], logger)
                return STATUS_FAILED
        else:
            # Always bring the app to front (cheap; guards against focus drift).
            _activate_unisync(logger)

            if do_setup:
                # UI path-entry (default / fallback).
                # 1a — Territory dropdown
                _set_territory(job["territory"], logger)
                # 2 — CACHE DRIVE PATH (topmost folder icon = match #1)
                logger.info("  Setting CACHE DRIVE PATH…")
                _set_path_field("unisync_cache_btn.png", job["cache_path"], logger, nth=0)
                # 3 — CLIENT DRIVE PATH (second folder icon = match #2)
                logger.info("  Setting CLIENT DRIVE PATH…")
                _set_path_field("unisync_client_btn.png", job["client_path"], logger, nth=1)
            else:
                logger.info(
                    "  Reusing existing Territory / Cache / Client "
                    "(unchanged for this file type)."
                )

        # 4 + 5 — open the CSV file picker (hamburger → 'Choose a csv of
        # workaudioids'), verifying a dialog actually appeared.
        if not _open_csv_picker(logger):
            logger.error(
                f"  ✗  CSV file picker never opened after "
                f"{CSV_PICKER_MAX_ATTEMPTS} attempts.\n"
                f"     The hamburger menu or the 'Choose a csv of workaudioids'\n"
                f"     row may have shifted.  Re-crop unisync_choose_csv.png\n"
                f"     to JUST that menu row (tight, ~20px tall) so the click\n"
                f"     lands precisely on it."
            )
            _capture_failure_screenshot(job["name"], logger)
            return STATUS_FAILED

        # 6 — navigate to CSV file.  UniSync AUTO-STARTS the job as soon as the
        # CSV finishes loading — selecting it here IS the trigger.
        logger.info("  Selecting CSV file (UniSync auto-starts on load)…")
        _open_panel_go_to_path(csv_path, logger)

        # 7 — settle pause: lets UniSync ingest the CSV and begin the job.
        logger.debug(f"  Settle pause ({POST_CSV_SETTLE}s)…")
        time.sleep(POST_CSV_SETTLE)

        # 8 — wait for completion by watching the client OUTPUT folder.
        # Retry passes use tighter windows: a small known set delivers fast (or
        # not at all, meaning the tracks are absent from the source), so we
        # don't sit through the full first-pass grace.
        if is_retry:
            return _wait_for_job_output(
                job, logger, csv_path=csv_path,
                startup_grace=OUTPUT_RETRY_STARTUP_GRACE,
                stability_window=OUTPUT_RETRY_STABILITY_WINDOW,
                is_retry=True,
            )
        return _wait_for_job_output(job, logger, csv_path=csv_path)

    except _LocateError as exc:
        logger.error(f"  UI element not found during '{job['name']}':\n  {exc}")
        _capture_failure_screenshot(job["name"], logger)
        return STATUS_FAILED
    except Exception as exc:
        logger.error(
            f"  Unexpected error during '{job['name']}': "
            f"{type(exc).__name__}: {exc!r}"
        )
        _capture_failure_screenshot(job["name"], logger)
        return STATUS_FAILED


# ---------------------------------------------------------------------------
# Low-level UI helpers
# ---------------------------------------------------------------------------

def _save_step_screenshot(label: str, logger: logging.Logger) -> None:
    """
    Save a full-screen snapshot to DEBUG_STEP_DIR.  Only fires when the
    module-level CAPTURE_STEPS flag is True (set via --capture-steps CLI).

    Verifies the file actually appears on disk and warns explicitly if not —
    the common cause is macOS Screen Recording permission not being granted
    to Terminal (or whichever app runs python), in which case pyautogui's
    underlying `screencapture` call returns without error AND without a file.
    """
    if not CAPTURE_STEPS:
        return
    try:
        import subprocess
        from datetime import datetime
        DEBUG_STEP_DIR.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now().strftime("%H%M%S")
        safe = "".join(c if c.isalnum() else "_" for c in label)
        path = DEBUG_STEP_DIR / ("step_" + ts + "_" + safe + ".png")

        # Call macOS screencapture directly so we can read its return code
        # and stderr.  -x silences the shutter sound.
        result = subprocess.run(
            ["screencapture", "-x", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if not path.exists() or path.stat().st_size == 0:
            logger.error(
                "    ✗  Screenshot NOT saved (file missing or 0 bytes):\n"
                "       " + str(path) + "\n"
                "       screencapture rc=" + str(result.returncode) + ", stderr=" + result.stderr.strip() + "\n"
                "       This is almost always a missing macOS permission.\n"
                "       Grant Screen Recording to Terminal (or your Python IDE):\n"
                "         System Settings → Privacy & Security → Screen Recording\n"
                "       Then fully QUIT and relaunch Terminal before re-running."
            )
            return

        logger.info("    📷  Step capture saved (" + str(path.stat().st_size) + " bytes): " + str(path))
    except Exception as exc:
        logger.warning("    Could not save step screenshot: " + type(exc).__name__ + ": " + str(exc))


def _capture_failure_screenshot(label: str, logger: logging.Logger) -> None:
    """
    Save a full-screen screenshot to FAILURE_SCREENSHOTS_DIR.  Called from
    the job error handlers so post-mortem inspection is possible without
    re-running the entire job.

    Filename: unisync_failure_{job_label}_{YYYYMMDD-HHMMSS}.png

    Robust capture: we explicitly save the PIL image and then verify the file
    actually exists with non-trivial size.  pyautogui.screenshot(path) has
    been observed to log success yet write nothing on macOS when Screen
    Recording permission is missing, so we (a) save the returned image
    object explicitly, (b) verify it landed, and (c) fall back to the macOS
    `screencapture` CLI, which is more reliable.  We log the TRUE outcome.
    """
    ts   = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() else "_" for c in label).strip("_")
    try:
        FAILURE_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning(f"  Could not create failure-screenshot dir: {exc}")
        return
    path = FAILURE_SCREENSHOTS_DIR / f"unisync_failure_{safe}_{ts}.png"

    def _ok() -> bool:
        try:
            return path.exists() and path.stat().st_size > 1024
        except Exception:
            return False

    # Attempt 1: pyautogui, saving the returned image object explicitly.
    try:
        import pyautogui
        img = pyautogui.screenshot()
        img.save(str(path))
    except Exception as exc:
        logger.warning(f"  pyautogui screenshot failed: {exc}")

    # Attempt 2 (fallback): macOS screencapture CLI.
    if not _ok():
        try:
            subprocess.run(
                ["screencapture", "-x", str(path)],
                capture_output=True, timeout=15,
            )
        except Exception as exc:
            logger.warning(f"  screencapture CLI failed: {exc}")

    if _ok():
        logger.info(f"  📸  Failure screenshot saved: {path}")
    else:
        logger.error(
            "  ⚠ Failure screenshot could NOT be saved (file missing or empty).\n"
            f"     Target was: {path}\n"
            "     This is almost always a macOS permission issue: grant\n"
            "     Terminal (or your shell) BOTH 'Screen & System Audio\n"
            "     Recording' AND 'Accessibility' in System Settings →\n"
            "     Privacy & Security, then fully quit and reopen Terminal.\n"
            "     Without Screen Recording permission, screen captures and\n"
            "     the image-matching that drives UniSync will also fail —\n"
            "     which is the likely root cause of the locator timeouts."
        )


class _LocateError(RuntimeError):
    """Raised when a UI element cannot be found after all retries."""


def _locate_safe(image_path: str):
    """
    Wrapper around pyautogui.locateOnScreen() that returns None on any
    failure — including the ImageNotFoundException raised by newer
    pyautogui versions when no match is found.

    Older pyautogui returned None for misses; newer versions raise.
    Catching everything makes the rest of the code version-agnostic.
    """
    import pyautogui
    try:
        return pyautogui.locateOnScreen(image_path, confidence=LOCATE_CONFIDENCE)
    except Exception:
        return None


def _locate_all_safe(image_path: str) -> list:
    """
    Return on-screen matches as Box objects, sorted top-to-bottom then
    left-to-right, with sub-pixel duplicate matches collapsed.

    pyautogui's image search reports the same UI element multiple times
    when there are anti-aliasing / sub-pixel variations in how it renders
    (10+ near-identical Box hits for a single icon is common).  Without
    deduplication, "click match #2" still clicks the same icon as #1 —
    just a pixel offset to the right.

    Deduplication keeps only the first match in each cluster of boxes
    whose top-left corners fall within DEDUP_PX of each other.
    """
    import pyautogui
    DEDUP_PX = 10  # pixels — boxes closer than this are the same icon

    try:
        matches = list(
            pyautogui.locateAllOnScreen(image_path, confidence=LOCATE_CONFIDENCE)
        )
    except Exception:
        return []

    matches.sort(key=lambda b: (int(b.top), int(b.left)))

    deduped = []
    for m in matches:
        is_dup = any(
            abs(int(m.top)  - int(kept.top))  < DEDUP_PX and
            abs(int(m.left) - int(kept.left)) < DEDUP_PX
            for kept in deduped
        )
        if not is_dup:
            deduped.append(m)
    return deduped


def _img(filename: str) -> str:
    """Return absolute path string for a reference screenshot."""
    return str(SCREENSHOTS_DIR / filename)


def _locate_nth_and_click(
    filename: str,
    logger: logging.Logger,
    nth: int = 0,
) -> None:
    """
    Find every on-screen match for the reference image, sort top-to-bottom,
    and click the (nth)-from-top match.

    Use this when a single screenshot crop matches multiple identical UI
    elements (e.g. two folder-icon buttons stacked in the same panel)
    and we need to disambiguate by position.

        cache button   = nth=0   (topmost folder icon)
        client button  = nth=1   (second folder icon)
    """
    import pyautogui

    for attempt in range(1, LOCATE_RETRIES + 1):
        matches = _locate_all_safe(_img(filename))
        if matches and len(matches) > nth:
            target = matches[nth]
            center = pyautogui.center(target)
            logger.info(
                f"    Clicking {filename} match #{nth+1}/{len(matches)} "
                f"@ ({center.x}, {center.y})  box={target}  attempt={attempt}"
            )
            pyautogui.click(center.x, center.y)
            time.sleep(POST_CLICK_WAIT)
            return
        if matches:
            logger.debug(
                f"    {filename}: only {len(matches)} match(es) found, "
                f"need at least {nth+1} (attempt {attempt}/{LOCATE_RETRIES})"
            )
        else:
            logger.debug(
                f"    {filename} not found (attempt {attempt}/{LOCATE_RETRIES})"
            )
        time.sleep(LOCATE_DELAY)

    raise _LocateError(
        f"'{filename}' did not produce at least {nth+1} match(es) on screen "
        f"after {LOCATE_RETRIES} attempts.\n"
        f"  If only one icon was matched but two are expected, re-crop the\n"
        f"  screenshot more tightly so it matches BOTH folder icons equally."
    )


def _locate_and_click(filename: str, logger: logging.Logger) -> None:
    """
    Find a UI element by reference image and click its centre.

    Uses plain pyautogui.click() (which works for popup buttons, regular
    buttons, and menu items).  Held-click patterns break NSPopUpButton —
    it interprets long mouseDown as menu-tracking mode and dismisses on
    mouseUp.  Plain click works.

    Coordinates are logged at INFO level so we can verify the script is
    clicking the right element on screen.  If a reference screenshot is
    matching the wrong UI element, the (x, y) will reveal it.
    """
    import pyautogui

    for attempt in range(1, LOCATE_RETRIES + 1):
        loc = _locate_safe(_img(filename))
        if loc:
            center = pyautogui.center(loc)
            logger.info(
                f"    Clicking {filename} @ ({center.x}, {center.y})  "
                f"box={loc}  attempt={attempt}"
            )
            pyautogui.click(center.x, center.y)
            time.sleep(POST_CLICK_WAIT)
            return
        logger.debug(
            f"    {filename} not found (attempt {attempt}/{LOCATE_RETRIES})"
        )
        time.sleep(LOCATE_DELAY)

    raise _LocateError(
        f"'{filename}' not found on screen after {LOCATE_RETRIES} attempts.\n"
        f"  Ensure UniSync is visible and unobscured.  Check that the crop\n"
        f"  in {SCREENSHOTS_DIR} still matches the current UI."
    )


def _unisync_is_running() -> bool:
    """True if a process named exactly 'UniSync' is running."""
    r = subprocess.run(["pgrep", "-x", UNISYNC_APP], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def _quit_unisync(logger: logging.Logger, timeout: float = 20.0) -> None:
    """
    Quit UniSync gracefully and wait for the process to exit.  Between jobs
    UniSync is idle (the previous job has finished), so this is a clean quit.
    If it doesn't exit within `timeout`, escalate to a forced kill.
    """
    if not _unisync_is_running():
        logger.debug("  UniSync not running; nothing to quit.")
        return
    logger.info("  Quitting UniSync (to reload prefs at next launch)…")
    subprocess.run(
        ["osascript", "-e", f'tell application "{UNISYNC_APP}" to quit'],
        capture_output=True, text=True,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _unisync_is_running():
            time.sleep(1.0)   # let the OS finish tearing it down
            logger.debug("  UniSync quit.")
            return
        time.sleep(0.5)
    logger.warning("  UniSync did not quit in time — forcing it to close.")
    subprocess.run(["pkill", "-x", UNISYNC_APP], capture_output=True, text=True)
    time.sleep(2.0)


def _launch_unisync(logger: logging.Logger, timeout: float = 30.0) -> None:
    """Launch UniSync and wait until the process is up, then settle."""
    logger.info("  Launching UniSync…")
    subprocess.Popen(["open", "-a", UNISYNC_APP])
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _unisync_is_running():
            break
        time.sleep(0.5)
    time.sleep(LAUNCH_WAIT)   # extra settle for the window/UI to be ready


def _relaunch_unisync_with_xml(job: dict, logger: logging.Logger) -> bool:
    """
    Configure a job WITHOUT the path-entry UI: quit UniSync, write the job's
    Territory/Cache/Client into UniSync.xml, relaunch (UniSync reads the prefs
    at launch), and bring it to the front.  Returns False if the XML write
    fails.
    """
    from unisync_prefs import write_unisync_xml_prefs

    logger.info(
        f"  Configuring UniSync for '{job['name']}' via UniSync.xml "
        f"(quit → write prefs → relaunch)…"
    )
    _quit_unisync(logger)
    ok = write_unisync_xml_prefs(
        job["territory"], job["cache_path"], job["client_path"],
        xml_path=UNISYNC_XML_PATH, logger=logger, dry_run=False,
    )
    if not ok:
        logger.error(
            "  ✗  Could not write UniSync.xml — falling back is required.\n"
            "     Check that the file exists at:\n"
            f"     {UNISYNC_XML_PATH}"
        )
        return False
    _launch_unisync(logger)
    _activate_unisync(logger)   # front + clear any stray modal
    return True


def _activate_unisync(logger: logging.Logger) -> None:
    """
    Bring UniSync to the foreground using AppleScript activate.
    If the app isn't running, fall back to 'open -a' and wait for launch.
    Note: AppleScript activate works even without a dictionary.

    After activation, send two Escape keystrokes to dismiss any modal
    that a previous aborted run may have left open (an unmounted-volume
    failure typically leaves a Go-to-Folder popup wedged inside the
    NSOpenPanel — that has to be cleared or every subsequent click
    lands on the dialog instead of the UniSync main window).
    """
    import pyautogui

    script = f'tell application "{UNISYNC_APP}" to activate'
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.info(f"  Launching {UNISYNC_APP}…")
        subprocess.Popen(["open", "-a", UNISYNC_APP])
        time.sleep(LAUNCH_WAIT)
    else:
        time.sleep(1.0)

    # Defensive cleanup: dismiss any stuck modals from a previous run.
    # Two Escapes — first to close any Go-to-Folder popup inside an
    # NSOpenPanel, second to cancel the NSOpenPanel itself.
    pyautogui.press("escape")
    time.sleep(0.3)
    pyautogui.press("escape")
    time.sleep(0.3)

    logger.debug("  UniSync is active.")


def _set_territory(territory: str, logger: logging.Logger) -> None:
    """
    Open the Territory dropdown and select the option matching `territory`.

    Each job has a specific required territory — setting the wrong one
    causes UniSync to fetch tracks from the wrong source.  Mapping:

        US MP3           → United States (MP3)
        US WAV           → United States
        US WAV w COVERS  → United States
        Ex-US MP3        → Rest of World (MP3)
        Ex-US WAV        → Rest of World
        Japan WAV        → Japan
    """
    option_img = TERRITORY_SCREENSHOTS.get(territory)
    if option_img is None:
        raise RuntimeError(
            f"No screenshot mapping for territory: {territory!r}.  "
            f"Known: {list(TERRITORY_SCREENSHOTS.keys())}"
        )

    logger.info(f"  Setting Territory → {territory}")

    # 1. Click the dropdown to open it
    _locate_and_click("unisync_territory_dropdown.png", logger)
    time.sleep(0.6)  # let the option list render

    # 2. Click the desired option from the open dropdown
    _locate_and_click(option_img, logger)
    time.sleep(POST_CLICK_WAIT)


def _set_clipboard(text: str, logger: logging.Logger) -> bool:
    """
    Put `text` on the macOS clipboard via pbcopy.  Returns True on success.

    pbcopy is built into macOS, needs no extra Python deps, and handles
    every character literally — no keyboard layout or Shift-timing issues.
    """
    try:
        proc = subprocess.run(
            ["pbcopy"],
            input=text.encode("utf-8"),
            check=True,
            timeout=5,
        )
        return proc.returncode == 0
    except Exception as exc:
        logger.error(f"    pbcopy failed: {exc}")
        return False


def _screen_fingerprint():
    """
    Return a small grayscale screenshot as a numpy array for cheap
    screen-change detection.  Crops out the right ~third of the screen
    (where the terminal/log window sits and scrolls) so its churn doesn't
    register as a change; the UniSync window and any centered dialog are
    in the left portion that we keep.
    """
    import numpy as np
    import pyautogui

    shot = pyautogui.screenshot().convert("L")
    w, h = shot.size
    shot = shot.crop((0, 0, int(w * 0.65), h)).resize((160, 120))
    return np.asarray(shot, dtype=np.int16)


def _open_csv_picker(logger: logging.Logger) -> bool:
    """
    Open UniSync's 'Choose a csv of workaudioids' file picker, VERIFYING that a
    dialog actually appeared.  Retries the hamburger-menu → menu-item
    sequence until a dialog is detected or attempts are exhausted.

    Returns True once the picker is open, False if it never opened.

    Detection: snapshot the screen BEFORE opening the menu (the plain main
    window), then after clicking the menu item.  An open file dialog is a
    large overlay → big mean-pixel difference from the main window; if the
    menu merely closed without opening a dialog the screen looks ~identical
    to the baseline → small difference → retry.
    """
    import numpy as np
    import pyautogui

    baseline = _screen_fingerprint()  # main window, nothing open

    for attempt in range(1, CSV_PICKER_MAX_ATTEMPTS + 1):
        logger.info(
            f"  Opening menu (attempt {attempt}/{CSV_PICKER_MAX_ATTEMPTS})…"
        )
        _locate_and_click("unisync_hamburger_btn.png", logger)
        time.sleep(POST_CLICK_WAIT)

        logger.info("  Selecting 'Choose a csv of workaudioids'…")
        _locate_and_click("unisync_choose_csv.png", logger)
        time.sleep(max(DIALOG_OPEN_WAIT, 2.0))

        after = _screen_fingerprint()
        diff = float(np.mean(np.abs(after - baseline)))
        logger.info(
            f"    Post-click screen change: {diff:.1f} "
            f"(need > {DIALOG_OPEN_DIFF_THRESHOLD} for an open dialog)"
        )
        if diff > DIALOG_OPEN_DIFF_THRESHOLD:
            logger.info("  ✓  CSV file picker is open.")
            return True

        logger.warning(
            "    No dialog detected — the menu didn't open or the menu-item "
            "click missed the row.  Pressing Escape and retrying."
        )
        pyautogui.press("escape")  # clear any half-open menu state
        time.sleep(0.6)

    return False


def _open_panel_go_to_path(path: str, logger: logging.Logger) -> None:
    """
    Inside an open macOS NSOpenPanel, navigate to `path` via Cmd+Shift+G.

    The path is delivered by CLIPBOARD PASTE, not by typing.  Two reasons:
      • Typing with pyautogui.write() drops characters and corrupts shifted
        symbols — observed live as "/Volumes/..." landing as "/Volue/".
      • The Go-to field opens PRE-POPULATED with the current location (e.g.
        "VM").  Cmd+A selects that pre-filled text, then Cmd+V pastes the
        path OVER the selection, replacing it.  The whole string lands
        atomically, regardless of keyboard layout or symbol density.

    Flow: Cmd+Shift+G → Cmd+A (select pre-filled text) → set clipboard →
    Cmd+V (replace) → Enter (navigate) → Enter (confirm/open).

    Each phase saves a step screenshot when CAPTURE_STEPS is on, so we
    can see exactly where path entry breaks down if it ever does.
    """
    import pyautogui

    # 1. Let the open panel animate in and gain focus.
    time.sleep(max(DIALOG_OPEN_WAIT, 2.5))
    _save_step_screenshot("01_dialog_open", logger)

    # 2. Cmd+Shift+G — opens the Go to Folder sheet inside the panel.
    pyautogui.hotkey("command", "shift", "g")
    time.sleep(1.2)
    _save_step_screenshot("02_after_cmd_shift_g", logger)

    # 3. Cmd+A — select any pre-filled text in the path field so the paste
    #    replaces it (the field can open holding the current location).
    pyautogui.hotkey("command", "a")
    time.sleep(0.2)

    # 4. Deliver the path via clipboard paste (immune to Shift-timing and
    #    dropped characters).  Fall back to typing only if pbcopy fails.
    if _set_clipboard(path, logger):
        time.sleep(0.15)  # let the pasteboard settle
        pyautogui.hotkey("command", "v")
    else:
        logger.warning(
            "    Clipboard unavailable — falling back to typing the path "
            "(special characters may be unreliable)."
        )
        pyautogui.write(path, interval=0.04)
    time.sleep(0.5)
    _save_step_screenshot("03_after_typing_path", logger)

    # 5. First Enter — navigates the panel to the pasted path.
    pyautogui.press("enter")
    time.sleep(0.9)
    _save_step_screenshot("04_after_first_enter", logger)

    # 6. Second Enter — clicks the Open button, closing the panel.
    pyautogui.press("enter")
    time.sleep(0.9)
    _save_step_screenshot("05_after_second_enter", logger)

    logger.debug(f"    NSOpenPanel → {path}")


def _set_path_field(
    btn_screenshot: str,
    path: str,
    logger: logging.Logger,
    nth: int = 0,
) -> None:
    """
    Click the nth folder-picker button (top-to-bottom) and navigate
    the resulting dialog to `path` via Cmd+Shift+G.

    nth=0 = topmost folder icon (CACHE in UniSync's settings panel)
    nth=1 = second-from-top    (CLIENT)
    """
    _locate_nth_and_click(btn_screenshot, logger, nth=nth)
    _open_panel_go_to_path(path, logger)


def _filenames_to_workaudioids(
    csv_path: str, ext: str, filenames: set[str], logger: logging.Logger
) -> list[str]:
    """
    Map a set of output leaf filenames back to their workAudioIds using the
    job's CSV.  Used to report exactly which tracks UniSync didn't deliver,
    matching the "Not found (N): …" list UniSync shows in its console.
    Returns a sorted list of workAudioId strings (empty if the column or file
    can't be read).
    """
    import csv as _csv

    if not filenames:
        return []
    suffix = ext.lower()
    ids: list[str] = []
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = _csv.DictReader(f)
            fields = reader.fieldnames or []
            fn_col = wid_col = None
            for c in fields:
                key = c.strip().lower().replace(" ", "").replace("_", "")
                if key in ("filename", "file") and fn_col is None:
                    fn_col = c
                elif key in ("workaudioid", "workaudioids") and wid_col is None:
                    wid_col = c
            if not fn_col or not wid_col:
                return []
            for row in reader:
                name = (row.get(fn_col) or "").strip()
                if not name:
                    continue
                if not name.lower().endswith(suffix):
                    name = name + ext
                if name in filenames:
                    wid = (row.get(wid_col) or "").strip()
                    if wid:
                        ids.append(wid)
    except Exception as exc:
        logger.debug(f"    Could not map filenames→workAudioIds: {exc}")
        return []
    # numeric sort when possible, else lexical
    try:
        ids.sort(key=lambda x: int(x))
    except ValueError:
        ids.sort()
    return ids


def _report_not_found(
    job: dict, missing: set[str], ext: str, logger: logging.Logger
) -> None:
    """
    Report tracks UniSync did not deliver.  Now that the download bug is fixed,
    files that never arrive are 'not found' in the UPM source — typically
    de-activated or un-published since the export CSV was made.  We surface the
    workAudioIds (matching UniSync's on-screen "Not found (N): …") so the user
    knows to refresh the export and re-run if they should still exist.
    """
    n = len(missing)
    ids = _filenames_to_workaudioids(job["csv"], ext, missing, logger)
    logger.warning(
        f"  ⚠  {n} track(s) not delivered for '{job['name']}' — UniSync reports "
        f"these as NOT FOUND in UPM."
    )
    if ids:
        shown = ", ".join(ids[:60]) + (f"  (+{len(ids) - 60} more)" if len(ids) > 60 else "")
        logger.warning(f"     workAudioIds: {shown}")
    logger.warning(
        "     This usually means they were de-activated or un-published in UPM\n"
        "     since the export was made.  Refresh the export CSV (Step 1 Domo)\n"
        "     and re-run Step 5 if these tracks should still exist; otherwise\n"
        "     they can be ignored."
    )


def _expected_output_filenames(
    csv_path: str, ext: str, logger: logging.Logger
) -> set[str]:
    """
    Read the job's CSV and return the set of output leaf filenames it
    should produce, e.g. {"BR_848_1_Foo.wav", ...}.

    The Filename column is "Filename" in every UPM CSV (US, Ex-US, and the
    Japan NTT metadata).  Tracklists store the bare basename; Japan stores
    the name with ".wav" already appended — so we only add `ext` when it's
    not already present (case-insensitive).
    """
    import csv as _csv

    expected: set[str] = set()
    suffix = ext.lower()
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = _csv.DictReader(f)
            fields = reader.fieldnames or []
            fn_col = None
            for c in fields:
                if c.strip().lower().replace(" ", "") in ("filename", "file"):
                    fn_col = c
                    break
            if not fn_col:
                logger.warning(
                    f"    No 'Filename' column in {Path(csv_path).name}; "
                    f"cannot track delivery by filename."
                )
                return expected
            for row in reader:
                name = (row.get(fn_col) or "").strip()
                if not name:
                    continue
                if not name.lower().endswith(suffix):
                    name = name + ext
                expected.add(name)
    except Exception as exc:
        logger.warning(f"    Could not read expected files from {csv_path}: {exc}")
    return expected


def _count_present(client_path: str, expected: set[str]) -> int:
    """
    Count how many of `expected` filenames currently exist anywhere under
    client_path (recursive).  Matches by leaf filename only.
    """
    import os

    if not expected:
        return 0
    found: set[str] = set()
    for _root, _dirs, files in os.walk(client_path):
        for f in files:
            if f in expected:
                found.add(f)
                if len(found) == len(expected):
                    return len(found)
    return len(found)


def _present_filenames(client_path: str, expected: set[str]) -> set[str]:
    """
    Return the subset of `expected` leaf filenames that currently exist
    anywhere under client_path (recursive).  Used to compute which files
    still need delivering so a retry can fetch ONLY those.
    """
    import os

    if not expected:
        return set()
    found: set[str] = set()
    for _root, _dirs, files in os.walk(client_path):
        for f in files:
            if f in expected:
                found.add(f)
    return found


def _write_reduced_csv(
    orig_csv: str,
    ext: str,
    missing_names: set[str],
    attempt: int,
    logger: logging.Logger,
) -> str | None:
    """
    Write a copy of `orig_csv` containing ONLY the rows whose output file is
    in `missing_names`, so a retry asks UniSync for just the undelivered
    files instead of the whole tracklist.  Preserves the original header and
    column order.  Returns the new CSV path, or None if it couldn't be built
    (no matching rows / no Filename column / write error) — in which case the
    caller should fall back to the full CSV or stop.

    The reduced CSV is written to a TEMP directory (not beside the original),
    as "<stem>_req{attempt}<suffix>", so the user's tracklist folder is never
    cluttered.  The caller deletes these when the job finishes.
    """
    import csv as _csv

    suffix = ext.lower()
    try:
        with open(orig_csv, encoding="utf-8-sig", newline="") as f:
            reader = _csv.DictReader(f)
            fields = reader.fieldnames or []
            fn_col = None
            for c in fields:
                if c.strip().lower().replace(" ", "") in ("filename", "file"):
                    fn_col = c
                    break
            if not fn_col:
                logger.warning(
                    "    Cannot build a reduced retry CSV (no Filename column); "
                    "will re-run the full CSV instead."
                )
                return None
            kept = []
            for row in reader:
                name = (row.get(fn_col) or "").strip()
                if not name:
                    continue
                out = name if name.lower().endswith(suffix) else name + ext
                if out in missing_names:
                    kept.append(row)
    except Exception as exc:
        logger.warning(f"    Could not read {orig_csv} to build retry CSV: {exc}")
        return None

    if not kept:
        return None

    import tempfile
    # Write retry/request CSVs to a temp directory, NOT beside the user's
    # tracklist — they're transient and shouldn't clutter the source paths.
    tmp_dir = Path(tempfile.gettempdir()) / "upm_unisync_csv"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        tmp_dir = Path(tempfile.gettempdir())
    p = Path(orig_csv)
    reduced = tmp_dir / f"{p.stem}_req{attempt}{p.suffix}"
    try:
        with open(reduced, "w", encoding="utf-8-sig", newline="") as f:
            writer = _csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(kept)
    except Exception as exc:
        logger.warning(f"    Could not write reduced request CSV: {exc}")
        return None

    logger.info(
        f"    Built request CSV with {len(kept)} file(s) (temp): {reduced.name}"
    )
    return str(reduced)


def _newest_mtime_above(folder: str, threshold: float) -> float:
    """
    Walk `folder` recursively and return the newest file mtime strictly above
    `threshold`, or `threshold` itself if nothing newer exists.

    This is the heart of the activity check: files written by UniSync during
    this job will have fresh mtimes (above the job-start threshold), while
    files left over from previous runs will have old mtimes and be ignored.
    """
    import os

    newest = threshold
    if not os.path.isdir(folder):
        return newest
    try:
        for root, _dirs, files in os.walk(folder):
            for f in files:
                try:
                    mt = os.stat(os.path.join(root, f)).st_mtime
                    if mt > newest:
                        newest = mt
                except OSError:
                    continue
    except OSError:
        pass
    return newest


def _wait_for_job_output(
    job: dict, logger: logging.Logger, csv_path: str | None = None,
    startup_grace: int | None = None, stability_window: int | None = None,
    is_retry: bool = False,
) -> str:
    """
    Wait for a UniSync job to finish by tracking the DELIVERED COUNT — how many
    of the CSV's expected output files are present and growing — rather than the
    client folder's modification time.

    Why the count and not folder mtime?  UniSync (and the OS) can touch files in
    the client folder without delivering any NEW expected file — re-stamping
    existing files, temp writes, etc.  Watching mtime treats that phantom
    activity as "still downloading" and makes the job sit through the full
    stability window even when nothing is actually arriving.  Tracking the
    count of expected files means "still downloading" == "the count is going
    up"; pre-existing files are captured as the initial baseline and don't count
    as new deliveries.

    Completion paths (all measured on the count):
      (a0) every expected file present                     ⇒ done after a short
           settle (OUTPUT_COMPLETE_SETTLE).
      (a)  delivered something this pass (or files were already present) AND the
           count hasn't increased for `stable` seconds      ⇒ done with whatever
           is present (stragglers handled by retry/verification).
      (b)  cold start — nothing present, nothing delivered within `grace`
           ⇒ FAILED on a first pass (CSV didn't load), or done/not-found on a
           retry.
      (c)  JOB_TIMEOUT elapsed                              ⇒ FAILED.
    """
    import os

    ext = ".mp3" if "MP3" in job["name"].upper() else ".wav"
    client = job["client_path"]
    expected = _expected_output_filenames(csv_path or job["csv"], ext, logger)
    total = len(expected)

    # Retry passes pass tighter windows; otherwise use the first-pass defaults.
    grace  = OUTPUT_STARTUP_GRACE   if startup_grace   is None else startup_grace
    stable = OUTPUT_STABILITY_WINDOW if stability_window is None else stability_window

    if total == 0:
        logger.warning(
            "    Expected-file set is empty — waiting a fixed interval, then "
            "deferring to verification."
        )
        time.sleep(stable)
        return STATUS_OK

    job_start = time.time()
    initial_have = _count_present(client, expected)
    logger.info(
        f"  Monitoring UniSync activity for {total} expected {ext} file(s)…"
    )
    logger.info(
        f"    Pre-existing in client folder: {initial_have}/{total} "
        f"(not counted as deliveries — only writes since now are tracked)."
    )

    deadline      = job_start + JOB_TIMEOUT
    last_have     = initial_have
    last_delivery = job_start     # time the DELIVERED COUNT last increased
    last_log      = job_start

    while time.time() < deadline:
        now = time.time()

        # Completion is driven by the DELIVERED COUNT, not raw folder mtime.
        # "Still downloading" means new expected files are still landing; once
        # the count stops moving the job is done.  Phantom writes (UniSync
        # re-touching files, temp files) no longer keep the job alive when no
        # new expected file has appeared.
        have = _count_present(client, expected)
        if have > last_have:
            last_delivery = now
            logger.info(f"    Delivered {have}/{total} ({have / total * 100:.0f}%).")
            last_have = have

        elapsed       = now - job_start
        idle          = now - last_delivery     # seconds since the count moved
        seen_delivery = have > initial_have

        # (a0) Everything is present → done after a short settle.
        if have >= total and idle > OUTPUT_COMPLETE_SETTLE:
            logger.info(
                f"  ✓  All {total} expected {ext} files present — job complete."
            )
            logger.info(f"    Final delivery: {have}/{total}")
            return STATUS_OK

        # (a) The delivered count has STALLED — no new file for `stable`
        # seconds — and UniSync is past any cold pre-scan (it either delivered
        # something this pass, or files were already present).  Whatever is in
        # the folder is all that's coming.
        if (seen_delivery or initial_have > 0) and idle > stable:
            logger.info(
                f"  ✓  No new files for {int(idle)}s — job complete "
                f"({have}/{total} delivered)."
            )
            if have < total:
                logger.warning(f"    {total - have} file(s) not delivered.")
            return STATUS_OK

        # (b) Cold start: nothing was present and nothing has been delivered
        # within the startup grace.  On a retry that means the requested tracks
        # aren't in the source; on a first pass it means the CSV didn't load.
        if initial_have == 0 and not seen_delivery and elapsed > grace:
            if is_retry:
                logger.info(
                    f"  ✓  Nothing delivered in {int(elapsed)}s — the "
                    f"{total - have}/{total} requested are not in the source."
                )
                return STATUS_OK
            logger.error(
                f"  ✗  No files delivered in {int(grace // 60)}m and "
                f"{have}/{total} present — the job did not start.\n"
                f"     The CSV probably didn't load.\n"
                f"     Client folder: {client}"
            )
            _capture_failure_screenshot(job["name"], logger)
            return STATUS_FAILED

        # Heartbeat every ~30s.
        if now - last_log > 30:
            tag = (f"last new file {int(idle)}s ago"
                   if (seen_delivery or have > 0) else "no deliveries yet")
            logger.info(
                f"    [{int(elapsed)}s] {have}/{total} delivered, {tag}."
            )
            last_log = now

        time.sleep(OUTPUT_POLL_INTERVAL)

    hrs = JOB_TIMEOUT / 3600
    logger.error(
        f"  ✗  Timed out after {hrs:.1f}h at {last_have}/{total} delivered: "
        f"{job['name']}\n"
        f"     UniSync may still be running — check its UI.  For very large "
        f"catalogs, retry with --timeout {hrs * 2:.0f}."
    )
    return STATUS_FAILED


# ---------------------------------------------------------------------------
# Standalone test entry point
# ---------------------------------------------------------------------------

def _run_test(args) -> None:
    """
    Run UniSync end-to-end for a single job or for all configured jobs.

    Use --job to test one job before committing to the full sequence.
    """
    import sys
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("unisync_test")

    ctx = ReleaseContext(year=args.year, month=args.month, part=args.part)
    logger.info(f"Release context: {ctx}")
    logger.info(f"  dry_run: {args.dry_run}")

    # Screenshot preflight
    if not verify_screenshots(logger):
        sys.exit(1)

    # Filter to single job if requested
    if args.job:
        matches = [j for j in ctx.unisync_jobs if j["name"].lower() == args.job.lower()]
        if not matches:
            logger.error(
                f"No UniSync job named {args.job!r}.\n"
                f"  Available: {[j['name'] for j in ctx.unisync_jobs]}"
            )
            sys.exit(1)
        jobs_to_run = matches
        logger.info(f"Running single job: {matches[0]['name']}")
    elif args.start_from:
        names_lower = [j["name"].lower() for j in ctx.unisync_jobs]
        if args.start_from.lower() not in names_lower:
            logger.error(
                f"No UniSync job named {args.start_from!r}.\n"
                f"  Available: {[j['name'] for j in ctx.unisync_jobs]}"
            )
            sys.exit(1)
        start_idx = names_lower.index(args.start_from.lower())
        jobs_to_run = ctx.unisync_jobs[start_idx:]
        skipped_names = [j["name"] for j in ctx.unisync_jobs[:start_idx]]
        logger.info(
            f"Resuming from job {start_idx + 1}/{len(ctx.unisync_jobs)}: "
            f"{ctx.unisync_jobs[start_idx]['name']}"
        )
        if skipped_names:
            logger.info(f"  Skipping previously-completed: {skipped_names}")
    else:
        jobs_to_run = ctx.unisync_jobs
        logger.info(f"Running all {len(jobs_to_run)} jobs sequentially.")

    # Run
    results: dict[str, str] = {}
    if args.dry_run:
        for job in jobs_to_run:
            logger.info(
                f"\n[DRY RUN] {job['name']}\n"
                f"  Cache:  {job['cache_path']}\n"
                f"  Client: {job['client_path']}\n"
                f"  CSV:    {job['csv']}"
            )
            results[job["name"]] = STATUS_SKIPPED
    else:
        try:
            import pyautogui  # noqa: F401
        except ImportError:
            logger.error("pyautogui not installed.  Run: pip install pyautogui Pillow")
            sys.exit(1)

        for i, job in enumerate(jobs_to_run):
            logger.info(f"\n{'─' * 52}")
            logger.info(f"Job {i + 1}/{len(jobs_to_run)}: {job['name']}")
            logger.info(f"  Cache:  {job['cache_path']}")
            logger.info(f"  Client: {job['client_path']}")
            logger.info(f"  CSV:    {job['csv']}")
            status = _run_single_job(
                job, dry_run=False, logger=logger, overwrite=args.overwrite
            )
            results[job["name"]] = status

            if status == STATUS_FAILED:
                logger.error(f"Stopping — '{job['name']}' failed.")
                for remaining in jobs_to_run[i + 1:]:
                    results[remaining["name"]] = STATUS_FAILED
                break

    logger.info("\n" + "─" * 52)
    logger.info("Summary:")
    for name, status in results.items():
        sym = {"ok": "✓", "skipped": "—", "failed": "✗"}.get(status, "?")
        logger.info(f"  {sym}  {name}: {status}")

    sys.exit(0 if all(s in ("ok", "skipped") for s in results.values()) else 1)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Run UniSync export jobs.")
    p.add_argument("--test",    action="store_true", required=True)
    p.add_argument("--year",    type=int, required=True)
    p.add_argument("--month",   type=int, required=True)
    p.add_argument("--part",    type=int, choices=[1, 2], required=True)
    p.add_argument("--job",     default=None,
                   help="Run a single job by name (e.g. 'US MP3').  "
                        "Omit to run all six.")
    p.add_argument("--start-from", default=None,
                   help="Resume from a specific job by name, skipping all "
                        "earlier jobs.  Useful for re-running after a "
                        "partial failure.  Mutually exclusive with --job.")
    p.add_argument("--dry-run", action="store_true",
                   help="Log paths without driving the UI.")
    p.add_argument("--overwrite", action="store_true",
                   help="Force every job to run even if all its expected "
                        "output files are already present in the client "
                        "folder.  Default behaviour skips jobs whose work "
                        "is already done (consistent with the rest of the "
                        "pipeline's --overwrite semantics).")
    p.add_argument("--debug",   action="store_true")
    p.add_argument("--capture-steps", action="store_true",
                   help="Save a screenshot at every step of path entry.  "
                        "Use this when paths aren't actually changing in "
                        "UniSync so we can see what dialog (if any) appears.")
    p.add_argument("--timeout", type=float, default=None, metavar="HOURS",
                   help=f"Per-job timeout in hours.  Default: "
                        f"{JOB_TIMEOUT / 3600:.0f}.  Raise this for very "
                        f"large catalogs that might take many hours to "
                        f"download (e.g. --timeout 12).")

    args = p.parse_args()

    if args.job and args.start_from:
        p.error("--job and --start-from are mutually exclusive.")

    if args.timeout is not None:
        if args.timeout <= 0:
            p.error("--timeout must be positive")
        JOB_TIMEOUT = int(args.timeout * 3600)

    # Promote the CLI flag to the module-level toggle that
    # _save_step_screenshot reads.  We are at module scope inside this
    # 'if __name__ == "__main__":' block, so a plain assignment updates
    # the running module's globals — which IS the function's __globals__.
    if args.capture_steps:
        CAPTURE_STEPS = True

    _run_test(args)