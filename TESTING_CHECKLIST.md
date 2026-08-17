# UPM Release Workflow — Operating & Testing Guide

This document has two parts:

1. **Running the Workflow** — how to actually run a release (normal Part 1/Part 2,
   previous-month, resuming after a failure, and which machine to run on).
2. **Testing Checklist** — a step-by-step plan for validating the automation,
   building confidence cheaply with dry-run and per-module tests before a full
   end-to-end run.

If you just need to run a release, read Part 1. If you're validating changes or
setting up a new machine, work through Part 2 top to bottom.

## Conventions used below

- All commands assume you are in the project's `files/` directory. **Both
  machines now use the same path:**
  `cd "$HOME/Documents/Scripts/Python/UPM Release WorkFlow Automation/files"`
  (On **USMPSMDHDF1**, run it in a console / Screen Sharing Terminal with an
  active GUI session for the Soundminer and UniSync steps.)
- Examples use **May 2026, Part 1** (`--year 2026 --month 5 --part 1`). Swap in your real release.
- `{specials}` = `/Volumes/Pegasus32 R8 - 1/_Specials/UPM/UPM-2026-05-P1`
- `{nbc}` = `{specials}/3-FINAL PACKAGING/Universal Production Music May 2026 Part 1 - NBC`
- The workflow can be launched from **either machine**. On HDF1, Soundminer runs
  inline. On HDF2, Steps 11–12 are submitted to the HDF1 Aqua LaunchAgent and
  monitored through HDF1's local queue over an SSH JSON/status channel—no Screen Sharing, Enter prompt,
  or Soundminer installation on HDF2. Detection is automatic by hostname.
- **Golden rule:** run with `--dry-run` first wherever it is supported, inspect, then run for real.

> **Before every run or test session:** confirm both Pegasus volumes are mounted
> (`ls -d "/Volumes/Pegasus32 R8 - 1" "/Volumes/Pegasus32 R8 - 2"`). After a
> reboot they may not auto-mount, which presents as "permission denied" or
> "no such file" errors that look like bugs but aren't.

> **Authentication check:** `python3 auth_manager.py --status` must report the
> current macOS user's Domo and UniSync state as configured/private. New users
> run `--setup domo` and `--setup unisync`; credentials are entered only into
> Microsoft/UniSync, never into this workflow. See `AUTHENTICATION.md`.

> **SoundMouse workbook requirement:** Step 16 exports metadata as CSV and
> converts it to XLSX without controlling Excel. A finished workbook must contain
> `xl/sharedStrings.xml` and no `t="inlineStr"` worksheet cells; this is required
> by the SoundMouse uploader even though Excel itself accepts either form.

---

# Part 1 — Running the Workflow

## Command quick reference

| Goal | Command |
|------|---------|
| Dry-run a normal release (preview, no changes) | `python3 upm_release_workflow.py --year 2026 --month 5 --part 1 --dry-run` |
| Run a normal release, Part 1 | `python3 upm_release_workflow.py --year 2026 --month 5 --part 1` |
| Run a normal release, Part 2 | `python3 upm_release_workflow.py --year 2026 --month 5 --part 2` |
| August transition Part 2 (all August content) | `python3 upm_release_workflow.py --year 2026 --month 8 --part 2 --full-month-content` |
| Exact rolling 14-day delivery | `python3 upm_release_workflow.py --start-date 2026-09-01 --end-date 2026-09-14` |
| Previous month (full month), auto from today | `python3 upm_release_workflow.py --previous-month` |
| Previous month relative to a given month | `python3 upm_release_workflow.py --previous-month --year 2026 --month 6` |
| Preview the whole run incl. non-maintrack deletions | add `--dry-run` |
| Re-do a step that already produced output | add `--overwrite` |
| Resume after a failure, skipping finished steps | add the matching `--skip-*` flags |

## What runs, and in what order

Preflight → 2/3 Folder setup → 1 Domo exports → 4 Album list DOCX/PDF →
5 UniSync → 6–8 Covers → 9 Verification → 10 Final packaging (+ SoundExchange forms) →
11 SourceAudio AIFF → 12 Soundminer → 12.7 NBC WAV→MP3 →
13 Non-maintrack cleanup → 14 NBC rename → 15 Final metadata cross-check →
16 SoundMouse delivery →
Final summary.

Steps 10–15 are gated behind the Step 9 verification: if verification fails the
finalize phase is blocked (escape with `--skip-verify`).

Each step reports `completed`, `skipped`, or `failed`. The final summary lists
every field (year/month/part, release date range, each step's status, the
missing-report path, the log-file path, and the overall status).

## A normal release (Part 1 or Part 2)

1. **Mount check** — `ls -d "/Volumes/Pegasus32 R8 - 1" "/Volumes/Pegasus32 R8 - 2"`.
2. **Dry-run first** — preview the whole plan without changing anything:
   ```bash
   python3 upm_release_workflow.py --year 2026 --month 5 --part 1 --dry-run
   ```
   In a from-scratch dry run, later steps will log `⚠ … not present yet` and
   `[DRY RUN] Skipping … preview` — that's expected; they can't preview against
   files the earlier (also dry-run) steps didn't actually create.
3. **Real run:**
   ```bash
   python3 upm_release_workflow.py --year 2026 --month 5 --part 1
   ```
   - On **USMPSMDHDF2**, preflight first verifies the HDF1 agent heartbeat and
     runs a non-destructive HDF1 GUI/crop/permission probe. The run then submits
     and monitors both Soundminer jobs automatically.
   - On **USMPSMDHDF1**, Step 12 runs inline automatically — no pause — and the
     full pipeline completes in one pass.
   - Step 13 **deletes** the non-maintracks in a normal run. Use `--dry-run`
     to preview the deletions without removing anything — that's the only
     safety gate; there is no separate opt-in flag.
4. **Read the final summary** — confirm `Overall status: ✓ completed`. If any
   step failed, the summary names it and prints restart guidance.

## A previous-month release (full month, no Part split)

