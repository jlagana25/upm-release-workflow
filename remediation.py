"""
remediation.py — Step 9b: Re-acquire missing files from the verification report
================================================================================
After Step 9 (verification.py) writes its missing-files report, this module
reads that report and re-acquires whatever it can, placing each file back
into the exact folder it was missing from.

Two kinds of missing files, two mechanisms
-------------------------------------------
1. AUDIO  (MP3, WAV, WAV w COVERS (MEDIA), Ex-US (MP3/WAV), Japan WAV)
   These have no direct download URL — they come only from UniSync.
   Remediation builds a *filtered* re-run CSV containing only the missing
   tracks (matched by Filename against the original tracklist, preserving
   each row's workAudioId), then feeds that CSV back through the existing
   UniSync automation.  UniSync re-fetches just those tracks into the same
   territory / cache / client folders as the original job.

   Driving the UniSync UI is opt-in (`run_unisync=True`).  By default the
   module only writes the re-run CSVs and prints the commands to run.

2. COVERS  (COVERS, WAV w COVERS (COVERS))
   These DO have CDN URLs.  Remediation re-runs the covers pipeline
   (Steps 6 → 7 → 8), which is idempotent: Step 6 skips covers already in
   the master library and downloads only the missing ones, Steps 7 and 8
   re-flatten and re-distribute into the correct album folders.  This is
   more robust than a report-path-driven copy because it reconstructs the
   "{AlbumNo} - {AlbumTitle}" folder names from the tracklist (the report
   only carries AlbumNo, not the title).

The verification report Type column maps to UniSync jobs like so:
    MP3                   → US MP3
    WAV                   → US WAV
    WAV w COVERS (MEDIA)  → US WAV w COVERS
    Ex-US (MP3)           → Ex-US MP3
    Ex-US (WAV)           → Ex-US WAV
    Japan WAV             → Japan WAV

Standalone test:
    python remediation.py --test --year 2026 --month 5 --part 1
                          [--report PATH]      # default: ctx.missing_report_csv
                          [--run-unisync]      # actually drive UniSync UI
                          [--overwrite]        # force cover re-download
                          [--dry-run]
                          [--debug]
"""

from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

from config import EXPORTS_DIR, ReleaseContext

# Reuse the exact column-detection logic the verifier uses, so the
# Filename column is found the same way in every CSV variant.
from tracklist_columns import _find_column, POSSIBLE_FILENAME_COLS


# ---------------------------------------------------------------------------
# Type → job mapping
# ---------------------------------------------------------------------------

# Verification report "Type" → UniSync job "name" (as defined in
# ctx.unisync_jobs).  Audio types only; covers handled separately.
MISS_TYPE_TO_JOB: dict[str, str] = {
    "MP3":                  "US MP3",
    "WAV":                  "US WAV",
    "WAV w COVERS (MEDIA)": "US WAV w COVERS",
    "Ex-US (MP3)":          "Ex-US MP3",
    "Ex-US (WAV)":          "Ex-US WAV",
    "Japan WAV":            "Japan WAV",
}

COVER_TYPES: frozenset[str] = frozenset({"COVERS", "WAV w COVERS (COVERS)"})

# Where filtered re-run CSVs are written
REMEDIATION_DIR = EXPORTS_DIR / "_Remediation"


# ---------------------------------------------------------------------------
# Report loading + classification
# ---------------------------------------------------------------------------

def _load_missing_report(
    report_path: Path, logger: logging.Logger
) -> list[dict[str, str]]:
    """Read the verification missing-files report into a list of dict rows."""
    if not report_path.is_file():
        logger.error(f"  ✗  Missing-files report not found: {report_path}")
        logger.error("     Run Step 9 (verification.py) first.")
        return []
    with report_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    logger.info(f"  Loaded {len(rows)} row(s) from {report_path.name}")
    return rows


