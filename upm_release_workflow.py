"""
upm_release_workflow.py — UPM Release Workflow Orchestrator
============================================================
Main CLI entry point. Runs the full UPM twice-monthly release workflow
from Domo exports through final packaging.

Usage:
    python upm_release_workflow.py --year 2026 --month 5 --part 1
    python upm_release_workflow.py --year 2026 --month 5 --part 2 --dry-run
    python upm_release_workflow.py --year 2026 --month 5 --part 1 \\
        --skip-domo --skip-unisync --skip-soundminer

Optional flags:
    --dry-run                     Print what would happen; no writes or copies
    --overwrite                   Replace existing destination folders/files
    --skip-domo                   Skip Step 1 (Domo exports)
    --skip-folder-setup           Skip Steps 2 & 3 (folder creation)
    --skip-album-list-doc         Skip Step 4 (DOCX/PDF album list)
    --skip-unisync                Skip Step 5 (UniSync jobs)
    --skip-covers                 Skip Steps 6–8 (cover download & distribution)
    --skip-verify                 Skip Step 9 (file verification)
    --skip-final-packaging        Skip Step 10 (copy originals to finals)
    --skip-sourceaudio            Skip Step 11 (SourceAudio AIFF build)
    --skip-soundminer             Skip Step 12 (Soundminer NBC workflow)
    --skip-nbc-mirror             Step 12: skip the embed+mirror, resume at 12.7
    --skip-non-maintrack-cleanup  Skip Step 13 (non-maintrack removal)
    --skip-rename                 Skip Step 14 (NBC filename rename)
    --skip-final-metadata-check   Skip Step 15 (final metadata cross-check)
    --skip-soundmouse             Skip Step 16 (SoundMouse delivery)
    --start-at STEP / --only STEP Resume at / run only one step (see step list)
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

from config import (
    BASELINE_HD_FINAL,
    BASELINE_HD_STAGING,
    BASELINE_SPECIALS,
    DOCX_TO_PDF_METHODS,
    REQUIRED_APPS,
    VOLUMES,
    ReleaseContext,
)
from logging_utils import (
    get_logger,
    log_section,
    log_step_end,
    log_step_skipped,
    log_step_start,
    summarise_results,
)


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

def run_preflight(ctx: ReleaseContext, logger, args=None) -> bool:
    """
    Verify required volumes, baseline paths, and applications exist.
    Returns True if all checks pass; False otherwise (workflow must abort).
    """
    log_section(logger, "Preflight Checks")
    ok = True

    # -- Mounted volumes ------------------------------------------------------
    for vol_key, vol_path in VOLUMES.items():
        if vol_path.exists():
            logger.info(f"  ✓  Volume {vol_key}: {vol_path}")
        else:
            logger.error(
                f"  ✗  Volume {vol_key} NOT MOUNTED: {vol_path}\n"
                f"     Mount the Pegasus32 drives and retry."
            )
            ok = False

    # -- Baseline source folders ----------------------------------------------
    baselines = {
        "Specials baseline":    BASELINE_SPECIALS,
        "HD Staging baseline":  BASELINE_HD_STAGING,
        "HD Final baseline":    BASELINE_HD_FINAL,
    }
    for name, path in baselines.items():
        if path.exists():
            logger.info(f"  ✓  {name}: {path}")
        else:
            logger.error(f"  ✗  {name} NOT FOUND: {path}")
            ok = False

    def _active(skip_attr: str) -> bool:
        return args is None or not bool(getattr(args, skip_attr, False))

    # -- Applications ---------------------------------------------------------
    from config import SOUNDMINER_AGENT_ENABLED, is_soundminer_machine
    for app_name, app_path in REQUIRED_APPS.items():
        if app_path.startswith("/"):
            found = Path(app_path).exists()
        else:
            found = shutil.which(app_path) is not None
        required = (
            (app_name == "UniSync" and _active("skip_unisync"))
            or (
                app_name == "Soundminer v5Pro"
                and is_soundminer_machine()
                and (
                    _active("skip_sourceaudio")
                    or (
                        _active("skip_soundminer")
                        and not bool(getattr(args, "skip_nbc_mirror", False))
                    )
                )
            )
            or (app_name == "ffmpeg" and _active("skip_soundminer"))
        )
        symbol = "✓" if found else ("✗" if required else "!")
        level_fn = logger.info if found else (logger.error if required else logger.warning)
        level_fn(
            f"  {symbol}  App: {app_name}  "
            f"({'found' if found else 'NOT FOUND — required by an active step' if required else 'not needed by selected steps'})"
        )
        if required and not found:
            ok = False

    # -- DOCX-to-PDF converter ------------------------------------------------
    pdf_converter = None
    for method in DOCX_TO_PDF_METHODS:
        if shutil.which(method):
            pdf_converter = method
            break
    if pdf_converter:
        logger.info(f"  ✓  DOCX→PDF converter: {pdf_converter}")
    else:
        required = _active("skip_album_list_doc")
        (logger.error if required else logger.warning)(
            f"  !  No DOCX→PDF converter found "
            f"(tried: {', '.join(DOCX_TO_PDF_METHODS)}).\n"
            f"     Step 4 will fail unless LibreOffice is installed."
        )
        if required:
            ok = False

    # -- Python package dependencies -----------------------------------------
    # import-name -> (pip-name, why, relevant skip flags).  A dependency is a
    # hard failure only when at least one of its consuming steps is active.
    _REQUIRED_IMPORTS = {
        "pandas":     ("pandas", "CSV/XLSX tracklists & metadata", ("skip_domo", "skip_covers", "skip_verify", "skip_final_metadata_check")),
        "openpyxl":   ("openpyxl", "XLSX exports and ingest forms", ("skip_domo", "skip_final_packaging", "skip_soundmouse")),
        "docx":       ("python-docx", "DOCX album list/templates", ("skip_folder_setup", "skip_album_list_doc")),
        "requests":   ("requests", "cover-art download", ("skip_covers", "skip_soundmouse")),
        "numpy":      ("numpy", "UniSync screen matching", ("skip_unisync",)),
        "playwright": ("playwright", "Domo browser automation", ("skip_domo", "skip_soundmouse")),
    }
    missing_deps = []
    for import_name, (pip_name, why, skip_attrs) in _REQUIRED_IMPORTS.items():
        required = any(_active(attr) for attr in skip_attrs)
        try:
            __import__(import_name)
            logger.info(f"  ✓  {pip_name} available")
        except ImportError:
            if required:
                missing_deps.append(pip_name)
                logger.error(f"  ✗  {pip_name} NOT installed — needed for {why}")
                ok = False
            else:
                logger.info(f"  —  {pip_name} not installed (selected steps do not need it)")
    if missing_deps:
        logger.warning(
            "     Install missing dependencies with:  "
            "pip install -r requirements.txt"
        )

    # -- Per-user authentication state --------------------------------------
    # Repair local permissions before either application can read its state.
    # Status is deliberately redacted: no username, cookie, or token reaches
    # workflow logs or structured reports.
    from auth_manager import auth_status, secure_auth_permissions
    try:
        secure_auth_permissions()
        private_auth = auth_status()
        # Step 16 also performs Domo exports even when Step 1 was selected out.
        if _active("skip_domo") or _active("skip_soundmouse"):
            domo_state = private_auth["domo"]
            if domo_state["state"] == "configured" and domo_state["private_permissions"]:
                logger.info(
                    "  ✓  Domo per-user session configured for unattended silent SSO"
                )
            else:
                message = (
                    "  ✗  Domo is not enrolled for unattended use by this "
                    "macOS user. Run outside the workflow: "
                    "python3 auth_manager.py --setup domo"
                )
                if bool(getattr(args, "dry_run", False)):
                    logger.warning(message)
                else:
                    logger.error(message)
                    ok = False
        if _active("skip_unisync"):
            unisync_state = private_auth["unisync"]
            if unisync_state["state"] == "configured" and unisync_state["private_permissions"]:
                logger.info(
                    "  ✓  UniSync per-user app/Keychain session configured "
                    "for unattended reuse"
                )
            else:
                logger.error(
                    "  ✗  UniSync is not configured for this macOS user. Run: "
                    "python3 auth_manager.py --setup unisync, sign in inside "
                    "UniSync, then rerun the workflow."
                )
                ok = False
    except OSError as exc:
        logger.error(f"  ✗  Could not secure per-user authentication state: {exc}")
        ok = False

    # -- HDF1 login-session agent --------------------------------------------
    needs_soundminer = _active("skip_sourceaudio") or (
        _active("skip_soundminer")
        and not bool(getattr(args, "skip_nbc_mirror", False))
    )
    use_agent = (
        needs_soundminer
        and not is_soundminer_machine()
        and SOUNDMINER_AGENT_ENABLED
        and not bool(getattr(args, "no_soundminer_agent", False))
    )
    if use_agent:
        from soundminer_agent import agent_health
        healthy, detail = agent_health()
        if healthy:
            logger.info(f"  ✓  {detail}")
            if not bool(getattr(args, "dry_run", False)):
                from soundminer_agent import run_via_agent
                if not run_via_agent(ctx, "probe", False, logger):
                    logger.error(
                        "  ✗  HDF1 agent is online but its Aqua GUI preflight "
                        "failed; refusing to start the release."
                    )
                    ok = False
        elif bool(getattr(args, "dry_run", False)):
            logger.warning(f"  !  HDF1 agent unavailable during dry-run: {detail}")
        else:
            logger.error(
                f"  ✗  HDF1 Soundminer agent unavailable: {detail}\n"
                "     Install/start once on HDF1 with: "
                "python3 soundminer_agent.py --install"
            )
            ok = False

    if not ok:
        logger.error(
            "\nPreflight FAILED — one or more required storage, application, "
            "dependency, or remote-agent checks failed.\n"
            "Resolve the specific issues above and re-run."
        )
    else:
        logger.info("\nPreflight passed.")

    return ok


# ---------------------------------------------------------------------------
# Step dispatcher helpers
# ---------------------------------------------------------------------------

# Canonical step statuses (per spec: completed | skipped | failed).
STATUS_COMPLETED = "completed"
STATUS_SKIPPED   = "skipped"
STATUS_FAILED    = "failed"

# Back-compat aliases used by older code paths in this module.  They all map
# onto the three canonical statuses above so the summary stays consistent.
_STEP_RESULT_OK      = STATUS_COMPLETED
_STEP_RESULT_FAILED  = STATUS_FAILED
_STEP_RESULT_SKIPPED = STATUS_SKIPPED
_STEP_RESULT_STUB    = STATUS_FAILED      # an unimplemented stub counts as a failure
_STEP_RESULT_DRYRUN  = STATUS_COMPLETED   # a clean dry-run of a step is "completed"


def _ok(dry_run: bool) -> str:
    # dry-run or real, a successful step is reported as completed.
    return STATUS_COMPLETED


class StepResults:
    """
    Ordered record of each step's status plus an optional detail/artifact
    string (e.g. an output path).  Keeps the canonical status separate from
    the human-readable detail so the final summary can show both and the
    overall pass/fail can be computed reliably.
    """

    def __init__(self) -> None:
        # key -> (status, detail)
        self._items: dict[str, tuple[str, str]] = {}

    def set(self, key: str, status: str, detail: str = "") -> None:
        self._items[key] = (status, detail)

    def status(self, key: str) -> str:
        return self._items.get(key, ("", ""))[0]

    def detail(self, key: str) -> str:
        return self._items.get(key, ("", ""))[1]

    def get(self, key: str, default: str = "") -> str:
        """Return 'status (detail)' or just status — used by legacy callers."""
        if key not in self._items:
            return default
        status, detail = self._items[key]
        return f"{status} — {detail}" if detail else status

    def any_failed(self) -> bool:
        return any(s == STATUS_FAILED for s, _ in self._items.values())

    def __contains__(self, key: str) -> bool:
        return key in self._items

    def __setitem__(self, key: str, value: str) -> None:
        """Legacy dict-style assignment: results[key] = status_string."""
        # value may be a bare status, or a status with a path detail appended.
        self._items[key] = (value, "")

    def __getitem__(self, key: str) -> str:
        return self.get(key)

    def items(self):
        return self._items.items()


def _soundminer_handoff(
    ctx, args, logger, remote_cfg: dict,
    *,
    step_no:     int            = 12,
    sm_cmd:      "str | None"   = None,
    dests:       "list | None"  = None,
    output_exts: tuple          = ("wav",),
    what:        str            = "the NBC embed + mirror",
) -> bool:
    """
    Soundminer hand-off: Soundminer runs on a separate, managed Mac that can't
    be driven over SSH.  Print the exact command to run there, then wait for
    the operator to complete it and verify the expected output actually
    appeared before returning success.

    Generic over both Soundminer-driven steps:
      • Step 12 (NBC):         WAV output under nbc_wav_music
      • Step 11 (SourceAudio): AIFF output under the two SourceAudio Music dirs

    Defaults reproduce the NBC (Step 12) behaviour.  In --dry-run, just
    describe the hand-off and return True.
    """
    host = remote_cfg.get("host", "the Soundminer Mac")
    # The operator runs this in the remote Mac's OWN Terminal, so use the
    # console-session path (/Users/hdfuser/…), not the SSH path (/Volumes/…).
    repo = remote_cfg.get(
        "console_repo_path",
        remote_cfg.get("repo_path", "<repo>/files"),
    )
    if dests is None:
        dests = [ctx.partner_dirs["nbc_wav_music"]]
    # Build the exact command for the operator to run on the Soundminer Mac.
    # pinned_cli_args() preserves a resolved Full/Part context regardless of
    # what date the hand-off command is eventually run.
    if sm_cmd is None:
        pinned_args = " ".join(ctx.pinned_cli_args())
        sm_cmd = (
            f"python3 soundminer.py --nbc "
            f"{pinned_args}"
        )

    logger.info("")
    logger.info(f"  ┌─ MANUAL STEP {step_no} — run on the Soundminer Mac ─────────────")
    logger.info(f"  │ Soundminer is on {host} and must be driven in its own")
    logger.info("  │ console / Screen Sharing session (screen capture does not")
    logger.info("  │ work over SSH on that machine).")
    logger.info("  │")
    logger.info(f"  │ 1. Connect to {host} via Screen Sharing.")
    logger.info("  │ 2. Open Terminal THERE and run:")
    logger.info(f"  │      cd {repo!r}")
    logger.info(f"  │      {sm_cmd}")
    if getattr(ctx, "previous_month", False):
        logger.info("  │      (pinned previous-month command → targets "
                    f"{ctx.month_display} Full regardless of run date.)")
    logger.info(f"  │ 3. Watch it through {what}; answer prompts.")
    logger.info("  │ 4. When it reports success, return here and press Enter.")
    logger.info("  │")
    logger.info("  │ Output is expected at:")
    for d in dests:
        logger.info(f"  │   {d}")
    logger.info("  │ (shared Pegasus volume — visible to both machines).")
    logger.info("  └──────────────────────────────────────────────────────────")

    if args.dry_run:
        logger.info("  [DRY RUN] Hand-off described; not waiting for completion.")
        return True

    if getattr(args, "unattended", False):
        logger.warning(
            f"  --unattended set but Step {step_no} needs a manual hand-off on "
            f"the Soundminer Mac.  Treating Step {step_no} as INCOMPLETE; run it "
            f"there and re-run this pipeline (skipping the completed steps) once "
            f"done, or run attended."
        )
        return False

    try:
        input(f"\n  Press Enter once Step {step_no} has completed on the "
              "Soundminer Mac (Ctrl+C to abort)… ")
    except (EOFError, KeyboardInterrupt):
        logger.warning(f"\n  Step {step_no} hand-off aborted by operator.")
        return False

    # Verify the expected output actually appeared before we continue — a
    # blind "Enter" shouldn't pass a step that didn't happen.
    ext_globs = []
    for e in output_exts:
        ext_globs += [f"*.{e.lower()}", f"*.{e.upper()}"]

    total = 0
    for d in dests:
        if not d.exists():
            logger.error(
                f"  ✗  Expected output not found at:\n     {d}\n"
                f"     The mirror step doesn't appear to have completed.  Re-run "
                f"Step {step_no} on the Soundminer Mac, or check the path."
            )
            return False
        cnt = sum(len(list(d.rglob(g))) for g in ext_globs)
        if cnt == 0:
            logger.error(
                f"  ✗  No {'/'.join(output_exts)} files found under:\n     {d}\n"
                f"     The mirror produced an empty tree.  Re-run Step {step_no} "
                f"on the Soundminer Mac before continuing."
            )
            return False
        total += cnt

    logger.info(f"  ✓  Verified {total} mirrored "
                f"{'/'.join(e.upper() for e in output_exts)} file(s) at the "
                f"destination(s).")
    return True


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def _status_label(status: str) -> str:
    """Canonical status word with a visual marker for the summary table."""
    return {
        STATUS_COMPLETED: "✓ completed",
        STATUS_SKIPPED:   "— skipped",
        STATUS_FAILED:    "✗ FAILED",
    }.get(status, status or "— not run")


def _agg_status(*statuses: str) -> str:
    """Worst-case status across several sub-steps (for grouped lines)."""
    present = [s for s in statuses if s]
    if not present:
        return ""
    if any(s == STATUS_FAILED for s in present):
        return STATUS_FAILED
    if all(s == STATUS_SKIPPED for s in present):
        return STATUS_SKIPPED
    return STATUS_COMPLETED


def _render_final_summary(
    ctx: ReleaseContext,
    args: argparse.Namespace,
    results: "StepResults",
    log_path,
    logger,
    started_at: datetime,
) -> None:
    """
    Print the final summary in the exact field order required by the spec.
    Covers (steps 6–8) are aggregated into one line.  Overall status is
    'failed' if any step failed, else 'completed'.
    """
    log_section(logger, "UPM Release Workflow — Final Summary")

    covers = _agg_status(
        results.status("6 Download covers"),
        results.status("7 Covers → Specials"),
        results.status("8 Covers → WAV w COVERS"),
    )
    overall = STATUS_FAILED if results.any_failed() else STATUS_COMPLETED

    from workflow_report import write_workflow_report
    report_path = write_workflow_report(ctx, args, results, log_path, started_at)

    # (label, value) rows — value is either a plain string or a status label.
    rows: list[tuple[str, str]] = [
        ("Release ID",             ctx.release_id),
        ("Year",                   str(ctx.year)),
        ("Month",                  f"{ctx.month_name} {ctx.year_str}"),
        ("Release type",           "Full" if ctx.is_full_month else f"Part {ctx.part}"),
        ("Release date range",     f"{ctx.release_start} → {ctx.release_end}"),
        ("Domo exports",           _status_label(results.status("1 Domo exports"))),
        ("Specials folder",        _status_label(results.status("2 Specials folder"))),
        ("HD folder",              _status_label(results.status("3 HD folders"))),
        ("Album list PDF",         _status_label(results.status("4 Album list doc"))),
        ("UniSync jobs",           _status_label(results.status("5 UniSync jobs"))),
        ("Covers",                 _status_label(covers)),
        ("Verification",           _status_label(results.status("9 Verification"))),
        ("Missing report",         str(ctx.missing_report_csv)),
        ("Final packaging",        _status_label(results.status("10 Final packaging"))),
        ("SoundExchange forms",    _status_label(results.status("10 SoundExchange forms"))),
        ("SourceAudio",            _status_label(results.status("11 SourceAudio"))),
        ("Soundminer",             _status_label(results.status("12 Soundminer"))),
        ("NBC MP3 conversion",     _status_label(results.status("12 NBC WAV→MP3"))),
        ("Non-maintrack cleanup",  _status_label(results.status("13 Non-maintrack cleanup"))),
        ("NBC filename rename",    _status_label(results.status("14 NBC rename"))),
        ("Final metadata check",   _status_label(results.status("15 Final metadata check"))),
        ("SoundMouse",             _status_label(results.status("16 SoundMouse"))),
        ("SoundMouse missing report", str(ctx.soundmouse_validation_report)),
        ("Log file",               str(log_path)),
        ("Structured report",      str(report_path)),
        ("Overall status",         _status_label(overall)),
    ]

    width = max(len(label) for label, _ in rows)
    lines = [f"  {label + ':':<{width + 1}}  {value}" for label, value in rows]
    logger.info("\n" + "\n".join(lines))

    if args.dry_run:
        logger.info(
            "\n  (DRY RUN — no files were changed.  Re-run without --dry-run "
            "to apply.)"
        )

    if results.any_failed():
        # Failed step names for the restart hint.
        failed_keys = [
            k for k, (s, _d) in results.items() if s == STATUS_FAILED
        ]
        logger.error(
            "\n  ✗ One or more steps FAILED: " + ", ".join(failed_keys) + "\n"
            "    The workflow is restartable: fix the cause, then re-run the "
            "SAME command.\n"
            "    Completed steps are idempotent (existing outputs are skipped "
            "unless --overwrite),\n"
            "    and you can skip already-finished phases with the matching "
            "--skip-* flags\n"
            "    to resume from the failed step."
        )
    else:
        logger.info("\n  ✓ All requested steps completed successfully.")


def run_workflow(args: argparse.Namespace) -> int:
    """
    Execute the full UPM release workflow.
    Returns exit code (0 = success, 1 = failure).
    """

    run_started_at = datetime.now().astimezone()

    # ---- Build release context ----------------------------------------------
    try:
        if getattr(args, "previous_month", False):
            # Previous-month full-month run.  --year/--month optionally pin the
            # reference month; otherwise it's relative to today.  --part is
            # ignored.
            if (args.year is None) ^ (args.month is None):
                raise ValueError(
                    "--previous-month: pass BOTH --year and --month, or "
                    "NEITHER (to use today's date)."
                )
            ctx = ReleaseContext.for_previous_month(
                year=args.year, month=args.month
            )
        else:
            # Normal Part 1 / Part 2 run — year, month, and part are required.
            missing = [
                name for name, val in
                (("--year", args.year), ("--month", args.month), ("--part", args.part))
                if val is None
            ]
            if missing:
                raise ValueError(
                    "Missing required argument(s) for a normal run: "
                    + ", ".join(missing)
                    + ".  (Or use --previous-month for a full-month run.)"
                )
            ctx = ReleaseContext(year=args.year, month=args.month, part=args.part)
    except ValueError as exc:
        print(f"ERROR: Invalid arguments — {exc}", file=sys.stderr)
        return 1

    # ---- Set up logging -----------------------------------------------------
    logger, log_path = get_logger(
        ctx.year, ctx.month, ctx.part, release_label=ctx.release_variant
    )

    log_section(logger, "UPM Release Workflow")
    logger.info(ctx.summary())

    # ---- Expand step selectors (--start-at / --only) into skip flags --------
    try:
        _apply_step_selectors(args, logger)
    except ValueError as exc:
        logger.error(f"  ✗  {exc}")
        return 1

    from config import (
        machine_role,
        is_soundminer_machine,
        REMOTE_SOUNDMINER_ENABLED,
        SOUNDMINER_AGENT_ENABLED,
    )
    if args.skip_soundminer:
        _sm_mode = "skipped (--skip-soundminer)"
    elif (
        SOUNDMINER_AGENT_ENABLED
        and not is_soundminer_machine()
        and not getattr(args, "no_soundminer_agent", False)
    ):
        _sm_mode = "unattended HDF1 login-session agent"
    elif REMOTE_SOUNDMINER_ENABLED:
        _sm_mode = "remote via SSH"
    elif is_soundminer_machine():
        _sm_mode = "inline (this is the Soundminer machine)"
    else:
        _sm_mode = "hand-off (run Step 12 on the Soundminer machine)"
    logger.info(
        f"\n  Machine:        {machine_role()}"
        f"\n  Step 12 mode:   {_sm_mode}"
    )
    logger.info(
        f"\n  Flags:\n"
        f"    --dry-run:  {args.dry_run}\n"
        f"    --overwrite:{args.overwrite}"
    )

    results = StepResults()

    # ---- Preflight ----------------------------------------------------------
    log_section(logger, "Preflight")
    if not run_preflight(ctx, logger, args):
        results.set("preflight", STATUS_FAILED)
        logger.error(
            "\n  ✗ Preflight failed — halting before any step ran.\n"
            "    Fix the issues above (usually a missing/unmounted volume) and "
            "re-run the same command; nothing was changed."
        )
        _render_final_summary(ctx, args, results, log_path, logger, run_started_at)
        return 1
    results.set("preflight", STATUS_COMPLETED)

    # ---- Execute steps (guarded) ----------------------------------------
    # Any unhandled exception in a step is converted into a clean, recorded
    # failure + final summary rather than a raw traceback, so the run stays
    # restartable and the operator always sees where it stopped.
    try:
            # NOTE ON ORDER: Folder Setup (Steps 2 & 3) executes BEFORE Domo
            # (Step 1), even though Domo is numbered first.  Three of the Domo
            # cards (NBC, Japan, Tunesat metadata) write their CSVs INTO the
            # Specials release tree, so that tree — built from the baseline by
            # Step 2 — must exist first.  Building the baseline first means
            # Domo drops its CSVs into the correct, already-named folders
            # instead of creating a partial skeleton.  Step NUMBERS in logs and
            # the summary are unchanged; only execution order differs.

            # ---- Steps 2 & 3: Folder Setup (run first — see note above) -------------
            log_section(logger, "Steps 2 & 3 — Folder Creation")
            if args.skip_folder_setup:
                log_step_skipped(logger, 2, "Specials Folder")
                log_step_skipped(logger, 3, "HD Update Folders")
                results["2 Specials folder"] = _STEP_RESULT_SKIPPED
                results["3 HD folders"]      = _STEP_RESULT_SKIPPED
            else:
                from folder_setup import create_specials_folder, create_hd_folders

                # Step 2
                log_step_start(logger, 2, "Create Specials Folder")
                ok2 = create_specials_folder(ctx, args.dry_run, args.overwrite, logger)
                log_step_end(logger, 2, "Create Specials Folder", ok2)
                results["2 Specials folder"] = _ok(args.dry_run) if ok2 else _STEP_RESULT_FAILED

                # Step 3
                log_step_start(logger, 3, "Create HD Update Folders")
                ok3 = create_hd_folders(ctx, args.dry_run, args.overwrite, logger)
                log_step_end(logger, 3, "Create HD Update Folders", ok3)
                results["3 HD folders"] = _ok(args.dry_run) if ok3 else _STEP_RESULT_FAILED

            # ---- Step 1: Domo Exports (runs after folders exist) --------------------
            log_section(logger, "Step 1 — Domo Exports")
            if args.skip_domo:
                log_step_skipped(logger, 1, "Domo Exports")
                # Still validate that the files exist if we're skipping
                from domo_exports import verify_exports_exist
                checks = verify_exports_exist(ctx, logger)
                missing = [k for k, v in checks.items() if not v]
                if missing:
                    logger.warning(
                        f"  Some expected CSV files are missing: {missing}\n"
                        f"  Downstream steps may fail."
                    )
                results["1 Domo exports"] = _STEP_RESULT_SKIPPED
            else:
                log_step_start(logger, 1, "Domo Exports")
                from domo_exports import run_domo_exports
                export_results = run_domo_exports(ctx, args.dry_run, logger)
                any_stub   = any(v == "stub"   for v in export_results.values())
                any_failed = any(v == "failed" for v in export_results.values())
                if any_stub:
                    results["1 Domo exports"] = _STEP_RESULT_STUB
                elif any_failed:
                    results["1 Domo exports"] = _STEP_RESULT_FAILED
                    log_step_end(logger, 1, "Domo Exports", False)
                else:
                    results["1 Domo exports"] = _ok(args.dry_run)
                    log_step_end(logger, 1, "Domo Exports", True)

            # ---- Step 4: Album List Doc ---------------------------------------------
            log_section(logger, "Step 4 — Album List DOCX & PDF")
            if args.skip_album_list_doc:
                log_step_skipped(logger, 4, "Album List DOCX/PDF")
                results["4 Album list doc"] = _STEP_RESULT_SKIPPED
            else:
                log_step_start(logger, 4, "Album List DOCX/PDF")
                from album_list_doc import create_album_list_doc
                ok4 = create_album_list_doc(ctx, args.dry_run, logger)
                log_step_end(logger, 4, "Album List DOCX/PDF", ok4)
                results["4 Album list doc"] = (
                    _ok(args.dry_run) if ok4 else _STEP_RESULT_STUB
                )

            # ---- Step 5: UniSync ----------------------------------------------------
            log_section(logger, "Step 5 — UniSync Music Export")
            if args.skip_unisync:
                log_step_skipped(logger, 5, "UniSync Jobs")
                results["5 UniSync jobs"] = _STEP_RESULT_SKIPPED
            else:
                log_step_start(logger, 5, "UniSync Jobs")
                from unisync_automation import (
                    run_all_unisync_jobs,
                    set_capture_steps,
                    set_supervised,
                    set_xml_setup,
                    STATUS_FAILED as _US_FAILED,
                )
                set_capture_steps(getattr(args, "capture_steps", False))
                set_supervised(getattr(args, "unisync_supervised", False))
                set_xml_setup(getattr(args, "unisync_xml_setup", True))
                job_results = run_all_unisync_jobs(
                    ctx, args.dry_run, logger, overwrite=args.overwrite
                )
                for job_name, status in job_results.items():
                    logger.info(f"  UniSync {job_name}: {status}")
                any_failed = any(v == _US_FAILED for v in job_results.values())
                if any_failed:
                    results["5 UniSync jobs"] = _STEP_RESULT_FAILED
                else:
                    results["5 UniSync jobs"] = _ok(args.dry_run)
                log_step_end(logger, 5, "UniSync Jobs", not any_failed)

            # Build WAV w COVERS by COPYING the WAV tree instead of
            # re-downloading it through UniSync (it's the WAV files plus
            # covers, which Step 8 adds).  Normally runs as the UniSync tail so
            # WAV w COVERS exists for final packaging even when the cover steps
            # are skipped.  Idempotent and cheap.  --rebuild-wav-covers forces
            # it even when UniSync itself is skipped (e.g. the audio is already
            # fetched but the WAV w COVERS tree needs rebuilding).
            if (not args.skip_unisync) or args.rebuild_wav_covers:
                if args.skip_unisync and args.rebuild_wav_covers:
                    log_section(logger,
                                "Step 5 tail — Rebuild WAV w COVERS (forced)")
                from covers import build_wav_with_covers_from_wav
                build_wav_with_covers_from_wav(ctx, args.dry_run, logger)

            # ---- Steps 6–8: Covers --------------------------------------------------
            log_section(logger, "Steps 6–8 — Album Covers")
            if args.skip_covers:
                log_step_skipped(logger, 6, "Download Covers")
                log_step_skipped(logger, 7, "Copy Covers → Specials")
                log_step_skipped(logger, 8, "Copy Covers → WAV w COVERS")
                results["6 Download covers"]       = _STEP_RESULT_SKIPPED
                results["7 Covers → Specials"]     = _STEP_RESULT_SKIPPED
                results["8 Covers → WAV w COVERS"] = _STEP_RESULT_SKIPPED
            else:
                from covers import (
                    download_covers,
                    copy_covers_to_specials,
                    copy_covers_to_wav_with_covers,
                )

                log_step_start(logger, 6, "Download Covers")
                ok6 = download_covers(ctx, args.dry_run, args.overwrite, logger)
                log_step_end(logger, 6, "Download Covers", ok6)
                results["6 Download covers"] = _ok(args.dry_run) if ok6 else _STEP_RESULT_STUB

                log_step_start(logger, 7, "Copy Covers → Specials")
                ok7 = copy_covers_to_specials(ctx, args.dry_run, logger)
                log_step_end(logger, 7, "Copy Covers → Specials", ok7)
                results["7 Covers → Specials"] = _ok(args.dry_run) if ok7 else _STEP_RESULT_STUB

                log_step_start(logger, 8, "Copy Covers → WAV w COVERS")
                ok8 = copy_covers_to_wav_with_covers(ctx, args.dry_run, logger)
                log_step_end(logger, 8, "Copy Covers → WAV w COVERS", ok8)
                results["8 Covers → WAV w COVERS"] = (
                    _ok(args.dry_run) if ok8 else _STEP_RESULT_STUB
                )

            # ---- Optional: prune Music trees before verifying -----------------------
            if args.prune_music:
                log_section(logger, "Prune — Reconcile 1-ORIGINAL/Music")
                from prune import prune_music_trees
                mode = "report" if args.dry_run else args.prune_mode
                removable, kept = prune_music_trees(ctx, mode, logger)
                results["Music prune"] = (
                    f"{removable} {'removed' if mode == 'delete' else 'archived' if mode == 'archive' else 'removable (report)'}"
                    f", {kept} kept"
                )

            # ---- Step 9: Verify (+ optional auto-remediation loop) ------------------
            log_section(logger, "Step 9 — Verification")
            verify_failed = False   # gates the finalization steps (10–15) below
            if args.skip_verify:
                log_step_skipped(logger, 9, "File Verification")
                results["9 Verification"] = _STEP_RESULT_SKIPPED
            else:
                log_step_start(logger, 9, "File Verification")
                if args.remediate:
                    # Verify → remediate → re-verify, looping until clean or capped.
                    # Re-fetches missing audio via UniSync (unless suppressed) and
                    # re-fixes covers, retrying UniSync's known intermittent misses.
                    from remediation import verify_and_remediate_loop
                    clean, remaining = verify_and_remediate_loop(
                        ctx,
                        max_attempts=args.remediate_attempts,
                        run_unisync=not args.remediate_no_unisync,
                        run_domo=not args.remediate_no_domo,
                        overwrite=args.overwrite,
                        dry_run=args.dry_run,
                        logger=logger,
                    )
                    ok9 = clean
                    if clean:
                        results["9 Verification"] = _ok(args.dry_run)
                    else:
                        # Must register as a real FAILURE so any_failed() trips
                        # and the run exits non-zero — otherwise a remediate run
                        # that never reached a clean state would report success.
                        results.set(
                            "9 Verification", STATUS_FAILED,
                            f"{remaining} still missing after "
                            f"{args.remediate_attempts} attempt(s)",
                        )
                else:
                    from verification import verify_all_files
                    ok9 = verify_all_files(ctx, args.dry_run, logger)
                    results["9 Verification"] = (
                        _ok(args.dry_run) if ok9 else _STEP_RESULT_STUB
                    )
                log_step_end(logger, 9, "File Verification", ok9)
                results["9 Missing report"] = str(ctx.missing_report_csv)
                verify_failed = not ok9

            # Finalization gate: if verification ran and found missing files, do NOT
            # build the final deliverable from an incomplete source.  (A dry-run is
            # exempt so the plan can be previewed end-to-end.)  Override paths:
            # fix the cause and re-run, add --remediate to auto-retry the producers,
            # or --skip-verify to package anyway.
            finalize_blocked = verify_failed and not args.dry_run
            if finalize_blocked:
                logger.error(
                    "\n  ✗ Verification failed — skipping final packaging and the "
                    "remaining finalization steps (10–15)\n"
                    "    so an incomplete release isn't assembled.  See the missing "
                    "report:\n"
                    f"      {ctx.missing_report_csv}\n"
                    "    Then either re-run the same command (idempotent — it fills "
                    "only the gaps),\n"
                    "    add --remediate to auto-retry the producers, or --skip-verify "
                    "to package anyway."
                )

            # ---- Step 10: Final Packaging -------------------------------------------
            log_section(logger, "Step 10 — Final Packaging")
            if finalize_blocked:
                log_step_skipped(logger, 10, "Copy Originals to Finals")
                results["10 Final packaging"] = "blocked — verification failed"
            elif args.skip_final_packaging:
                log_step_skipped(logger, 10, "Copy Originals to Finals")
                results["10 Final packaging"] = _STEP_RESULT_SKIPPED
            else:
                log_step_start(logger, 10, "Copy Originals to Finals")
                from final_packaging import copy_originals_to_finals
                ok10 = copy_originals_to_finals(
                    ctx, args.dry_run, logger, overwrite=args.overwrite
                )
                log_step_end(logger, 10, "Copy Originals to Finals", ok10)
                results["10 Final packaging"] = (
                    _ok(args.dry_run) if ok10 else _STEP_RESULT_STUB
                )

            # ---- Step 10 (cont.): SoundExchange ISRC Ingest Forms ----------------
            # A metadata-only final deliverable produced from the Step-1
            # SoundExchange exports into 3-FINAL PACKAGING/…- SoundExchange.
            # It belongs to the final-packaging phase, so it shares Step 10's
            # verification gate and is skipped by --skip-final-packaging; a
            # dedicated --skip-soundexchange skips just this without skipping
            # the audio/cover copy above.
            if finalize_blocked:
                log_step_skipped(logger, 10, "SoundExchange Ingest Forms")
                results["10 SoundExchange forms"] = "blocked — verification failed"
            elif args.skip_final_packaging or args.skip_soundexchange:
                log_step_skipped(logger, 10, "SoundExchange Ingest Forms")
                results["10 SoundExchange forms"] = _STEP_RESULT_SKIPPED
            else:
                log_step_start(logger, 10, "SoundExchange Ingest Forms")
                from split_se_ingest_forms import run_soundexchange_split
                ok_se = run_soundexchange_split(
                    ctx, dry_run=args.dry_run, logger=logger
                )
                log_step_end(logger, 10, "SoundExchange Ingest Forms", ok_se)
                results["10 SoundExchange forms"] = (
                    _ok(args.dry_run) if ok_se else _STEP_RESULT_STUB
                )

            # ---- Step 11: SourceAudio (Soundminer scan → AIFF mirror) -------------
            log_section(logger, "Step 11 — SourceAudio (AIFF) Mirror")
            if finalize_blocked:
                log_step_skipped(logger, 11, "SourceAudio Mirror")
                results["11 SourceAudio"] = "blocked — verification failed"
            elif args.skip_sourceaudio:
                log_step_skipped(logger, 11, "SourceAudio Mirror")
                results["11 SourceAudio"] = _STEP_RESULT_SKIPPED
            else:
                log_step_start(logger, 11, "SourceAudio Mirror")
                from config import (
                    REMOTE_SOUNDMINER_ENABLED,
                    REMOTE_SOUNDMINER,
                    SOUNDMINER_AGENT_ENABLED,
                    is_soundminer_machine,
                )
                sa_dests = [
                    ctx.partner_dirs["sourceaudio_music"],
                    ctx.partner_dirs["sourceaudio_exus_music"],
                ]
                if is_soundminer_machine():
                    # We ARE on the Soundminer Mac — drive the GUI directly,
                    # inline (this is the capturable console/GUI session).
                    logger.info(
                        "  Running ON the Soundminer machine → executing Step 11 "
                        "inline (no hand-off)."
                    )
                    from soundminer import run_soundminer_sourceaudio_workflow
                    ok_sa = run_soundminer_sourceaudio_workflow(
                        ctx, args.dry_run, logger,
                        unattended=(not args.soundminer_attended),
                        db_shortcut=args.sourceaudio_db_shortcut,
                        resume=args.soundminer_resume,
                    )
                elif (
                    SOUNDMINER_AGENT_ENABLED
                    and not args.no_soundminer_agent
                ):
                    from soundminer_agent import run_via_agent
                    ok_sa = run_via_agent(
                        ctx, "sourceaudio", args.dry_run, logger,
                        options={
                            "capture_steps": args.capture_steps,
                            "resume": args.soundminer_resume,
                            "db_shortcut": args.sourceaudio_db_shortcut,
                        },
                    )
                else:
                    # Pipeline Mac: Soundminer lives on a separate managed Mac
                    # that can't be driven over SSH.  Hand off to the operator
                    # (REMOTE_SOUNDMINER_ENABLED only wires the NBC SSH path, so
                    # SourceAudio always hands off from here).
                    if REMOTE_SOUNDMINER_ENABLED:
                        logger.info(
                            "  REMOTE_SOUNDMINER_ENABLED is set, but the SSH path "
                            "only covers the NBC step; SourceAudio uses the manual "
                            "hand-off."
                        )
                    pinned_args = " ".join(ctx.pinned_cli_args())
                    sa_cmd = (
                        f"python3 soundminer.py --sourceaudio "
                        f"{pinned_args}"
                    )
                    if str(args.sourceaudio_db_shortcut) != "8":
                        sa_cmd += (
                            f" --sourceaudio-db-shortcut "
                            f"{args.sourceaudio_db_shortcut}"
                        )
                    ok_sa = _soundminer_handoff(
                        ctx, args, logger, REMOTE_SOUNDMINER,
                        step_no=11,
                        sm_cmd=sa_cmd,
                        dests=sa_dests,
                        output_exts=("aif", "aiff"),
                        what="the SourceAudio scan + AIFF mirror (both passes)",
                    )
                log_step_end(logger, 11, "SourceAudio Mirror", ok_sa)
                results["11 SourceAudio"] = (
                    _ok(args.dry_run) if ok_sa else _STEP_RESULT_STUB
                )

            # ---- Step 12: Soundminer NBC --------------------------------------------
            #   Kept directly after SourceAudio (Step 11) so the operator stays
            #   in Soundminer for both partner deliveries before moving on.
            log_section(logger, "Step 12 — Soundminer NBC Workflow")
            if finalize_blocked:
                log_step_skipped(logger, 12, "Soundminer NBC")
                results["12 Soundminer"] = "blocked — verification failed"
            elif args.skip_soundminer:
                log_step_skipped(logger, 12, "Soundminer NBC")
                results["12 Soundminer"] = _STEP_RESULT_SKIPPED
            else:
                log_step_start(logger, 12, "Soundminer NBC Embed + Mirror")
                from config import (
                    REMOTE_SOUNDMINER_ENABLED,
                    REMOTE_SOUNDMINER,
                    SOUNDMINER_AGENT_ENABLED,
                    is_soundminer_machine,
                )
                if args.skip_nbc_mirror:
                    # Mirror already done in a prior run — verify the WAV tree
                    # is present and non-empty, then treat the mirror as
                    # complete and fall through to the 12.7 conversion.
                    wav_dest = ctx.partner_dirs["nbc_wav_music"]
                    wav_count = (
                        sum(1 for _ in wav_dest.rglob("*.wav"))
                        + sum(1 for _ in wav_dest.rglob("*.WAV"))
                    ) if wav_dest.exists() else 0
                    if wav_count == 0:
                        logger.error(
                            "  ✗  --skip-nbc-mirror set, but no WAV files found "
                            f"at:\n     {wav_dest}\n"
                            "     Run the NBC Soundminer mirror first (drop "
                            "--skip-nbc-mirror), or check the path."
                        )
                        ok_nbc = False
                    else:
                        logger.info(
                            f"  ↩  Skipping NBC embed+mirror (12.1–12.6) — "
                            f"{wav_count} WAV file(s) already present; resuming "
                            "at the WAV→MP3 conversion."
                        )
                        ok_nbc = True
                elif (
                    SOUNDMINER_AGENT_ENABLED
                    and not is_soundminer_machine()
                    and not args.no_soundminer_agent
                ):
                    from soundminer_agent import run_via_agent
                    ok_nbc = run_via_agent(
                        ctx, "nbc", args.dry_run, logger,
                        options={
                            "capture_steps": args.capture_steps,
                            "resume": args.soundminer_resume,
                        },
                    )
                elif REMOTE_SOUNDMINER_ENABLED:
                    # SSH-triggered remote execution (only viable on an unmanaged Mac
                    # where GUI automation over SSH is permitted).
                    from remote_runner import run_soundminer_remote
                    ok_nbc = run_soundminer_remote(ctx, args.dry_run, logger)
                elif is_soundminer_machine():
                    # We ARE on the Soundminer Mac — drive the GUI directly, inline,
                    # no hand-off pause.  This is the capturable console/GUI session,
                    # so pyautogui works (unlike over SSH).
                    logger.info(
                        "  Running ON the Soundminer machine → executing Step 12 "
                        "inline (no hand-off)."
                    )
                    from soundminer import run_soundminer_nbc_workflow
                    ok_nbc = run_soundminer_nbc_workflow(
                        ctx, args.dry_run, logger,
                        unattended=(not args.soundminer_attended),
                        resume=args.soundminer_resume,
                    )
                else:
                    # Hand-off mode.  We're on the pipeline Mac; Soundminer runs on a
                    # separate, managed Mac that cannot be driven over SSH (no screen
                    # capture in the SSH context).  Pause here and have the operator
                    # run Step 12 directly on that machine inside its Screen Sharing /
                    # console session; the shared Pegasus volumes make the data
                    # hand-off automatic.
                    ok_nbc = _soundminer_handoff(
                        ctx, args, logger, REMOTE_SOUNDMINER, step_no=12
                    )
                log_step_end(logger, 12, "Soundminer NBC Embed + Mirror", ok_nbc)
                results["12 Soundminer"] = (
                    _STEP_RESULT_SKIPPED if args.skip_nbc_mirror and ok_nbc
                    else (_ok(args.dry_run) if ok_nbc else _STEP_RESULT_STUB)
                )

                # Step 12.7: WAV → MP3 conversion — only attempt if the mirror step
                # actually produced output (skipping it on a failed/declined hand-off
                # avoids converting an empty or partial WAV tree).
                if ok_nbc and not args.dry_run:
                    log_step_start(logger, 12, "NBC WAV → MP3 Conversion")
                    from audio_conversion import convert_nbc_wav_to_mp3
                    ok_nbc_conv = convert_nbc_wav_to_mp3(ctx, args.dry_run, args.overwrite, logger)
                    log_step_end(logger, 12, "NBC WAV → MP3 Conversion", ok_nbc_conv)
                    results["12 NBC WAV→MP3"] = (
                        _ok(args.dry_run) if ok_nbc_conv else _STEP_RESULT_FAILED
                    )
                elif args.dry_run:
                    log_step_start(logger, 12, "NBC WAV → MP3 Conversion")
                    from audio_conversion import convert_nbc_wav_to_mp3
                    ok_nbc_conv = convert_nbc_wav_to_mp3(ctx, args.dry_run, args.overwrite, logger)
                    log_step_end(logger, 12, "NBC WAV → MP3 Conversion", ok_nbc_conv)
                    results["12 NBC WAV→MP3"] = _ok(args.dry_run)
                else:
                    logger.warning(
                        "  Skipping NBC WAV→MP3 conversion because the Soundminer "
                        "mirror step did not complete successfully."
                    )
                    results["12 NBC WAV→MP3"] = _STEP_RESULT_SKIPPED

            # ---- Step 13: Non-MainTrack Cleanup -------------------------------------
            log_section(logger, "Step 13 — Non-MainTrack Cleanup")
            if finalize_blocked:
                log_step_skipped(logger, 13, "Non-MainTrack Removal")
                results["13 Non-maintrack cleanup"] = "blocked — verification failed"
            elif args.skip_non_maintrack_cleanup:
                log_step_skipped(logger, 13, "Non-MainTrack Removal")
                results["13 Non-maintrack cleanup"] = _STEP_RESULT_SKIPPED
            else:
                log_step_start(logger, 13, "Non-MainTrack Removal")
                from cleanup import remove_non_maintracks
                # Real deletion in a normal run; only --dry-run holds back to a
                # report.  --dry-run is the safety net, so there's no separate
                # opt-in flag — a normal run deletes the non-maintracks.
                ok_cleanup = remove_non_maintracks(
                    ctx=ctx,
                    dry_run=args.dry_run,
                    actually_delete=not args.dry_run,
                    logger=logger,
                )
                log_step_end(logger, 13, "Non-MainTrack Removal", ok_cleanup)
                results["13 Non-maintrack cleanup"] = (
                    _ok(args.dry_run) if ok_cleanup else _STEP_RESULT_STUB
                )

            # ---- Step 14: Rename NBC Files ------------------------------------------
            log_section(logger, "Step 14 — Rename NBC Music Files")
            if finalize_blocked:
                log_step_skipped(logger, 14, "NBC Filename Rename")
                results["14 NBC rename"] = "blocked — verification failed"
            elif args.skip_rename:
                log_step_skipped(logger, 14, "NBC Filename Rename")
                results["14 NBC rename"] = _STEP_RESULT_SKIPPED
            else:
                log_step_start(logger, 14, "NBC Filename Rename")
                from cleanup import rename_nbc_music_files
                ok13 = rename_nbc_music_files(ctx, args.dry_run, logger)
                log_step_end(logger, 14, "NBC Filename Rename", ok13)
                results["14 NBC rename"] = (
                    _ok(args.dry_run) if ok13 else _STEP_RESULT_STUB
                )

            # ---- Step 15: Final Packaging metadata ⇄ media cross-check -------------
            log_section(logger, "Step 15 — Final Metadata Cross-Check")
            if finalize_blocked:
                log_step_skipped(logger, 15, "Final Metadata Cross-Check")
                results["15 Final metadata check"] = "blocked — verification failed"
            elif args.skip_final_metadata_check:
                log_step_skipped(logger, 15, "Final Metadata Cross-Check")
                results["15 Final metadata check"] = _STEP_RESULT_SKIPPED
            else:
                log_step_start(logger, 15, "Final Metadata Cross-Check")
                from final_metadata_verification import (
                    verify_final_packaging_metadata,
                )
                ok15 = verify_final_packaging_metadata(ctx, logger, args.dry_run)
                log_step_end(logger, 15, "Final Metadata Cross-Check", ok15)
                results["15 Final metadata check"] = (
                    _ok(args.dry_run) if ok15 else _STEP_RESULT_STUB
                )

            # ---- Step 16: SoundMouse delivery --------------------------------------
            # SoundMouse is an independent delivery and is intentionally not
            # gated by the Step 9 Specials/HD verification result.
            log_section(logger, "Step 16 — SoundMouse Delivery")
            if args.skip_soundmouse:
                log_step_skipped(logger, 16, "SoundMouse Delivery")
                results["16 SoundMouse"] = _STEP_RESULT_SKIPPED
            else:
                log_step_start(logger, 16, "SoundMouse Delivery")
                from soundmouse import run_soundmouse_step
                ok16 = run_soundmouse_step(
                    ctx, args.dry_run, args.overwrite, logger
                )
                log_step_end(logger, 16, "SoundMouse Delivery", ok16)
                results["16 SoundMouse"] = (
                    _ok(args.dry_run) if ok16 else _STEP_RESULT_FAILED
                )
    except KeyboardInterrupt:
        logger.error("\n  ✗ Interrupted by user (Ctrl-C). Halting.")
        if not results.any_failed():
            results.set("interrupted", STATUS_FAILED, "user interrupt")
    except Exception as exc:
        import traceback
        from logging_utils import current_step
        where = current_step()
        logger.error(
            f"\n  ✗ Unexpected error during {where}: "
            f"{type(exc).__name__}: {exc}"
        )
        logger.error(traceback.format_exc())
        results.set(where, STATUS_FAILED, f"{type(exc).__name__}: {exc}")

    # ---- Final summary -------------------------------------------------------
    _render_final_summary(ctx, args, results, log_path, logger, run_started_at)

    # Exit non-zero if any step hard-failed (makes the run scriptable and the
    # restart decision unambiguous).
    return 1 if results.any_failed() else 0


# ---------------------------------------------------------------------------
# Step selectors (--start-at / --only) → concrete --skip-* expansion
# ---------------------------------------------------------------------------

# Ordered pipeline units.  Each entry: (token, numeric position, attribute that
# skips the WHOLE unit).  Combined groups are represented by their lead number
# (folder setup = steps 2 & 3 → "2"; covers = steps 6–8 → "6").  Step 12 is
# special: it has two entry points — "12" (full embed + mirror + convert) and
# "12.7" (convert only, mirror already done) — resolved in _apply_step_selectors.
_STEP_UNITS = [
    # token,  position, skip-flag attr,                human name
    ("1",    1.0,  "skip_domo",                  "Domo exports"),
    ("2",    2.0,  "skip_folder_setup",          "Folder setup (Steps 2-3)"),
    ("4",    4.0,  "skip_album_list_doc",        "Album list (DOCX/PDF)"),
    ("5",    5.0,  "skip_unisync",               "UniSync"),
    ("6",    6.0,  "skip_covers",                "Covers (download/distribute, 6-8)"),
    ("9",    9.0,  "skip_verify",                "Verification (gate)"),
    ("10",   10.0, "skip_final_packaging",       "Final packaging (+ SoundExchange forms)"),
    ("11",   11.0, "skip_sourceaudio",           "SourceAudio (AIFF mirror)"),
    ("12",   12.0, "skip_soundminer",            "Soundminer NBC"),
    ("12.7", 12.7, None,                         "NBC WAV->MP3 (within step 12)"),
    ("13",   13.0, "skip_non_maintrack_cleanup", "Non-maintrack cleanup"),
    ("14",   14.0, "skip_rename",                "NBC rename"),
    ("15",   15.0, "skip_final_metadata_check",  "Final metadata cross-check"),
    ("16",   16.0, "skip_soundmouse",            "SoundMouse delivery"),
]
_STEP_TOKENS = [u[0] for u in _STEP_UNITS]


def format_step_list() -> str:
    """Render the canonical step registry — the single source of truth that
    --only / --start-at and the per-step skip flags are derived from."""
    lines = ["Workflow steps (token  name  [skip flag]):"]
    for tok, _pos, attr, name in _STEP_UNITS:
        flag = f"--{attr.replace('_', '-')}" if attr else "(no skip flag)"
        lines.append(f"  {tok:<5} {name:<34} {flag}")
    lines.append(
        "\nUse --only <token> to run one step, or --start-at <token> to resume "
        "from it.\nGated steps (10-15) also require Step 9 to pass "
        "(or --skip-verify)."
    )
    return "\n".join(lines)
_ALL_SKIP_ATTRS = [
    "skip_domo", "skip_folder_setup", "skip_album_list_doc", "skip_unisync",
    "skip_covers", "skip_verify", "skip_final_packaging", "skip_soundexchange",
    "skip_sourceaudio", "skip_soundminer", "skip_nbc_mirror",
    "skip_non_maintrack_cleanup", "skip_rename", "skip_final_metadata_check",
    "skip_soundmouse",
]


def _apply_step_selectors(args, logger) -> None:
    """Expand --start-at / --only into the concrete --skip-* flags.

    The pipeline is linear, so 'start at K' = skip every unit before K, and
    'only K' = skip every unit except K.  Step 12's two entry points (12 = full
    embed+mirror+convert; 12.7 = convert only, mirror already done) map to
    skip_soundminer vs skip_nbc_mirror.  Raises ValueError on an unknown token.
    """
    token = args.start_at or args.only
    if token is None:
        return
    if token not in _STEP_TOKENS:
        raise ValueError(
            f"Unknown step '{token}'.  Valid steps: {', '.join(_STEP_TOKENS)}."
        )
    kv = float(token)

    def _set(attr, val=True):
        setattr(args, attr, val)

    if args.start_at:
        for _tok, pos, attr, _name in _STEP_UNITS:
            if attr is None or attr == "skip_soundminer":
                continue                      # 12 boundary handled below
            if pos < kv:
                _set(attr)
        if kv > 12.7:
            _set("skip_soundminer")           # start after step 12 entirely
        elif kv == 12.7:
            _set("skip_nbc_mirror")           # start at the convert; mirror done
        logger.info(f"  --start-at {token}: resuming at step {token}; "
                    "earlier steps skipped.")
    else:  # --only
        for attr in _ALL_SKIP_ATTRS:
            _set(attr, True)
        if token == "12":
            _set("skip_soundminer", False)
            _set("skip_nbc_mirror", False)
        elif token == "12.7":
            _set("skip_soundminer", False)    # let the step 12 block run …
            _set("skip_nbc_mirror", True)     # … but only the convert part
        else:
            attr = next(a for (t, _, a, _) in _STEP_UNITS if t == token)
            if attr:
                _set(attr, False)
            if token == "10":
                # SoundExchange is a Step 10 sub-phase with its own skip flag
                # but no token of its own — un-skip it so `--only 10` runs it.
                _set("skip_soundexchange", False)
        logger.info(f"  --only {token}: running step {token} only; all other "
                    "steps skipped.")


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="upm_release_workflow",
        description="UPM Twice-Monthly Release Workflow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--year",  type=int, help="Release year, e.g. 2026")
    p.add_argument("--month", type=int, help="Release month, e.g. 5")
    p.add_argument(
        "--part",
        type=int,
        choices=[1, 2],
        help="1 = days 1-14, 2 = days 15-end (ignored with --previous-month)",
    )
    p.add_argument(
        "--previous-month",
        action="store_true",
        help="Full-month run for the PREVIOUS month (no Part split).  By "
             "default the previous month is relative to today; pass --year/"
             "--month to compute it relative to a specific month instead.  "
             "Domo uses its built-in 'Previous Month' preset and all folders "
             "use explicit 'Month YYYY Full' naming.",
    )

    # Safety flags
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen; make no changes to disk",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing destination folders/files",
    )

    # Skip flags
    skips = p.add_argument_group("skip flags")
    skips.add_argument("--skip-domo",                   action="store_true")
    skips.add_argument("--skip-folder-setup",           action="store_true")
    skips.add_argument("--skip-album-list-doc",         action="store_true")
    skips.add_argument("--skip-unisync",                action="store_true")
    skips.add_argument("--skip-covers",                 action="store_true")
    skips.add_argument("--skip-verify",                 action="store_true")
    skips.add_argument("--skip-final-packaging",        action="store_true")
    skips.add_argument("--skip-soundexchange",          action="store_true",
        help="Step 10: skip only the SoundExchange ISRC Ingest Form generation "
             "(the audio/cover copy still runs). --skip-final-packaging skips "
             "both.")
    skips.add_argument("--skip-sourceaudio",            action="store_true")
    skips.add_argument("--skip-non-maintrack-cleanup",  action="store_true")
    skips.add_argument("--skip-soundminer",             action="store_true")
    skips.add_argument("--skip-nbc-mirror",             action="store_true",
        help="Step 12: the NBC Soundminer embed+mirror (12.1–12.6) is already "
             "done; skip it and resume at the WAV→MP3 conversion (12.7). "
             "Verifies the NBC WAV tree is non-empty first.")
    skips.add_argument("--skip-rename",                 action="store_true")
    skips.add_argument("--skip-final-metadata-check",   action="store_true",
        help="Skip Step 15 (cross-check each 3-FINAL PACKAGING partner's "
             "metadata sheet against its media folder).")
    skips.add_argument("--skip-soundmouse", action="store_true",
        help="Skip Step 16 (SoundMouse Domo exports, folders, WAVs, covers, "
             "and bucket-selected metadata sheets).")
    skips.add_argument("--rebuild-wav-covers",          action="store_true",
        help="Run the WAV w COVERS audio build (the Step 5 tail) even when "
             "--skip-unisync is set — e.g. the audio is already fetched but the "
             "WAV w COVERS tree needs rebuilding.")

    # Step selectors — convenience shorthands that EXPAND into the right set of
    # --skip-* flags so you don't hand-assemble them.  Mutually exclusive.
    sel = p.add_argument_group("step selectors")
    sel_mx = sel.add_mutually_exclusive_group()
    sel_mx.add_argument("--start-at", metavar="STEP", default=None,
        help="Resume the pipeline AT this step, skipping everything before it. "
             f"Valid: {', '.join(_STEP_TOKENS)}.  E.g. --start-at 12.7 runs only "
             "the NBC WAV→MP3 conversion and the rename.")
    sel_mx.add_argument("--only", metavar="STEP", default=None,
        help="Run ONLY this step (skip everything else). "
             f"Valid: {', '.join(_STEP_TOKENS)}.  E.g. --only 9 runs just "
             "verification; --only 11 runs just SourceAudio.")

    p.add_argument(
        "--list-steps",
        action="store_true",
        help="Print the canonical step list (token, name, skip flag) and exit. "
             "This registry is the single source of truth for --only/--start-at.",
    )

    # Behaviour flags
    p.add_argument(
        "--soundminer-attended",
        action="store_true",
        help="Steps 11 & 12: run the Soundminer SourceAudio and NBC workflows "
             "ATTENDED — pause for you to press Enter after each scan/import/"
             "embed and to confirm the Mirror Settings dialog before OK. The "
             "DEFAULT is fully unattended (no Enter prompts): scan/import/embed "
             "completion is detected by watching the Soundminer UI settle, and "
             "the Mirror Settings dialog is auto-accepted (its settings persist "
             "between releases). Use this flag for a first run on a new machine "
             "to eyeball that the persisted mirror settings are correct.",
    )
    p.add_argument(
        "--no-soundminer-agent",
        action="store_true",
        help="Disable the shared HDF1 login-session agent and use the legacy "
             "manual/SSH Soundminer path. Intended only for recovery.",
    )
    p.add_argument(
        "--soundminer-resume",
        action="store_true",
        help="Resume Soundminer from its last validated phase checkpoint for "
             "this release instead of repeating completed phases.",
    )
    p.add_argument(
        "--sourceaudio-unattended",
        action="store_true",
        help="Deprecated / no-op: Step 11 is unattended by default now. Kept "
             "for backward compatibility. Use --soundminer-attended to force "
             "the attended pauses instead.",
    )
    p.add_argument(
        "--sourceaudio-db-shortcut",
        default="8",
        metavar="KEY",
        help="Step 11: Soundminer database shortcut digit to switch to before "
             "scanning (default '8' = ⌘8, the SourceAudio database). Pass a "
             "different digit if the SourceAudio DB shortcut changes.",
    )
    p.add_argument(
        "--capture-steps",
        action="store_true",
        help="Step 5: save per-step UniSync UI screenshots to "
             "_logs/unisync_debug_steps/ for diagnosing path-entry issues.",
    )
    p.add_argument(
        "--unisync-supervised",
        action="store_true",
        help="Step 5: supervised UniSync. A UI failure (e.g. accidental focus "
             "loss) pauses for you to press Enter and retry instead of failing; "
             "and after the automatic retry cap, if files are still missing, it "
             "pauses for you to press Enter to keep retrying just the missing "
             "ones until they're all delivered before continuing.",
    )
    p.add_argument(
        "--unisync-xml-setup",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Step 5 (DEFAULT ON): configure each UniSync job by writing its "
             "Territory/Cache/Client into UniSync.xml and relaunching UniSync, "
             "instead of driving the path-entry UI. Eliminates the territory "
             "dropdown, folder-icon clicks, and Cmd+Shift+G path typing. Pass "
             "--no-unisync-xml-setup to fall back to the old on-screen UI. "
             "Quits/relaunches UniSync on the first pass of each file type; "
             "same-type retries reuse the running app.",
    )

    # Auto-remediation (Step 9b) flags
    remediate = p.add_argument_group("auto-remediation")
    remediate.add_argument(
        "--remediate",
        action="store_true",
        help="After Step 9, automatically re-fetch missing files and "
             "re-verify, looping until clean or --remediate-attempts is hit. "
             "Ladder: re-fetch via UniSync; if a pass recovers nothing, "
             "re-export the affected Domo card(s) and re-fetch again; then "
             "re-fix covers.",
    )
    remediate.add_argument(
        "--remediate-attempts",
        type=int,
        default=3,
        metavar="N",
        help="Max verify→remediate cycles (default 3).  UniSync downloads "
             "are intermittently flaky, so retrying often recovers tracks "
             "that failed on an earlier pass.",
    )
    remediate.add_argument(
        "--remediate-no-unisync",
        action="store_true",
        help="During auto-remediation, do NOT drive the UniSync UI — only "
             "write filtered re-run CSVs and re-fix covers.  Use when you "
             "want to inspect or run the re-fetch manually.",
    )
    remediate.add_argument(
        "--remediate-no-domo",
        action="store_true",
        help="During auto-remediation, do NOT escalate to a Domo re-export "
             "when a UniSync re-fetch makes no progress.  By default, a "
             "stalled re-fetch triggers re-exporting the affected card(s) "
             "from Domo (to pick up upstream track changes) before retrying.",
    )

    prune_grp = p.add_argument_group("Music-tree pruning")
    prune_grp.add_argument(
        "--prune-music",
        action="store_true",
        help="Before verification, reconcile every 1-ORIGINAL/Music tree "
             "against its tracklist and remove files/folders the current "
             "release does NOT reference (stale albums, leftovers, duplicate "
             "album folders, junk).  Safe by default — see --prune-mode.",
    )
    prune_grp.add_argument(
        "--prune-mode",
        choices=["report", "archive", "delete"],
        default="report",
        help="What --prune-music does: 'report' (default) previews and writes "
             "a CSV, changing nothing; 'archive' moves extras to a timestamped "
             "_PRUNED-<stamp> side folder (recoverable); 'delete' hard-removes "
             "them.  --dry-run forces 'report' regardless.",
    )

    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "list_steps", False):
        print(format_step_list())
        sys.exit(0)
    sys.exit(run_workflow(args))