Use this for the monthly full-month export. It covers the whole prior calendar
month (1st → last day), uses the explicit `UPM-YYYY-MM-FULL` / `Month YYYY Full`
naming, and tells Domo to use its built-in **"Previous Month"** preset.

```bash
# Auto — previous month relative to today's date:
python3 upm_release_workflow.py --previous-month --dry-run     # preview
python3 upm_release_workflow.py --previous-month               # real run

# Pinned — previous month relative to a specific month (June 2026 → May 2026):
python3 upm_release_workflow.py --previous-month --year 2026 --month 6
```

Note: with `--previous-month`, the `--part` flag is ignored (there is no Part 1/2
split). Pass **both** `--year` and `--month` to pin the reference month, or
**neither** to use today's date — passing only one is rejected.

## If a step fails

The run is restartable. The summary names the failed step and prints guidance.
To recover:

- **Fix the cause** (often a missing/unmounted volume or a missing upstream CSV),
  then **re-run the same command.** Completed steps are idempotent — they skip
  existing outputs unless you pass `--overwrite`.
- **Skip finished phases** with the matching `--skip-*` flags to resume from the
  failed step, e.g. to resume at Covers after Steps 1–5 are done:
  ```bash
  python3 upm_release_workflow.py --year 2026 --month 5 --part 1 \
    --skip-domo --skip-folder-setup --skip-album-list-doc --skip-unisync
  ```
- An **unexpected error** (not a normal step failure) is caught too: the run
  records it, names the step it happened in, prints the final summary, and exits
  non-zero — no raw traceback, and nothing left half-done that a re-run can't
  recover from.
- For a Soundminer phase failure, add `--soundminer-resume`. Completed phases
  are checkpointed, but a skip occurs only after the exact destination
  filename manifest is revalidated.

## All flags

`--year` `--month` `--part` · `--previous-month` · `--dry-run` · `--overwrite` ·
`--delete-non-maintracks` (deprecated and ignored; retained only so old commands
do not error), `--soundminer-resume`, `--no-soundminer-agent` (recovery only)

Per-step skips: `--skip-domo`, `--skip-folder-setup`, `--skip-album-list-doc`,
`--skip-unisync`, `--skip-covers`, `--skip-verify`, `--skip-final-packaging`,
`--skip-soundexchange`, `--skip-sourceaudio`, `--skip-soundminer`,
`--skip-nbc-mirror`, `--skip-non-maintrack-cleanup`, `--skip-rename`,
`--skip-final-metadata-check`, `--skip-soundmouse`.

Step selectors (mutually exclusive): `--start-at STEP` resumes at a step and runs
to the end; `--only STEP` runs just that step. Valid STEP tokens:
`1, 2, 4, 5, 6, 9, 10, 11, 12, 12.7, 13, 14, 15, 16` (e.g. `--only 15` runs only the
final metadata cross-check; `--start-at 12.7` resumes at NBC WAV→MP3).

---

# Part 2 — Testing Checklist

Work top to bottom: the **dry-run** and **per-module** tests build confidence
cheaply before the **full end-to-end** run.



## 1. Dry-run test — Part 1

Confirms the whole pipeline plans correctly for a Part 1 window without touching disk.

- **Command:**
  ```bash
  python3 upm_release_workflow.py --year 2026 --month 5 --part 1 --dry-run
  ```
- **Expected output:**
  - Header shows `Release range: 2026-05-01 → 2026-05-14`.
  - Every step logs `[DRY RUN]` lines describing intended actions.
  - Final summary lists all fields; every run step shows `✓ completed`, Soundminer/MP3 show `— skipped`.
  - `Overall status: ✓ completed`, exit code `0` (`echo $?`).
- **Inspect:**
  - The "Release date range" line = `2026-05-01 → 2026-05-14`.
  - No new files appear under `{specials}` (run `ls -la {specials}` before/after — unchanged).
- **Rollback/cleanup:** None needed — dry-run writes nothing. Only a log file is created under the `_Logs` directory; safe to leave or delete.

---

## 2. Dry-run test — Part 2

Confirms the Part 2 date window (15th → final calendar day) computes correctly, including month-length edges.

- **Command:**
  ```bash
  python3 upm_release_workflow.py --year 2026 --month 5 --part 2 --dry-run
  ```
- **Expected output:**
  - Header shows `Release range: 2026-05-15 → 2026-05-31`.
  - Same all-`[DRY RUN]` behavior as Test 1; `Overall status: ✓ completed`.
- **Inspect:**
  - Date range end = last day of the month. Spot-check edge months: February (`--month 2`) should end `-28` (or `-29` in a leap year like 2024); April should end `-30`.
  - Folder/paths in the log use `UPM-2026-05-P2` and explicit
    `… May 2026 Part 2 Release - NBC` naming.
- **Rollback/cleanup:** None — dry-run only.

---

## 3. Folder creation test

Validates Steps 2 & 3 (Specials folder tree + HD update folders).

- **Command (dry-run first, then real):**
  ```bash
  python3 folder_setup.py --test --year 2026 --month 5 --part 1 --dry-run
  python3 folder_setup.py --test --year 2026 --month 5 --part 1
  ```
  (Sub-target the steps if needed: `--step specials` or `--step hd`.)
- **Expected output:**
  - Dry-run lists each folder it *would* create.
  - Real run logs each `mkdir`; existing folders are reported as already present (idempotent), not errors.
- **Inspect:**
  - `ls -la "{specials}"` — the `1-ORIGINAL`, `2-STAGING`, `3-FINAL PACKAGING` (etc.) subtree exists.
  - No MTV-Viacom folder exists anywhere in the newly generated tree; legacy
    baseline copies are filtered during both fresh and additive setup.
  - The HD update folders exist at their configured location.
- **Rollback/cleanup:**
  - If you created folders only for the test, remove the top-level release folder you created:
    `rm -rf "{specials}"` **(only if this release is purely a test and contains no real data).**
  - Re-running is safe and non-destructive, so usually no cleanup is needed — leave the folders for the next test.