def _classify(
    rows: list[dict[str, str]], logger: logging.Logger
) -> tuple[dict[str, set[str]], list[dict[str, str]], list[str]]:
    """
    Split report rows into:
      audio_by_job  : {UniSync job name → set of missing Filenames}
      cover_rows    : list of cover-miss report rows
      structural    : list of human-readable structural problems
                      (MISSING_CSV / MISSING_COLUMN) that block remediation
    """
    audio_by_job: dict[str, set[str]] = {}
    cover_rows: list[dict[str, str]] = []
    structural: list[str] = []

    for r in rows:
        rtype = (r.get("Type") or "").strip()

        if rtype in ("MISSING_CSV", "MISSING_COLUMN"):
            structural.append(
                f"{rtype} [{r.get('Source CSV', '?')}]: {r.get('Reason', '')}"
            )
            continue

        if rtype in COVER_TYPES:
            cover_rows.append(r)
            continue

        job = MISS_TYPE_TO_JOB.get(rtype)
        if job is None:
            logger.warning(f"  Unrecognized miss Type {rtype!r} — skipping.")
            continue

        filename = (r.get("Filename") or "").strip()
        if not filename:
            logger.warning(
                f"  Audio miss with no Filename (Type={rtype!r}) — cannot "
                f"target a re-run; skipping this row."
            )
            continue
        audio_by_job.setdefault(job, set()).add(filename)

    return audio_by_job, cover_rows, structural


# ---------------------------------------------------------------------------
# Build a filtered re-run CSV for one job
# ---------------------------------------------------------------------------

def _build_rerun_csv(
    job: dict,
    missing_filenames: set[str],
    dry_run: bool,
    logger: logging.Logger,
) -> Optional[Path]:
    """
    Filter the job's source tracklist down to only the rows whose Filename
    is in `missing_filenames`, and write the result as a re-run CSV.

    Returns the path to the written CSV (or, in dry-run, the path that
    WOULD be written), or None if the source CSV is unreadable / no rows
    matched.
    """
    src_csv = Path(job["csv"])
    if not src_csv.is_file():
        logger.error(f"    ✗  Source CSV not found for re-run: {src_csv}")
        return None

    try:
        df = pd.read_csv(src_csv, dtype=str, encoding="utf-8-sig").fillna("")
    except Exception as exc:
        logger.error(f"    ✗  Could not read {src_csv}: {exc}")
        return None

    fn_col = _find_column(list(df.columns), POSSIBLE_FILENAME_COLS)
    if not fn_col:
        logger.error(
            f"    ✗  No Filename column in {src_csv.name} — cannot filter."
        )
        return None

    filtered = df[df[fn_col].isin(missing_filenames)]
    matched = len(filtered)
    requested = len(missing_filenames)

    if matched == 0:
        logger.warning(
            f"    No rows in {src_csv.name} matched the {requested} missing "
            f"Filename(s).  (Filename mismatch between report and tracklist?)"
        )
        return None
    if matched < requested:
        logger.warning(
            f"    Matched only {matched} of {requested} missing Filename(s) "
            f"in {src_csv.name} — {requested - matched} could not be located "
            f"in the tracklist."
        )

    safe_name = job["name"].replace(" ", "_").replace("(", "").replace(")", "")
    out_path = REMEDIATION_DIR / f"{safe_name}_rerun.csv"

    if dry_run:
        logger.info(
            f"    [DRY] would write {matched}-row re-run CSV → {out_path}"
        )
        return out_path

    REMEDIATION_DIR.mkdir(parents=True, exist_ok=True)
    # utf-8-sig matches the BOM-encoded tracklists Domo produces, which
    # UniSync reads without issue.
    filtered.to_csv(out_path, index=False, encoding="utf-8-sig")
    logger.info(f"    ✓  Wrote {matched}-row re-run CSV → {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# Audio remediation
# ---------------------------------------------------------------------------

def remediate_audio(
    ctx: ReleaseContext,
    audio_by_job: dict[str, set[str]],
    dry_run: bool,
    run_unisync: bool,
    logger: logging.Logger,
) -> dict[str, str]:
    """
    For each UniSync job that has missing tracks, build a filtered re-run
    CSV and (if run_unisync) drive UniSync to re-fetch those tracks.

    Returns {job_name: status_str} where status is one of
    'csv_only' (CSV built, UniSync not run), 'ok', 'failed', 'skipped',
    or 'no_match' (no tracklist rows matched).
    """
    results: dict[str, str] = {}
    if not audio_by_job:
        logger.info("  No audio files to remediate.")
        return results

    jobs_by_name = {j["name"]: j for j in ctx.unisync_jobs}

    # Import UniSync only when needed — keeps remediation importable on
    # machines without the GUI-automation deps installed.
    run_single_job = None
    if run_unisync:
        try:
            from unisync_automation import _run_single_job as run_single_job
        except Exception as exc:
            logger.error(
                f"  ✗  Could not import UniSync automation: {exc}\n"
                f"     Falling back to CSV-only mode."
            )
            run_unisync = False

    for job_name, filenames in audio_by_job.items():
        logger.info(f"  ▶  {job_name}: {len(filenames)} missing track(s)")
        job = jobs_by_name.get(job_name)
        if job is None:
            logger.error(f"    ✗  No UniSync job definition named {job_name!r}.")
            results[job_name] = "failed"
            continue

        rerun_csv = _build_rerun_csv(job, filenames, dry_run, logger)
        if rerun_csv is None:
            results[job_name] = "no_match"
            continue

        if not run_unisync:
            results[job_name] = "csv_only"
            continue

        # Build a job that points at the filtered CSV
        rerun_job = dict(job)
        rerun_job["csv"] = str(rerun_csv)
        rerun_job["name"] = f"{job_name} (re-run {len(filenames)})"
        logger.info(f"    Driving UniSync for re-run: {rerun_job['name']}")
        status = run_single_job(rerun_job, dry_run, logger)
        results[job_name] = status

    return results


# ---------------------------------------------------------------------------
# Cover remediation
# ---------------------------------------------------------------------------

def remediate_covers(
    ctx: ReleaseContext,
    cover_rows: list[dict[str, str]],
    dry_run: bool,
    overwrite: bool,
    logger: logging.Logger,
) -> bool:
    """
    Re-acquire missing cover files by re-running the (idempotent) covers
    pipeline: download any covers absent from the master library, then
    re-flatten into /Covers and re-distribute into the WAV w COVERS album
    folders.  Returns True on success.
    """
    if not cover_rows:
        logger.info("  No cover files to remediate.")
        return True

    n_unique = len({r.get("AlbumCoverArt", "") for r in cover_rows})
    logger.info(
        f"  {len(cover_rows)} cover miss row(s) → {n_unique} unique cover(s); "
        f"re-running the covers pipeline."
    )

    try:
        from covers import (
            download_covers,
            copy_covers_to_specials,
            copy_covers_to_wav_with_covers,
        )
    except Exception as exc:
        logger.error(f"  ✗  Could not import covers module: {exc}")
        return False

    ok = download_covers(ctx, dry_run, overwrite, logger)
    ok = copy_covers_to_specials(ctx, dry_run, logger) and ok
    ok = copy_covers_to_wav_with_covers(ctx, dry_run, logger) and ok
    return ok


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def remediate_from_report(
    ctx: ReleaseContext,
    dry_run: bool,
    overwrite: bool,
    run_unisync: bool,
    logger: logging.Logger,
    report_path: Optional[Path] = None,
) -> bool:
    """
    Read the verification report and remediate every missing file it can.

    Returns True if remediation completed without error.  Note: True does
    NOT guarantee every file is now present — re-run verification.py
    afterward to confirm (this function will offer the exact command).
    """
    report_path = report_path or ctx.missing_report_csv

    logger.info("Step 9b — Remediate missing files from verification report")
    logger.info(f"  Report:      {report_path}")
    logger.info(f"  Run UniSync: {run_unisync}")
    logger.info(f"  Dry-run:     {dry_run}")

    rows = _load_missing_report(report_path, logger)
    return remediate_from_rows(
        ctx, rows, dry_run, overwrite, run_unisync, logger
    )


def remediate_from_rows(
    ctx: ReleaseContext,
    rows: list[dict[str, str]],
    dry_run: bool,
    overwrite: bool,
    run_unisync: bool,
    logger: logging.Logger,
) -> bool:
    """Remediate in-memory verification findings; reports remain audit-only."""
    if not rows:
        logger.info("  Nothing to remediate.")
        return True

    audio_by_job, cover_rows, structural = _classify(rows, logger)

    if structural:
        logger.error(
            "  ✗  The report contains structural problems that must be "
            "fixed before remediation:"
        )
        for s in structural:
            logger.error(f"      {s}")
        logger.error(
            "     These usually mean a CSV is missing or has unexpected "
            "columns — fix Step 1 / Step 9 inputs and re-verify."
        )
        return False

    total_audio = sum(len(v) for v in audio_by_job.values())
    logger.info(
        f"  Classified: {total_audio} audio track(s) across "
        f"{len(audio_by_job)} job(s); {len(cover_rows)} cover row(s)."
    )

    # --- Covers (always safe to remediate over HTTP) -----------------------
    covers_ok = remediate_covers(ctx, cover_rows, dry_run, overwrite, logger)

    # --- Audio -------------------------------------------------------------
    audio_results = remediate_audio(
        ctx, audio_by_job, dry_run, run_unisync, logger
    )

    # --- Summary -----------------------------------------------------------
    logger.info("")
    logger.info("  Remediation summary")
    logger.info(f"    Covers: {'OK' if covers_ok else 'FAILED'}")
    if audio_results:
        for job_name, status in audio_results.items():
            logger.info(f"    {job_name}: {status}")
    else:
        logger.info("    Audio: nothing to do")

    # If we built re-run CSVs but didn't drive UniSync, tell the user how.
    csv_only_jobs = [j for j, s in audio_results.items() if s == "csv_only"]
    if csv_only_jobs:
        logger.info("")
        logger.info(
            "  Re-run CSVs were written but UniSync was NOT driven "
            "(run_unisync is off)."
        )
        logger.info("  To fetch the missing audio, either re-run with "
                    "--run-unisync, or feed each CSV to UniSync manually:")
        for job_name in csv_only_jobs:
            safe = job_name.replace(" ", "_").replace("(", "").replace(")", "")
            logger.info(f"      {REMEDIATION_DIR / (safe + '_rerun.csv')}")

    # Suggest re-verification
    logger.info("")
    logger.info("  After remediation, re-verify with:")
    logger.info(
        f"      python verification.py --test --year {ctx.year} "
        f"--month {ctx.month} --part {ctx.part}"
    )

    audio_failed = any(s == "failed" for s in audio_results.values())
    return covers_ok and not audio_failed


# ---------------------------------------------------------------------------
# Verify → remediate loop
# ---------------------------------------------------------------------------

def _reexport_domo_for_jobs(
    ctx: ReleaseContext,
    job_names: set[str],
    dry_run: bool,
    logger: logging.Logger,
) -> bool:
    """
    Escalation tier: re-export the Domo card(s) that PRODUCE the tracklists
    for the given UniSync jobs, so any upstream track changes (re-IDs,
    re-activations, metadata fixes) are picked up before the next UniSync
    re-fetch.  Returns True if every needed card re-exported cleanly.

    The job → card mapping is derived from the job's source CSV matching one
    of the context's tracklist paths, so it stays correct if names change.
    """
    jobs_by_name = {j["name"]: j for j in ctx.unisync_jobs}
    csv_to_card = {
        str(ctx.us_tracklist_csv):   "us_tracklist",
        str(ctx.exus_tracklist_csv): "exus_tracklist",
        str(ctx.japan_metadata_csv): "japan_metadata",
    }

    card_keys: set[str] = set()
    for name in job_names:
        job = jobs_by_name.get(name)
        if not job:
            logger.warning(f"    No UniSync job definition named {name!r}; skipping.")
            continue
        key = csv_to_card.get(str(job.get("csv")))
        if key:
            card_keys.add(key)
        else:
            logger.warning(
                f"    Could not map job {name!r} (CSV {job.get('csv')!r}) "
                f"to a Domo card; skipping its re-export."
            )

    if not card_keys:
        logger.warning("    No Domo card(s) to re-export for the stalled job(s).")
        return False

    logger.warning(
        f"  ⤴  Escalating to Domo: re-exporting card(s) "
        f"[{', '.join(sorted(card_keys))}] in case the tracks changed upstream."
    )
    try:
        from domo_exports import run_domo_exports
    except Exception as exc:
        logger.error(f"    ✗  Could not import the Domo exporter: {exc}")
        return False

    results = run_domo_exports(ctx, dry_run, logger, only_keys=sorted(card_keys))
    if not results:
        return False
    ok = all(s in ("ok", "skipped") for s in results.values())
    if not ok:
        logger.error(f"    ✗  Domo re-export reported: {results}")
    return ok


def verify_and_remediate_loop(
    ctx: ReleaseContext,
    max_attempts: int,
    run_unisync: bool,
    overwrite: bool,
    dry_run: bool,
    logger: logging.Logger,
    run_domo: bool = True,
) -> tuple[bool, int]:
    """
    Repeatedly verify → remediate until no files are missing or
    max_attempts is reached.  Returns (all_clear, final_missing_count).

    Escalation ladder when tracks are still missing from a media folder:
      1. Re-fetch the missing tracks through UniSync (the usual fix; UniSync
         downloads are intermittently flaky, so a later pass often succeeds).
      2. If a UniSync pass makes NO progress, escalate: re-export the Domo
         card(s) that produce the affected tracklist(s) — picking up any
         upstream track changes — then re-fetch through UniSync again.
         Controlled by `run_domo` (default on; disabled via --remediate-no-domo).
      3. If it still can't be cleared by max_attempts, give up and report
         (these are tracks genuinely absent upstream).

    Each verification pass returns its findings in memory, so remediation acts
    only on what is *still* missing; the CSV is an audit artifact rather than an
    internal hand-off. In --dry-run the loop runs once as a write-free preview.
    """
    from verification import verify_all_files

    if max_attempts < 1:
        max_attempts = 1

    last_count: Optional[int] = None
    domo_reexported = False   # have we escalated to a Domo re-export this stall?

    for attempt in range(1, max_attempts + 1):
        logger.info("")
        logger.info("─" * 56)
        logger.info(f"  Verify + Remediate — attempt {attempt}/{max_attempts}")
        logger.info("─" * 56)

        # --- Verify (writes the report when anything is missing) ----------
        current_rows: list[dict[str, str]] = []
        clean = verify_all_files(
            ctx, dry_run, logger, findings_out=current_rows
        )
        if clean:
            logger.info(f"  ✓  No missing files on attempt {attempt}. Done.")
            return True, 0

        # A global dry-run is write-free, so verification deliberately did not
        # create/replace the report that normally drives remediation. Never read
        # a stale report from an earlier real run and pretend it describes this
        # preview.
        if dry_run:
            logger.info(
                "  (dry-run) Previewing remediation directly from in-memory "
                "findings; the audit report remains unwritten."
            )
            remediate_from_rows(
                ctx, current_rows, dry_run=True, overwrite=overwrite,
                run_unisync=run_unisync, logger=logger,
            )
            return False, len(current_rows)

        count = len(current_rows)
        logger.info(f"  Attempt {attempt}: {count} missing file row(s).")

        prev_count = last_count   # what was still-missing before this attempt
        if last_count is not None:
            if count < last_count:
                logger.info(
                    f"  Progress: {last_count} → {count} "
                    f"({last_count - count} recovered)."
                )
                domo_reexported = False   # progress resumed → allow future escalation
            else:
                logger.warning(
                    f"  No progress since last attempt "
                    f"({last_count} → {count})."
                )
        last_count = count

        # --- Last attempt: don't remediate again, just report -------------
        if attempt == max_attempts:
            logger.warning(
                f"  Reached max attempts ({max_attempts}) with {count} "
                f"file(s) still missing."
            )
            break

        # --- Escalation tier: UniSync re-fetch stalled → re-export Domo ----
        # If the previous UniSync pass recovered nothing (no progress), the
        # tracklist itself may be stale (a track re-IDed / re-activated
        # upstream).  Re-export the affected Domo card(s) so the next UniSync
        # pass requests the current set.  Done at most once per stall.
        stalled = (prev_count is not None) and (count >= prev_count)
        if run_domo and stalled and not domo_reexported:
            audio_by_job, _covers, _structural = _classify(
                current_rows, logger
            )
            if audio_by_job:
                ok_reexport = _reexport_domo_for_jobs(
                    ctx, set(audio_by_job.keys()), dry_run, logger
                )
                domo_reexported = True
                if ok_reexport:
                    logger.info(
                        "  Domo re-export complete — re-fetching the refreshed "
                        "tracklist(s) through UniSync."
                    )
                else:
                    logger.warning(
                        "  Domo re-export did not fully succeed; continuing with "
                        "UniSync re-fetch on the existing tracklist."
                    )
            else:
                logger.info(
                    "  Stalled, but no audio jobs in the report to escalate "
                    "(missing items may be covers); skipping Domo re-export."
                )

        # --- Remediate, then loop back to verify --------------------------
        remediate_from_rows(
            ctx, current_rows, dry_run=False, overwrite=overwrite,
            run_unisync=run_unisync, logger=logger,
        )

    final_count = last_count or 0
    if final_count == 0:
        logger.info("  ✓  All files present after remediation.")
        return True, 0

    logger.error(
        f"  ✗  {final_count} file(s) still missing after {max_attempts} "
        f"attempt(s)."
    )
    logger.error(
        f"     These are likely tracks that are absent upstream, or a "
        f"persistent UniSync issue.  Inspect the report and the WorkAudioID "
        f"column to escalate:\n     {ctx.missing_report_csv}"
    )
    return False, final_count


# ---------------------------------------------------------------------------
# Standalone test entry point
# ---------------------------------------------------------------------------

def _run_test(args) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("remediation_test")

    ctx = ReleaseContext(year=args.year, month=args.month, part=args.part)
    logger.info(f"Release context: {ctx}")

    report = Path(args.report) if args.report else None
    if args.loop:
        ok, remaining = verify_and_remediate_loop(
            ctx,
            max_attempts=args.attempts,
            run_unisync=args.run_unisync,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            logger=logger,
        )
        logger.info(f"\n  Loop finished: clear={ok}, still_missing={remaining}")
        sys.exit(0 if ok else 1)

    ok = remediate_from_report(
        ctx,
        dry_run=args.dry_run,
        overwrite=args.overwrite,
        run_unisync=args.run_unisync,
        logger=logger,
        report_path=report,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description="Re-acquire missing files listed in the verification report."
    )
    p.add_argument("--test",    action="store_true", required=True)
    p.add_argument("--year",    type=int, required=True)
    p.add_argument("--month",   type=int, required=True)
    p.add_argument("--part",    type=int, choices=[1, 2], required=True)
    p.add_argument("--report",  default=None,
                   help="Path to a missing-files report.  Default: "
                        "ctx.missing_report_csv (the most recent Step 9 run).")
    p.add_argument("--loop", action="store_true",
                   help="Repeatedly verify → remediate until clean or "
                        "--attempts is reached (handles UniSync's flaky "
                        "downloads).  Re-verifies itself between rounds.")
    p.add_argument("--attempts", type=int, default=3, metavar="N",
                   help="Max verify→remediate cycles when --loop is set "
                        "(default 3).")
    p.add_argument("--run-unisync", action="store_true",
                   help="Actually drive the UniSync UI to re-fetch missing "
                        "audio.  Without this, only the filtered re-run CSVs "
                        "are written and covers are re-fixed.")
    p.add_argument("--overwrite", action="store_true",
                   help="Force re-download of covers even if already present "
                        "in the master library.")
    p.add_argument("--dry-run", action="store_true",
                   help="Log every action without downloading, copying, or "
                        "driving any UI.")
    p.add_argument("--debug",   action="store_true",
                   help="Verbose logging.")

    args = p.parse_args()
    _run_test(args)