---

## 4. Domo export test

Validates Step 1 (browser-driven Domo card exports → CSV/XLSX). Requires the
current user to have completed the one-time `auth_manager.py --setup domo`
enrollment and `auth_manager.py --enroll-domo-keychain` first. A normal
workflow run must not wait for manual account/password entry.

- **Command (one card first, then all):**
  ```bash
  # Single card — fastest way to validate the mechanism (NBC metadata):
  python3 domo_exports.py --test --year 2026 --month 5 --part 1 --only nbc

  # A subset — --only is comma-separated (case-insensitive, matches key or label):
  python3 domo_exports.py --test --previous-month --only netmix_metadata,synchtank_metadata

  # Refresh both SourceAudio delivery metadata files:
  python3 domo_exports.py --test --previous-month --only sourceaudio_metadata,sourceaudio_exus_metadata

  # All cards:
  python3 domo_exports.py --test --year 2026 --month 5 --part 1
  ```
- **Cards exported:** the core tracklist/album/cleanup/NBC cards plus the partner
  metadata cards: `netmix_metadata`, `synchtank_metadata`, `scripps_metadata`,
  `qwire_metadata`, `sourceaudio_metadata`, `sourceaudio_exus_metadata`,
  `japan_jmdtss_metadata` (**.xlsx**), `soundexchange_mgb`
  (**.xlsx**), `soundexchange_ztunes` (**.xlsx**). Most write CSV; the three
  noted write XLSX (passthrough — the Domo workbook is kept as-is, not converted).
- **Expected output:**
  - Browser opens; log says `Attempting unattended Domo/Microsoft silent SSO…`,
    then `Protected Domo workspace access verified.` and
    `Logged in using the private per-user session.` A return to the Domo host
    alone is not accepted as success.
  - When Microsoft shows the saved-account tile/password form, the log reports
    redacted Keychain account selection and password submission. Neither value
    appears in the log, screenshot name, URL, process list, or report.
  - Allow up to three minutes for the unattended Microsoft→Domo redirect. If
    the saved session is expired or MFA is required, Step 1 then fails and
    reports `auth_manager.py --setup domo`; it does not pause for operator input.
  - Per card: navigation, date-range set (`05/01/2026 → 05/14/2026`), download, then `Output: …csv` or `…xlsx`.
  - Summary shows each card `✓` and the written path.
  - SourceAudio US and Ex-US exports overwrite the baseline CSVs in their
    respective delivery `Metadata` folders. `--skip-domo` rejects an unchanged
    baseline template, and any failed Domo export blocks Steps 10–15.
  - On the initial workflow export, no AIFFs exist yet and the SourceAudio delta
    check logs that it is skipped. On a later refresh, it compares tracks by
    `External Id`. A sibling `Missing` folder is rebuilt only when differences
    exist and contains `SourceAudio Missing Audit.csv` plus AIFs for additions
    and filename revisions. Removed tracks and superseded filenames are deleted
    from the local `Music` folder only after all required AIFs are ready; their
    SourceAudio service entries remain explicitly listed for manual deletion.
  - If an added track has no unique WAV source, the refreshed export is marked
    for retrieval through the same UniSync route as the initial workflow. US
    uses `United States` + the US WAV cache + `Music/WAV`; Ex-US uses `Rest of
    World` + `Music/Ex-US (WAV)`. The log must show all three values before the
    CSV is selected. If retrieval still fails, existing local media is
    preserved and the audit row says `NOT PREPARED`.
  - New US album covers use the path structure of an existing `CDNAlbumArt`
    value, replace its leaf with the refreshed cover token plus `.webp`, and
    save the result under the metadata-provided cover filename in the master,
    flat-original, and `WAV w COVERS` album locations.
  - Before a post-run catalog refresh, mark partner-system uploads separately
    from official delivery:
    `python3 delivery_state.py <date args> --mark-uploaded sourceaudio,sourceaudio_exus`.
    `--show` must list omitted partners as `pending`. Pending Step 10
    destinations are exact-synced after the additive copy, including the union
    of US and eligible Ex-US files in Tunesat. Uploaded SourceAudio, Netmix, and
    SoundMouse must create an audited `Missing` package; ordinary uploaded
    partners still refresh in place. Delivered ordinary destinations must be
    logged and skipped. Step 15 must validate uploaded SourceAudio and Netmix
    against original media plus `Missing`; SoundMouse does the same in Step 16. Use
    `--prune-music --prune-mode archive` on the refresh
    run so removals also leave the canonical original trees recoverably.
    For a recoverable standalone Tunesat sync, pass
    `--archive-extras <release>/_WORKFLOW/refresh_archive/<stamp>/Tunesat-Music`.
  - A nested authoritative label such as `BTV / pitch` must be preserved by
    prune even when the first on-disk component has stray whitespace (`BTV `).
    Verify a refresh prune reports zero false extras for that tree.
- **Inspect:**
  - NBC CSV exists and is non-trivial:
    `ls -la "{specials}/1-ORIGINAL/Metadata/UPM-US NBCUniversal Metadata Export.csv"` (should be ~MBs, not 0/167 bytes).
  - SoundExchange exports land in **2-STAGING**, not final packaging:
    `ls -la "{specials}/2-STAGING/SoundExchange/Metadata/"` → both `SoundExchange Universal Music - *.xlsx`.
  - Open one CSV and confirm it has rows for the correct date window.
  - For a SourceAudio refresh, inspect the sibling `Missing` folder and its
    audit CSV before uploading. The audit is the authoritative list of manual
    SourceAudio removals.
- **Standalone SourceAudio reconciliation (no Domo download):**
  ```bash
  python3 sourceaudio_delta.py --previous-month --territory both --dry-run
  python3 sourceaudio_delta.py --previous-month --territory both
  ```
  Use the exact date/month flags for the original delivery. The dry-run reports
  counts without copying, converting, archiving, or deleting files.
- **Rollback/cleanup:**
  - Delete the test export(s) if they shouldn't persist.
  - Re-running overwrites the same files, so cleanup is optional.
  - **Note:** this writes to the Pegasus volume — confirm the volume is writable from the pipeline machine first (a prior "permission denied: /Volumes/Pegasus32 R8 - 1" was just an unmounted volume after reboot).

---

## 5. Album List DOCX/PDF test

Validates Step 4 (generate the album-list Word doc and convert to PDF).

- **Command:**
  ```bash
  python3 album_list_doc.py --test --year 2026 --month 5 --part 1 --dry-run
  python3 album_list_doc.py --test --year 2026 --month 5 --part 1
  ```
  (PDF conversion may use `--convert-to pdf` / `--headless`; include them if your run requires the headless converter.)
- **Expected output:**
  - Dry-run reports the intended DOCX/PDF output paths.
  - Real run logs DOCX creation, then PDF conversion, then the final PDF path.
- **Inspect:**
  - DOCX and PDF both exist in the album-list output folder (check the path printed in the log).
  - Open the PDF: correct month/year title, album entries present and readable.
- **Rollback/cleanup:**
  - Delete the generated `.docx`/`.pdf` if they are test artifacts.
  - Re-running overwrites; safe to leave otherwise.

---

## 6. UniSync single-job test

Validates Step 5 (UniSync UI automation) for **one** job before running all. UniSync runs through the orchestrator (no standalone CLI), so scope it with a single job by limiting the run.

- **Command (recommended: isolate UniSync via the orchestrator, skipping everything else):**
  ```bash
  python3 upm_release_workflow.py --year 2026 --month 5 --part 1 \
    --skip-domo --skip-folder-setup --skip-album-list-doc \
    --skip-covers --skip-verify --skip-final-packaging \
    --skip-non-maintrack-cleanup --skip-soundminer --skip-rename --dry-run
  ```
  - Run the **dry-run first** to confirm the planned UniSync jobs and their CSV/territory/paths.
  - Then drop `--dry-run` to actually drive UniSync. Watch the first job complete before letting the rest proceed.
- **Expected output:**
  - Dry-run lists each UniSync job (territory, cache path, client path, CSV).
  - Real run drives the UniSync UI; per-job progress and completion logged.
- **Inspect:**
  - For the first job's territory, confirm files landed in its `client_path` (printed in the log).
  - Spot-check a handful of downloaded files exist and are non-zero.
- **Rollback/cleanup:**
  - Remove downloaded files for the test territory if they shouldn't persist (delete the territory subfolder under the client path).
  - UniSync is "download missing" by nature, so re-running fills gaps rather than duplicating — generally no cleanup needed.

---

## 7. Covers test

Validates Steps 6–8 (download album covers, copy into Specials, copy into "WAV w COVERS").

- **Command:**
  ```bash
  python3 covers.py --test --year 2026 --month 5 --part 1 --dry-run
  python3 covers.py --test --year 2026 --month 5 --part 1
  ```
  (Sub-target with `--step 6`, `--step 7`, or `--step 8` to isolate download vs. the two copy phases.)
- **Expected output:**
  - Dry-run lists covers to download and copy destinations.
  - Real run logs downloads, then the two copy passes.
- **Inspect:**
  - Cover images present in the Specials covers folder and in the "WAV w COVERS" tree (paths in the log).
  - Open a couple of `.jpg`/`.png` covers to confirm they're valid images, not error pages.
- **Rollback/cleanup:**
  - Delete downloaded/copied covers if test-only.
  - `--overwrite` re-copies; otherwise existing covers are skipped, so re-running is safe.

---

## 8. Verification test

Validates Step 9 (compare expected vs. present files; write the missing report).

- **Command:**
  ```bash
  python3 verification.py --test --year 2026 --month 5 --part 1
  ```
  (Optional `--source <path>` to point at a specific tree.)
- **Expected output:**
  - Logs counts of expected/found/missing.
  - Writes the missing-report CSV; path is logged.
  - Step status `✓ completed` when nothing is missing; reports the missing count otherwise.
- **Inspect:**
  - Open the missing report:
    `"$HOME/Documents/Scripts/Python/_Exports/_New Releases/UPM May 2026_Missing_<date>.csv"`
  - Empty (header only) = clean. Rows = genuinely missing files to chase (often via re-running UniSync/covers).
- **Rollback/cleanup:**
  - The missing-report CSV is a read-only artifact; delete it if test-only. Verification changes nothing else.
  - This is purely a read/report step — safe to run anytime.

---

## 9. Final packaging test

Validates Step 10 (copy originals into the final delivery package structure).

> **Step 10 now has two phases:** this test covers the audio/cover copy
> (`final_packaging.py`). The SoundExchange ISRC Ingest Form generation is the
> second phase of Step 10 and is covered by **Test 15** below. In a full run
> both happen under Step 10; `--skip-soundexchange` skips only the second phase.

- **Command:**
  ```bash
  python3 final_packaging.py --test --year 2026 --month 5 --part 1 --dry-run
  python3 final_packaging.py --test --year 2026 --month 5 --part 1
  ```
  (`--only "Tunesat"` / `--only "Japan"` to re-run a single partner's copy op.)
- **Expected output:**
  - Dry-run lists each copy operation (source → destination).
  - Real run logs copies into `3-FINAL PACKAGING/…`.
- **Inspect:**
  - `ls "{specials}/3-FINAL PACKAGING/"` — partner delivery folders populated.
  - Spot-check file counts in a partner folder against the source.
- **Rollback/cleanup:**
  - Delete the partner folders under `3-FINAL PACKAGING/` that were created for the test.
  - `--overwrite` re-copies; otherwise existing files are skipped.

---

## 10. SourceAudio AIFF mirror test (Step 11)

Validates Step 11 (Soundminer scan → **AIFF** mirror) for the two SourceAudio deliveries. Direct testing runs on USMPSMDHDF1, but a normal HDF2 orchestrator run submits the work to HDF1's login-session agent and monitors it without a machine switch. In a full pass Step 11 runs right before the NBC step (12).

**Prerequisites on USMPSMDHDF1:** same as the NBC Soundminer test (Accessibility + Screen Recording, reference crops, current code verified by byte size), plus the two source trees must exist:
- `{specials}/1-ORIGINAL/Music/WAV w COVERS/MEDIA/` (US source)
- `{specials}/2-STAGING/SME WAV ExUS/MEDIA/` (Ex-US source)

- **Command (dry-run first, then a supervised first run):**
  ```bash
  # Plan only — confirms the source→dest pairs and settings, touches nothing:
  python3 soundminer.py --sourceaudio --year 2026 --month 5 --part 1 --dry-run

  # --attended supervises scan progress and optionally lets you review the
  # automatically applied Mirror Settings before the script clicks OK:
  python3 soundminer.py --sourceaudio --attended --year 2026 --month 5 --part 1

  # Normal run: the SourceAudio profile is applied automatically before each
  # mirror, overriding any incompatible settings persisted by Step 12:
  python3 soundminer.py --sourceaudio --year 2026 --month 5 --part 1

  # Resume only phases whose checkpoint and destination manifest still agree:
  python3 soundminer.py --sourceaudio --resume --year 2026 --month 5 --part 1
  ```
  - Runs **unattended by default** and explicitly applies the complete SourceAudio profile before every mirror. Add `--attended` to review the applied settings and supervise the other long-running phases. (`--unattended` still exists but is a deprecated no-op.)
  - `--sourceaudio-db-shortcut` defaults to `"8"` (⌘8); pass a different number only if your SourceAudio DB is on another slot.
  - Add `--capture-steps` for per-step screenshots.
- **What it does:** for each (source → destination) pair it deletes all records → Scan Sounds into Database → Mirror to AIFF:
  1. `WAV w COVERS/MEDIA` → `…Release - SourceAudio/Music`
  2. `2-STAGING/SME WAV ExUS/MEDIA` → `…Release - SourceAudio Ex-US/Music`

  The mirror uses the SourceAudio settings (AIFF, Build Using Library then Volume, `<Filename:1>`). Soundminer persists one global set of mirror settings, so Step 11 explicitly overwrites all controls before every pass. Step 12 likewise applies its NBC/Broadcast Wave profile before mirroring. Step 11 also rejects WAV files already present in either SourceAudio destination and stops immediately if a mirror begins producing WAV instead of AIFF.
- **Expected output:**
  - Header `─── Step 11 — Soundminer SourceAudio (AIFF) workflow ───`.
  - Per pair: records cleared → scan → mirror dialog → SourceAudio settings applied and checkboxes verified → OK → destination picker → mirror runs to completion.
  - `✓` on full success; any hard failure returns non-zero and names the failing pair.
- **Inspect:**
  - `find "{specials}/3-FINAL PACKAGING/Universal Production Music * Release - SourceAudio/Music" -name "*.aif*" | wc -l` — AIFF count matches the US (WAV w COVERS) track count.
  - Same for the Ex-US dest (`…Release - SourceAudio Ex-US/Music`).
  - Spot-check one AIFF with `ffprobe` — PCM codec, AIFF container.
- **Rollback/cleanup:**
  - Delete the AIFF trees under the two SourceAudio dests to re-test.
  - Re-running clears records and re-mirrors; the scan/mirror is repeatable and idempotent against a clean dest.

---

## 11. Soundminer test (runs on USMPSMDHDF1)

Validates Step 12 (database switch → delete → import → embed → mirror). The UI process must execute in HDF1's login/Aqua session. Normally the persistent HDF1 LaunchAgent provides that session while HDF2 submits and monitors the job; direct commands remain useful for diagnostics.

> **Inline vs. agent:** on HDF1, Step 12 runs inline. On HDF2, it writes an
> atomic request in HDF1's local Application Support queue over SSH, then polls
> HDF1 heartbeats and phase/result JSON. SSH never drives the GUI. Use
> `--no-soundminer-agent` only to restore the legacy manual handoff.

**Prerequisites on USMPSMDHDF1:**
- Terminal has **Accessibility** + **Screen Recording** granted.
- The `hdfuser` Aqua session is logged in and unlocked when the job begins.
  Agent jobs hold a `caffeinate` assertion to prevent idle display sleep and
  fail closed if the console nevertheless locks during processing.
- The four/three reference crops exist (`python3 make_soundminer_crops.py` if not).
  If one live control changes, recapture only that host-specific crop with
  `python3 recapture_crop.py <crop_filename.png>` while the control is visible.
- The NBC metadata CSV exists at `{specials}/1-ORIGINAL/Metadata/UPM-US NBCUniversal Metadata Export.csv`.
- The staged WAVs exist at `{specials}/2-STAGING/SME WAV 48K NBC/MEDIA/`.
- The remote has the **current code** (verify by byte size, e.g. `wc -c soundminer.py`).
- The agent is installed and online:
  `python3 soundminer_agent.py --install` (one time), then
  `python3 soundminer_agent.py --status`. The installer selects HDF1's
  GUI-capable Framework Python and deploys its runtime under
  `~/Library/Application Support/UPM Soundminer Agent`; re-install after code
  updates so that runtime copy stays current. Jobs open as short-lived commands
  in HDF1 Terminal to inherit Terminal's Screen Recording/Accessibility grants;
  the agent tails and reports them without operator input.

- **Command (dry-run first, then attended real run):**
  ```bash
  # Plan only — confirms paths, touches nothing:
  python3 soundminer.py --test --year 2026 --month 5 --part 1 --dry-run

  # Full attended run with per-step screenshots:
  python3 soundminer.py --test --year 2026 --month 5 --part 1 --capture-steps

  # Non-destructive permissions/capture/crop diagnostic:
  python3 soundminer.py --nbc --preflight-only --year 2026 --month 5 --part 1
  ```
  - To re-test only later phases (when records are already imported/embedded):
    `--skip-delete-records --skip-import --skip-embed` (jumps to mirror).
  - Prefer `--resume` after a failure; the checkpoint skips a phase only after
    the exact output manifest is revalidated.
  - `--restart-app` is recovery-only for a stuck Soundminer UI. It requests a
    graceful quit, refuses to force-kill after 30 seconds, relaunches, and must
    be paired only with phase skips whose state was directly validated.
- **Expected output:**
  - `12.2` database switch → `✓ verified` or `⚠ proceeding` (both OK; ⌘6 is deterministic).
  - `12.3` `✓ Records cleared`.
  - `12.4` creates a runtime-only import CSV without Domo's `GRAND TOTAL`
    footer, then both pickers navigate; attended pause until you confirm import
    done (`✓ Import complete`). The original Domo CSV remains unchanged.
    Folder pickers must navigate to the target's parent, select the target
    folder row, and confirm it; navigating inside the folder leaves macOS Open
    disabled and is a hard failure. Recovery may auto-dismiss only the exact
    `The open file operation failed` alert before canceling its abandoned
    picker; unknown alerts remain untouched.
    Normal runs require no Enter. The duplicate warning is accepted, but an
    unmatched-fields dialog is accepted only for the audited
    `is_SongBasedonLyrics`, `HasVocals`, and `Is_Explicit` set. A new field is
    a hard failure. A phase that shows no positive UI activity also fails
    instead of advancing on a fixed timeout. Loss of the Soundminer window or
    a locked console is detected independently of pixel activity and stops the
    run with a targeted error. A dialog/progress signal observed by the initial
    watcher carries into the idle detector, so a short import completed inside
    that first window is not later misreported as never having started. During
    Soundminer's modal progress sheets (including Embed Metadata), the app can
    temporarily expose zero Accessibility windows; a frontmost Soundminer menu
    bar is accepted as proof that the visible modal UI is still present.
  - `12.5` explicitly focuses the central record grid before ⌘A, then embeds
    via the Database menu. This prevents a post-restart Search Database focus
    from consuming Select All; the unattended monitor waits for completion.
  - A visible **Soundminer Log Window** during import or embed is a hard failure:
    the workflow stops, leaves the log open, and saves a diagnostic screenshot.
  - `12.6` mirror dialog → settings verified → OK clicked using the dialog's
    logical Accessibility geometry → dialog closure verified → destination
    picker → mirror runs. Never feed a raw Retina image-match coordinate to a
    click or type the destination while Mirror Settings remains focused. If
    the folder-selection Return has already replaced the light picker with
    Soundminer's dark Processing Records modal, treat the picker as closed and
    let the exact output manifest—not another blind Open click—prove success.
  - `12.7` polling shows the `.wav` count climbing, then compares the exact
    expected/actual filename manifests. Equal counts with different files fail.
    Soundminer 5 may expand `Mirror Source Folder Structure` from the Pegasus
    volume root. Normalization is allowed only after that nested tree itself
    exactly matches all expected filenames: missing files move into the
    established `WAV/MEDIA/...` tree and the duplicate wrapper is retained in
    a sibling `_mirror_quarantine_*` folder for recovery/audit.
- **Inspect:**
  - `find "{nbc}/Music/WAV" -name "*.wav" | wc -l` — expected record count (e.g. 2148).
  - Step screenshots in `…/Scripts/Python/UPM Release WorkFlow Automation/_logs/soundminer_debug_steps/`.
  - On failure: `…/_logs/soundminer_failures/step12_fail_*.png` shows the exact UI state.
- **Rollback/cleanup:**
  - Mirror output lives under `{nbc}/Music/WAV` — delete that tree to re-test from clean: `rm -rf "{nbc}/Music/WAV"`.
  - The NBCUniversal Soundminer database can be re-cleared by the workflow's own `12.3 Delete all records` on the next run, so no manual DB cleanup needed.
  - If a run is interrupted mid-mirror, cancel any open Soundminer dialog before re-running.

---

## 12. WAV-to-MP3 conversion test (runs on USMPSMDHDF2)

Validates Step 12.7 (flatten the mirrored MEDIA tree if needed, then encode 320k MP3s). Requires `ffmpeg` on the pipeline machine (`which ffmpeg`).

- **Command:**
  ```bash
  python3 audio_conversion.py --test --year 2026 --month 5 --part 1 --dry-run
  python3 audio_conversion.py --test --year 2026 --month 5 --part 1
  # Re-encode everything (ignore existing MP3s):
  python3 audio_conversion.py --test --year 2026 --month 5 --part 1 --overwrite
  ```
- **Expected output:**
  - If Soundminer mirrored with "Mirror Source Folder Structure": a `Detected 'Mirror Source Folder Structure' nesting` line, then the MEDIA folder is moved up to `WAV/MEDIA` and the `_Specials/…` scaffold removed.
  - Per-file `✎ …wav → …mp3` lines; summary with `WAV files found / Converted / Skipped / Errors`.
  - `✓ Step 12.7 complete`. Errors (if any) are per-file and listed, not fatal.
- **Inspect:**
  - `find "{nbc}/Music/MP3" -name "*.mp3" | wc -l` — should equal the WAV count.
  - `ls "{nbc}/Music/WAV/"` and `ls "{nbc}/Music/MP3/"` — both show `MEDIA/` (with label subfolders), no leftover `_Specials/`.
  - Spot-check one MP3 with `ffprobe`: codec `mp3`, ~320 kb/s, sample rate preserved (≤48k).
- **Rollback/cleanup:**
  - Delete the MP3 tree to re-test: `rm -rf "{nbc}/Music/MP3"`.
  - The flatten step **moves** the WAV tree (it does not copy). If you need the original nested layout back for any reason, re-run the Soundminer mirror (Test 11); the flatten is idempotent and a flat tree is a no-op.
  - Without `--overwrite`, existing MP3s are skipped, so re-running only fills gaps.

---

## 13. Final rename test

Validates Step 14 (strip characters outside `[A-Za-z0-9_ ]` from filenames under NBC Music). Runs on the pipeline machine off the shared volume.

- **Command:**
  ```bash
  python3 cleanup.py --test --year 2026 --month 5 --part 1 --rename --dry-run
  python3 cleanup.py --test --year 2026 --month 5 --part 1 --rename
  ```
- **Expected output:**
  - Target root logged = `{nbc}/Music`.
  - Dry-run shows `old → new` for each file that would change and a `Would rename: N` summary.
  - Real run logs `✎ old → new`; summary of scanned / renamed / already-clean / collisions / errors.
  - Refuses to run (logs `✗`) if the resolved path doesn't match the exact NBC Music structure (scope guard).
- **Inspect:**
  - Pick a file that had special characters (e.g. `&`, parentheses, accented letters) and confirm they're stripped, with the extension and spaces preserved.
  - Confirm **directories were not renamed** (only files).
  - A transition/recovery NBC run may pass Soundminer's tightly validated
    `--specials-dir-override`, `--client-label-override`, and
    `--nbc-metadata-override`; the HDF1 agent must forward those arguments and
    log the resolved source and destination before touching the GUI.
  - A catalog refresh with an existing partial NBC/SourceAudio mirror must log
    `valid partial destination` and continue with `Skip Existing` only when all
    existing filenames belong to the refreshed manifest. Any unexpected,
    duplicate, or wrong-format output must still stop before GUI mutation.
- **Rollback/cleanup:**
  - Renames are in place and not automatically reversible. **Always run `--dry-run` first** and review.
  - To restore original names, re-run the Soundminer mirror + conversion (Tests 11–12), which regenerate the tree from source.
  - Collisions are skipped (logged), never overwritten — so no data is lost to a name clash.

---

## 14. Final metadata cross-check test (Step 15)

Validates Step 15: cross-references each partner deliverable's metadata sheet (or the US/Ex-US tracklist) against the audio actually present in its media folder, including both SourceAudio deliveries, and checks covers where required. Missing audio/cover = **FAIL**; extra files = warning.

- **Command:**
  ```bash
  python3 final_metadata_verification.py --year 2026 --month 5 --part 1 --dry-run
  python3 final_metadata_verification.py --previous-month
  # or through the orchestrator:
  python3 upm_release_workflow.py --previous-month --only 15
  ```
- **Expected output:**
  - Per check: `media: …`, `audio source: …`, an audio-match line, and where applicable a cover line — **per-album** for Netmix and SME WAV ExUS, **present-anywhere** for SynchTank; Tunesat/NTT Data/Discovery/ESPN are media-only.
  - Sheet footer/summary rows are filtered (logged as `skipped N summary row(s)` — this is what fixed the Tunesat `count 2044` false positive).
  - Final line: `Checked N, Skipped M, Discrepancies D, Result ✓ PASS` (FAIL writes a per-discrepancy CSV to the `_Exports` folder).
  - A trailing note lists any `3-FINAL PACKAGING` partner folder with no audio cross-check — metadata-only folders (SoundExchange, Qwire) are expected there; an **unexpected** folder is the signal to watch for.
- **Inspect:**
  - Media-absent partners log `↩ Media folder not present — skipping` (not a failure).
  - If FAIL, open the CSV report and confirm each row is a genuine miss.

---

## 15. SoundExchange ingest-form split test

Validates the SoundExchange export → ISRC ingest-form split (`split_se_ingest_forms.py`). Consolidates the two retired per-entity scripts.

> **Runs automatically in Step 10.** In a full pipeline run this same logic runs as the *second phase of Step 10* (final packaging) via `run_soundexchange_split()` — see the note in Test 9. This standalone test exercises the tool on its own (handy for re-generating the forms without re-running packaging). `--skip-soundexchange` skips this phase inside a full run.

- **Prereqs:**
  - `{specials}/2-STAGING/SoundExchange/Metadata/SoundExchange Universal Music - *.xlsx` present (from Test 4's `--only soundexchange`).
  - `{specials}/2-STAGING/SoundExchange/ISRC Ingest Form.xlsx` template present (or in the baseline `2-STAGING/SoundExchange/`).
- **Command:**
  ```bash
  python3 split_se_ingest_forms.py --previous-month --dry-run   # preview only, writes nothing
  python3 split_se_ingest_forms.py --previous-month             # both entities
  python3 split_se_ingest_forms.py --previous-month --only mgb  # one entity
  ```
- **Expected output:**
  - Prints `Template: …` (resolved from 2-STAGING, else baseline) and `Output: …` (the 3-FINAL PACKAGING SoundExchange folder) before writing anything.
  - Per entity: `Saved ISRC Ingest Form - {MGB NA LLC|Z TUNES LLC} - Part N.xlsx — K data row(s)` (≤9990 rows/part). In `--dry-run`, `[WOULD WRITE] … — K data row(s)` and no files created.
  - If an export sheet is missing: `⚠ Source sheet not found — run the Step 1 Domo export for SoundExchange first` — the run reports the missing entity and exits non-zero (a dry-run only warns). Run Test 4's `--only soundexchange` and retry.
- **Inspect:**
  - Open `… - Part 1.xlsx`: data begins at row 11 of the `Form` sheet, columns aligned with the template.
  - Files land in `{specials}/3-FINAL PACKAGING/Universal Production Music {Month} Release - SoundExchange/`.

---

## 16. SoundMouse delivery test (Step 16)

Validates the separate SoundMouse delivery: tracklist + bucket exports, one
workflow-period folder, WAV download, flat covers, and only the metadata
workbooks selected by the bucket.

- **Offline logic test:**
  ```bash
  python3 -m unittest test_soundmouse.py
  ```
- **Preview / real commands:**
  ```bash
  python3 upm_release_workflow.py --previous-month --only 16 --dry-run
  python3 upm_release_workflow.py --previous-month --only 16
  ```
- **Expected full-month naming (June 2026 example):**
  - Tracklist: `Soundmouse 06-01-26 to 07-01-26.csv` (exclusive upper bound).
  - Delivery: `2026-06-01_to_2026-06-30/{MEDIA,Covers,Metadata}` (inclusive range).
    This directory is derived from the workflow period; raw `ActivationRange`
    values in Domo do not split or rename the delivery.
  - UniSync routes rows present in the canonical US tracklist to United States,
    sends the remaining rows to Rest of World, and uses Japan only as a
    fallback. All passes share the same `MEDIA` directory.
  - Metadata contains only the `SoundMouseMetadata NN - … .xlsx` files named
    by the SoundMouse bucket card. Each card is downloaded as CSV first and
    converted to a clean XLSX; no Domo workbook formatting is carried forward.
  - The final SoundMouse validation unions every `Filename` and album-artwork
    filename across those selected workbooks and confirms they exist under
    `MEDIA` and `Covers`. Any missing item fails Step 16 and is listed in
    `SoundMouse <ActivationRange>_Missing.csv`; a clean report is header-only.
- **Part naming:** Part 1 uses `06-01-26 to 06-15-26`; Part 2 uses
  `06-15-26 to 07-01-26`. The corresponding workflow-period directories remain
  inclusive (`01_to_14` and `15_to_30`).

---

## 17. Full end-to-end test

The real thing: all steps in order, through the orchestrator. Do a complete **dry-run first**, then the real run.

- **Command (dry-run, no Soundminer pause):**
  ```bash
  python3 upm_release_workflow.py --year 2026 --month 5 --part 1 --dry-run --skip-soundminer
  ```
- **Command (real run — pauses at the Soundminer hand-off):**
  ```bash
  python3 upm_release_workflow.py --year 2026 --month 5 --part 1
  ```
  When it reaches Step 12, it prints the hand-off banner. Then, on **USMPSMDHDF1** (Screen Sharing Terminal):
  ```bash
  cd "$HOME/Documents/Scripts/Python/UPM Release WorkFlow Automation/files"
  python3 soundminer.py --test --year 2026 --month 5 --part 1 --capture-steps
  ```
  Back on **USMPSMDHDF2**, press ENTER; the pipeline verifies the WAV output, runs 12.7 conversion, then Steps 13 (non-maintrack cleanup), 14 (rename), and 15 (final metadata cross-check), then the summary.
  - Step 13 deletes the non-maintracks in a normal run; `--dry-run` previews them only.
- **Command (real run, inline — launched ON USMPSMDHDF1):**
  ```bash
  cd "$HOME/Documents/Scripts/Python/UPM Release WorkFlow Automation/files"
  python3 upm_release_workflow.py --year 2026 --month 5 --part 1
  ```
  Run from the Soundminer machine, Step 12 runs **inline with no pause** — the
  whole pipeline (1–16) completes in one pass. Confirm the run header shows
  `Machine: USMPSMDHDF1 (Soundminer machine)` and `Step 12 mode: inline`. Same
  Soundminer prerequisites as Test 11 apply (Accessibility + Screen Recording,
  crops, CSV, staged WAVs, current code).
- **Command (previous-month full-month run):**
  ```bash
  python3 upm_release_workflow.py --previous-month --dry-run     # preview
  python3 upm_release_workflow.py --previous-month               # real run
  ```
  Confirm the header shows the correct prior month and a `2026-05-01 → 2026-05-31`
  full-month range, folders use explicit `UPM-2026-05-FULL` and
  `May 2026 Full` naming, and
  Domo uses its "Previous Month" preset. Inspect the same deliverables as below,
  under the full-month folders (e.g. `UPM-2026-05-FULL`,
  `… May 2026 Full Release - NBC`).
- **Expected output:**
  - Each step logs start → end with a status; no `✗ FAILED` lines.
  - Final summary shows the full field list, every requested step `✓ completed` (Soundminer/MP3/rename `✓` after the hand-off or inline run), `Overall status: ✓ completed`, exit code `0`.
- **Inspect (the deliverables):**
  - `{specials}/1-ORIGINAL/Metadata/` — all six Domo CSVs.
  - `{specials}/3-FINAL PACKAGING/` — all partner delivery folders populated.
  - `{nbc}/Music/WAV/MEDIA/` and `{nbc}/Music/MP3/MEDIA/` — equal file counts, clean (no `_Specials/` scaffold).
  - Album list PDF present and correct.
  - Missing report empty (or only expected gaps).
  - The single run log file (path in the summary) captures the whole session.
- **Rollback/cleanup:**
  - For a pure test release, the cleanest rollback is to delete the whole release tree: `rm -rf "{specials}"` and the HD update folders — **only if this release contains no real deliverables.**
  - For a real release, do **not** bulk-delete; instead re-run individual steps with `--skip-*`/`--overwrite` to correct specific issues.
  - Because every step is idempotent (skips existing outputs unless `--overwrite`) and the run is restartable, the safest "rollback" for a partial failure is usually to fix the cause and re-run the same command, letting completed steps skip.

---

## General restart & safety notes

- **Restart after a failure:** re-run the **same command**. Completed steps skip existing outputs; use the matching `--skip-*` flags to jump past phases you know are done and resume at the failed step.
- **Dry-run everywhere first:** every step that writes supports `--dry-run`. In
  particular, Step 13 deletes non-main tracks by default on every real run;
  `--dry-run` is its preview/safety guard. `--delete-non-maintracks` is deprecated
  and ignored (a compatibility no-op), so it does not enable or disable deletion.
- **Volumes:** if anything fails with "permission denied" or "no such file" on `/Volumes/Pegasus32 R8 - 1`, check the volume is mounted before assuming a code bug — especially after a reboot.
- **Soundminer is the only non-restartable-in-place step** in the sense that it drives a GUI; if interrupted, cancel any open dialog and re-run (use `--skip-*` to resume at embed/mirror).
- **Keep the remote code in sync:** before any Step 12 run, make sure USMPSMDHDF1 has the current modules (verify by `wc -c` byte size against the pipeline machine). Stale remote copies caused several false failures historically.
- **Logs:** each orchestrator run writes one timestamped log file (path shown in the summary). Keep it with the release for an audit trail.
