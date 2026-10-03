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
- The workflow can be launched from **either machine**. On HDF1, Soundminer runs
  inline. On HDF2, Step 11 is submitted to the HDF1 Aqua LaunchAgent and
  monitored through HDF1's local queue over an SSH JSON/status channel—no Screen Sharing, Enter prompt,
  or Soundminer installation on HDF2. Detection is automatic by hostname.
- **Golden rule:** run with `--dry-run` first wherever it is supported, inspect, then run for real.

> **Before every run or test session:** confirm both Pegasus volumes are mounted
> (`ls -d "/Volumes/Pegasus32 R8 - 1" "/Volumes/Pegasus32 R8 - 2"`). After a
> reboot they may not auto-mount, which presents as "permission denied" or
> "no such file" errors that look like bugs but aren't.

> **Authentication check:** `python3 auth_manager.py --status` must report the
> current macOS user's Domo SSO, optional Domo API, UniSync, and Monday state as
> configured/private. Enroll a Domo API client with
> `python3 auth_manager.py --enroll-domo-api-keychain`; invalid credentials must
> fail before either Keychain item changes.
> New users run `--setup domo`, `--setup unisync`, and
> `--enroll-monday-keychain`. Domo/UniSync credentials are entered only into
> Microsoft/UniSync, while the Monday token is collected once through a hidden
> prompt, validated, and verified after Keychain storage. See
> `AUTHENTICATION.md`.

> **SoundMouse workbook requirement:** Step 16 exports metadata as CSV and
> converts it to XLSX, then requires native Microsoft Excel to apply Clear
> Formats and save the installed delivery copy. A finished workbook must contain
> `xl/sharedStrings.xml` and no `t="inlineStr"` worksheet cells; this is required
> by the SoundMouse uploader even though Excel itself accepts either form. The
> rewritten OOXML parts must also retain canonical default namespaces (no
> generated `ns0:` prefixes), because Excel may accept XML that SoundMouse's
> stricter workbook parser rejects. Blank CSV fields must remain absent cells,
> not explicit empty shared strings. The workflow also verifies that Excel is
> the final writer and that no metadata value changed during normalization. On
> a new Mac, open one SoundMouse workbook in Excel once and grant access to the
> delivery location before attempting an unattended Step 16 run.

---

# Part 1 — Running the Workflow

## Command quick reference

| Goal | Command |
|------|---------|
| Dry-run a normal release (preview, no changes) | `python3 upm_release_workflow.py --year 2026 --month 5 --part 1 --dry-run` |
| Run a normal release, Part 1 | `python3 upm_release_workflow.py --year 2026 --month 5 --part 1` |
| Run a normal release, Part 2 | `python3 upm_release_workflow.py --year 2026 --month 5 --part 2` |
| August transition Part 2 (all August content) | `python3 upm_release_workflow.py --year 2026 --month 8 --part 2 --full-month-content` |
| August transition Part 1 refresh (July full-month content) | `python3 upm_release_workflow.py --previous-month` (while run date is August 2026; targets the existing August 2026 Part 1 client folders) |
| Initial rolling transition | `python3 upm_release_workflow.py --start-date 2026-09-01 --end-date 2026-09-11` |
| Exact rolling 14-day delivery | `python3 upm_release_workflow.py --start-date 2026-09-12 --end-date 2026-09-25` |
| First-of-month NTT/JMD-TSS/Qwire/Scripps build | `python3 monthly_delivery_workflow.py --delivery-date 2026-10-01 --dry-run` |
| Previous month (full month), auto from today | `python3 upm_release_workflow.py --previous-month` |
| Previous month relative to a given month | `python3 upm_release_workflow.py --previous-month --year 2026 --month 6` |
| Preview the whole run incl. non-maintrack deletions | add `--dry-run` |
| Re-do a step that already produced output | add `--overwrite` |
| Resume after a failure, skipping finished steps | add the matching `--skip-*` flags |

NTT DATA, JMD/TSS, Qwire, and Scripps use the separate monthly command:
`python3 monthly_delivery_workflow.py --delivery-date 2026-10-01 --dry-run`.
Confirm it uses root `UPM-2026-10-MONTHLY`, Monday batch `UPM20261001`, and
content dates September 1–30 while every client-facing label says
`October 2026`. It must export only the four monthly cards and run only Japan
UniSync. Every rolling context must report these endpoints as `not_due`, omit
Japan UniSync, and leave no monthly partner tree in Final Packaging. Confirm a
monthly rerun preserves existing exact files and never imports audio or metadata
from a rolling release.

## What runs, and in what order

Preflight → Monday source-board API load/verification → 2/3 Folder setup → 1 Domo exports → 4 Album list DOCX/PDF →
5 UniSync → 6–8 Covers → 9 Verification → 10 Final packaging (+ SoundExchange forms) →
11 SourceAudio AIFF → 13 Non-maintrack cleanup → 15 Final metadata cross-check →
16 SoundMouse delivery → 17 BMAT custom-content delivery → 18 Monday status synchronization →
Final summary.

Steps 10–15 are gated behind the Step 9 verification: if verification fails the
finalize phase is blocked (escape with `--skip-verify`).

Each step reports `completed`, `skipped`, or `failed`. The final summary lists
every field (year/month/part, release date range, each step's status, the
missing-report path, the log-file path, and the overall status).

## A normal release (Part 1 or Part 2)

1. **Mount check** — preflight automatically reconnects the exact known HDF1
   SMB shares on HDF2 and exact attached volume names on HDF1, then checks
   `ls -d "/Volumes/Pegasus32 R8 - 1" "/Volumes/Pegasus32 R8 - 2"`.
   Test the intentional-unmount guard separately with `--no-auto-mount` (or
   the private `~/.upm_release_workflow/disable_auto_mount` sentinel); required
   Pegasus mounts must then fail closed instead of being reconnected.
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
     and monitors the SourceAudio Soundminer job automatically.
   - On **USMPSMDHDF1**, Step 11 runs inline automatically — no pause — and the
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
`--skip-soundexchange`, `--skip-sourceaudio`, `--skip-non-maintrack-cleanup`,
`--skip-final-metadata-check`, `--skip-soundmouse`, `--skip-bmat`, `--skip-monday`.

Step selectors (mutually exclusive): `--start-at STEP` resumes at a step and runs
to the end; `--only STEP` runs just that step. Valid STEP tokens:
`1, 2, 4, 5, 6, 9, 10, 11, 13, 15, 16, 17, 18` (e.g. `--only 15` runs only the
final metadata cross-check).

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

  # Target one August bridge card while retaining Aug 1–31 + Part 2 naming:
  python3 domo_exports.py --test --year 2026 --month 8 --part 2 --full-month-content --only japan_metadata

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
  - If a reduced retry repeatedly makes zero progress, Step 5 identifies the
    mapped source Domo card as stale. It runs configured upstream sources before
    the directly owning ETL (Japan 4312 → 4278; SoundMouse 3691 → 4330), waits
    for a new `SUCCESSFUL` History row at every link, replaces only that card
    export, and retries only the refreshed new/missing manifest. It attempts
    this at most once per card and fails closed on ambiguous lineage, ETL
    failure, timeout, or a second stall.
  - After every completed or failed job, exact-match cleanup closes only
    Microsoft `Working...` tabs carrying UniSync's Azure client ID.
  - If UniSync opens a `DAMS SSO` browser tab and its menu never becomes
    usable, complete the normal retained-session renewal in the newest tab:
    `UMG Employee` → saved UMG work account → saved-password autofill →
    `Sign in`. Wait for UniSync to receive the callback, then close only the
    UniSync-created `DAMS SSO` and Microsoft `Working...` tabs. Do not recrop
    the UniSync menu while the browser login is covering or blocking the app.
- **Inspect:**
  - For the first job's territory, confirm files landed in its `client_path` (printed in the log).
  - Spot-check a handful of downloaded files exist and are non-zero.
- **Rollback/cleanup:**
  - Remove downloaded files for the test territory if they shouldn't persist (delete the territory subfolder under the client path).
  - UniSync is "download missing" by nature, so re-running fills gaps rather than duplicating — generally no cleanup needed.

### Authentic SourceAudio AI-team demo

This command demonstrates the real US SourceAudio path in five written steps:
Domo's SourceAudio metadata card, a varied 20-row External Id selection,
WorkAudioId-based US WAV retrieval through normal XML-configured UniSync,
preparation of the `WAV w COVERS/MEDIA` Soundminer source, HDF1's normal
login-session agent running the SourceAudio scan and AIFF mirror, and Finder
inspection of the final partner package. It does not run Ex-US or unrelated
partners.

The demo uses a separate UPM release root named
`<release-id>-AI-SOURCEAUDIO-DEMO`, directly under the normal Specials base.
Soundminer therefore sees the same path layout as production without touching
the canonical release. A rerun archives the previous demo root under
`_AI Team Demo Archive`. The final deliverable contains the selected SourceAudio
metadata, Soundminer-produced AIFF media, and a WorkAudioId manifest under:

`3-FINAL PACKAGING/Universal Production Music <release label> AI Demo - SourceAudio`

During a Codex-assisted recording, explanations are posted in the task so Codex
can remain visible on the left. Show Domo and UniSync on the right for the first
phases, then show an authenticated HDF1 Screen Sharing window on the right for
the real Soundminer phase. No spoken narration or notification popups are used.
Soundminer retains its correctness gates and settling windows, so this authentic
demo is not forcibly terminated at five minutes and may take longer.

Do not run the real command until screen recording is active. Make sure both
Pegasus volumes are mounted, place Codex on the left and the active workflow app
on the right, and do not use the mouse or keyboard once GUI automation begins.

```bash
cd "$HOME/Documents/Scripts/Python/UPM Release WorkFlow Automation/files"
python3 ai_team_demo.py --previous-month --year 2026 --month 8
```

The `--previous-month --year 2026 --month 8` pair deliberately resolves to the
July 2026 full-month date range; it does not depend on the current date. To
rehearse only the printed plan without opening either application, add
`--dry-run`.

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
  python3 final_packaging.py --test --year 2026 --month 5 --part 1 --copy-workers 4
  ```
  (`--only "Tunesat"` / `--only "Japan"` to re-run a single partner's copy op.)
- **Expected output:**
  - Dry-run lists each copy operation (source → destination).
  - Real run logs `copy workers: 4` and copies into `3-FINAL PACKAGING/…`.
  - Destinations run one at a time; up to four files within the active
    destination copy concurrently on HDF2.
- **Inspect:**
  - `ls "{specials}/3-FINAL PACKAGING/"` — partner delivery folders populated.
  - Spot-check file counts in a partner folder against the source.
  - No hidden `*.upm-copy-*` temporary siblings remain after completion.
- **Rollback/cleanup:**
  - Delete the partner folders under `3-FINAL PACKAGING/` that were created for the test.
  - `--overwrite` re-copies; otherwise existing files are skipped.
  - `--copy-workers 1` restores the former serial copy path for diagnosis.

- **Offline concurrency regression:**
  ```bash
  python3 -m unittest test_delivery_refresh.py
  ```
  The synthetic test verifies complete content, atomic publication, clean
  restart/skip behavior, temporary-file cleanup, and rejection of zero workers.

---

## 10. SourceAudio AIFF mirror test (Step 11)

Validates Step 11 (Soundminer scan → **AIFF** mirror) for the two SourceAudio deliveries. Direct testing runs on USMPSMDHDF1, but a normal HDF2 orchestrator run submits the work to HDF1's login-session agent and monitors it without a machine switch.

**Prerequisites on USMPSMDHDF1:** Accessibility + Screen Recording, reference crops, current code verified by byte size, plus the two source trees must exist:
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
  # mirror, overriding any incompatible persisted settings:
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

  The mirror uses the SourceAudio settings (AIFF, Build Using Library then Volume, `<Filename:1>`). Soundminer persists one global set of mirror settings, so Step 11 explicitly overwrites all controls before every pass. Step 11 also rejects WAV files already present in either SourceAudio destination and stops immediately if a mirror begins producing WAV instead of AIFF.
- **Expected output:**
  - Header `─── Step 11 — Soundminer SourceAudio (AIFF) workflow ───`.
  - Per pair: records cleared → scan → mirror dialog → SourceAudio settings applied and checkboxes verified → OK → destination picker → mirror runs to completion.
  - A small scan that completes before the idle watcher begins logs that the
    result grid changed and proceeds to the exact mirror-manifest gate; it must
    not fail merely because no later animation was visible.
  - A visible Soundminer Log Window still stops the scan immediately.
  - `✓` on full success; any hard failure returns non-zero and names the failing pair.
- **Inspect:**
  - `find "{specials}/3-FINAL PACKAGING/Universal Production Music * Release - SourceAudio/Music" -name "*.aif*" | wc -l` — AIFF count matches the US (WAV w COVERS) track count.
  - Same for the Ex-US dest (`…Release - SourceAudio Ex-US/Music`).
  - Spot-check one AIFF with `ffprobe` — PCM codec, AIFF container.
- **Rollback/cleanup:**
  - Delete the AIFF trees under the two SourceAudio dests to re-test.
  - Re-running clears records and re-mirrors; the scan/mirror is repeatable and idempotent against a clean dest.

---

## Historical NBC Soundminer reference (retired)

> NBCUniversal is retired. The material below is retained only to explain
> historical recovery helpers and is not an orchestrator step or supported
> release command. Steps 12, 12.7, and 14 are not valid selector tokens.

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
    that first window is not later misreported as never having started. Import
    idle is not accepted until a row-scaled minimum runtime has also elapsed,
    preventing a temporarily static progress bar from advancing with a partial
    database. During
    Soundminer's modal progress sheets (including Embed Metadata), the app can
    temporarily expose zero Accessibility windows; a frontmost Soundminer menu
    bar is accepted as proof that the visible modal UI is still present.
  - `12.5` explicitly focuses the central record grid before ⌘A, then embeds
    via the Database menu. This prevents a post-restart Search Database focus
    from consuming Select All; the unattended monitor watches the central
    progress-sheet region at a sensitive threshold until the sheet disappears
    and the UI settles. Whole-screen motion is too diluted to detect reliable
    percentage changes in this modal.
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
    a sibling `_mirror_quarantine_*` folder for recovery/audit. Expected NBC
    filename comparisons normalize composed/decomposed Unicode identically,
    preserve repeated metadata spaces, and model v5Pro's removal of ampersands
    plus its filename-illegal set (`&<>:\"/\\|?*`).
    If refreshed metadata changes punctuation/accents, an old output is moved
    to `_filename_updates_wav_quarantine_*` only when its folded identity maps to
    one distinct expected name and that exact refreshed file already exists.
- **Inspect:**
  - `find "{nbc}/Music/WAV" -name "*.wav" | wc -l` — expected record count (e.g. 2148).
  - Step screenshots in `…/Scripts/Python/UPM Release WorkFlow Automation/_logs/soundminer_debug_steps/`.
  - On failure: `…/_logs/soundminer_failures/step12_fail_*.png` shows the exact UI state.
- **Rollback/cleanup:**
  - Mirror output lives under `{nbc}/Music/WAV` — delete that tree to re-test from clean: `rm -rf "{nbc}/Music/WAV"`.
  - The NBCUniversal Soundminer database can be re-cleared by the workflow's own `12.3 Delete all records` on the next run, so no manual DB cleanup needed.
  - If a run is interrupted mid-mirror, cancel any open Soundminer dialog before re-running.

---

## Historical NBC WAV-to-MP3 reference (retired)

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

## Historical NBC rename reference (retired)

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
  - Confirm the enrolled Domo API is attempted before any retained-browser
    fallback and the API projection reproduces the expected card columns.
  - Tracklist: `Soundmouse 06-01-26 to 07-01-26.csv` (exclusive upper bound).
  - Delivery: `2026-06-01_to_2026-06-30/{MEDIA,Covers,Metadata}` (inclusive range).
    This directory is derived from the workflow period; raw `ActivationRange`
    values in Domo do not split or rename the delivery.
  - UniSync derives its country passes from `Territory List`. Australia must be
    first whenever any row contains `OZ`; only countries needed by remaining
    uncovered rows are added. Each pass shares the same `MEDIA` directory and
    requests only the still-missing manifest. If the final required country
    makes zero progress, confirm the workflow refreshes SoundMouse's upstream
    catalog DataFlow 3691 before card-owning DataFlow 4330, re-exports the
    tracklist, and retries only the recalculated missing manifest.
  - Metadata contains only the `SoundMouseMetadata NN - … .xlsx` files named
    by the SoundMouse bucket card. Each card is downloaded as CSV first and
    converted to a clean XLSX; no Domo workbook formatting is carried forward.
    Native Excel then applies Clear Formats and saves each installed workbook;
    Step 16 opens with Excel's standard POSIX-file command, validates the
    resulting workbook name, and then keeps that bound workbook object so an
    unrelated workbook becoming active cannot redirect the save. It fails if
    Excel was not the final writer or changed a metadata value.
    Duplicate `Filename` or `UNIQUE TRACK ID` values must fail both conversion
    and final validation, including otherwise-identical duplicate rows.
  - The final SoundMouse validation unions every `Filename` and album-artwork
    filename across those selected workbooks and confirms they exist under
    `MEDIA` and `Covers`. Any missing item fails Step 16 and is listed in
    `SoundMouse <ActivationRange>_Missing.csv`; a clean report is header-only.
  - After marking SoundMouse `uploaded` or `delivered`, a refresh with additions
    creates `Missing/MEDIA` and `Missing/Metadata`; `Missing/Covers` is created
    only when a genuinely new cover filename is introduced. Audio-only additions
    and filename corrections must not duplicate existing album artwork. Each
    correction workbook contains only added-audio or added-cover rows while
    retaining the required shared-string serialization and native Excel save.
- **Part naming:** Part 1 uses `06-01-26 to 06-15-26`; Part 2 uses
  `06-15-26 to 07-01-26`. The corresponding workflow-period directories remain
  inclusive (`01_to_14` and `15_to_30`).

---

## 17. BMAT custom-content delivery test (Step 17)

BMAT follows the same resolved release-date window as the rest of the workflow,
but owns its two Domo exports and is independent of the Step 9 finalization gate.

- **Offline logic test:** `python3 -m unittest test_bmat_delivery.py`
- **Preview:** `python3 upm_release_workflow.py --start-date 2026-09-01 --end-date 2026-09-11 --only 17 --dry-run`
- Confirm the releases card is date-filtered while the submission-inventory card
  retains its saved full-inventory filter. Accepted and `ingestion_pending`
  catalogues in `_Specials/BMAT/_WORKFLOW/delivery_ledger.json` must be excluded.
- A `prepared` or `failed` batch for the same workflow and catalogue selection is
  resumed. DAMS is navigation/download-only: the step may open Albums and Audio
  pages but must never alter album information.
- The downloaded ZIP must contain each manifest WAV exactly once and no extra
  WAVs. Local package validation precedes SFTP; remote sizes are verified and
  lower-case `delivery.complete` is uploaded last.

---

## 18. Monday synchronization test (Step 18)

Validates batch selection and status planning without changing the board, then
applies the same validated plan through the Monday API.

The source board's replacement automation keys off `Batch Master`, not the
retired `Release Part` column. It must create one row
in each of Content Updates, Hard Drive Updates, and SoundMouse Updates with the
same compact start-date batch (`UPMYYYYMMDD`). No Part suffix is added.
The August 2026 Part 2 transition therefore uses `UPM20260801` for all three
packages while retaining its client-facing Part 2 name. Historical legacy
month/part records remain readable, including the older Part 2 SoundMouse-only
shape.
Confirm that the same context derives mounted-volume roots as
`UPM-2026-08-01` (and rolling periods such as `UPM-2026-09-01`); the compact
Monday batch value must not leak into the filesystem folder spelling.
For rolling runs, confirm Final Packaging partner folders abbreviate month
names while retaining the full inclusive range, for example
`Universal Production Music Sep 1–11 2026 Releases - SynchTank` and
`Universal Production Music Sep 29–Oct 12 2026 Releases - SynchTank`. Confirm
the same abbreviated range appears in partner-facing metadata filenames,
album lists, package labels, and any delivery-message subject while internal
batch IDs and audit date values remain unchanged.

The Domo Audio Batch card is the combined control inventory for Monday. Its
rows are the union of albums routed to UPM-US, UPM-ExUS, or SoundMouse, with
the `Catalog` field recording the applicable routes. Do not use this combined
card as a substitute for the separate partner tracklists used by the delivery
steps. Name each imported source-board group `UPPM Audio Batch YYMMDD` (for
example `UPPM Audio Batch 260801`) but map its Batch column to the full compact
workflow ID (`UPM20260801`).

Before a live workflow, refresh/export the date-filtered Domo Audio Batch card,
load the batch into `UPPM Audio Batch Releases`, and verify through the Monday
API that the automation created exactly one Content Updates, Hard Drive Updates,
and SoundMouse Updates item—with all required subitems—for the compact batch.
Use the Monday API for source-board loading, verification, and repair rather
than browser UI automation.

- **Offline logic test:**
  ```bash
  python3 -m unittest test_monday_sync.py
  ```
- **Preview before every live update:**
  ```bash
  python3 upm_release_workflow.py --start-date 2026-09-01 --end-date 2026-09-14 --only 18 --dry-run
  ```
- Confirm the log maps Content/HD/SoundMouse to `UPM20260901`,
  lists every proposed old/new status, and performs no mutation.
- Confirm source preflight removes NTT DATA, JMD/TSS, Qwire, and Scripps from
  rolling Content Updates through the Monday API; those subitems exist only on
  the standalone `UPMYYYYMM01` monthly item.
- A real full run advances successfully prepared package subitems to
  `Clear to Send`. It derives each main-item status from its subitems and never
  downgrades `Complete`, `Done`, `Not Needed`, or `API Client - Not Needed`.
  Rolling batches must not contain NTT DATA, JMD/TSS, Qwire, or Scripps
  subitems; source preflight removes template-created copies through the API.
  The standalone `UPMYYYYMM01` monthly item contains only those four subitems;
  they show `Working On It` while building and `Clear to Send` after all
  previous-month metadata/audio and final-package gates pass.
  After mutation it re-reads both batches and fails if any requested status is
  not confirmed.
- Confirm live checkpoints run after Step 10 (Hard Drive), after Step 15
  (Digital Fulfillment), and after each successful SoundMouse media, cover, and
  metadata phase. A checkpoint must not reuse results from an older report;
  Step 18 remains the final reconciliation and recovery entry point.
- Missing/duplicate batch rows, renamed required subitems, changed status-label
  IDs, or API errors fail Step 18 before unsafe writes. Correct the board/schema
  and rerun with `--only 18`; recovery reads the newest non-skipped outcomes
  from non-dry-run structured reports, with newer failures overriding older
  successes. Use `--monday-batch YYYYMM` for an intentional legacy override or
  `--monday-batch UPMYYYYMMDD` for an exact-date override.

---

## 19. Sony Ci delivery test

Sony Ci remains credential-blocked until the developer key is enrolled. Before
the first live delivery, use a dry-run plan and synthetic API fixtures to verify:

- Only workspace `UPM-Audio` is accepted, and the batch folder is placed under
  the correct `Content Updates`, `HD Updates`, or `SoundMouse Updates` parent.
- The folder name is `ctx.storage_root` while its `Batch` metadata value is
  exactly `ctx.release_id`.
- A new MediaBox is created rather than mutating the historical template, with
  Secure/Protected access, notifications, source download, and 30-day expiry as
  specified in `DELIVERY_ENDPOINTS.md`.
- MP3 and WAV resolve to separate private rosters of exactly 12 and 8 unique
  normalized addresses. Neither roster is written to logs or receipts, and any
  appearance of `CIMT-TV` fails before upload or notification.
- The HD notification body matches the approved template byte-for-byte after
  substituting the active client-facing release label and private delivery
  contact. Confirm the final paragraph retains its literal double asterisks.
- Creating an exact-name duplicate, notifying before upload verification, or
  receiving an ambiguous API response fails without advancing delivery state.

---

## 20. SynchTank S3 delivery test

The SynchTank endpoint is standalone while the broader post-packaging delivery
layer is developed. It uploads the final SynchTank package beneath an exact
package-name prefix and creates `delivery.complete` inside that prefix last.

- **Offline logic test:**
  ```bash
  python3 -m unittest test_synchtank_delivery.py
  ```
- **Enroll credentials once, using hidden prompts:**
  ```bash
  python3 auth_manager.py --enroll-synchtank-keychain
  ```
- **Preview the exact local package without contacting AWS:**
  ```bash
  python3 synchtank_delivery.py --start-date 2026-09-12 --end-date 2026-09-25 --dry-run
  ```
- Before a live run, confirm Step 15 passed for this exact batch. A live run
  preserves historical package prefixes, fails on unexpected objects inside
  the active prefix, uploads or resumes active package keys by exact byte size,
  verifies that complete manifest, and writes the empty marker inside the
  active prefix last. The release-local receipt and partner delivery
  state are written only after that final verification.

---

## 21. TuneSat SFTP delivery test

The standalone TuneSat endpoint uploads the complete final package, preserving
the complete package beneath its exact package-name folder in `/AudioFiles`,
with `Music/` and `Metadata/` inside that folder.

- **Offline logic test:**
  ```bash
  python3 -m unittest test_tunesat_delivery.py
  ```
- **Enroll credentials once, using hidden prompts:**
  ```bash
  python3 auth_manager.py --enroll-tunesat-keychain
  ```
- **Preview without connecting:**
  ```bash
  python3 tunesat_delivery.py --start-date 2026-09-12 --end-date 2026-09-25 --dry-run
  ```
- A live run requires completed Step 10 and Step 15 results for the exact batch,
  rejects unexpected remote files, resumes exact size-matched files, uploads
  through `.part` temporary siblings and atomic renames, verifies the final
  manifest, writes a private receipt, and marks TuneSat delivered. TuneSat does
  not receive a completion marker.

---

## 22. SoundMouse Uploader delivery test

This standalone endpoint submits the complete Step 16 release directory through
the installed Soundmouse Uploader. The target must be workspace `UPPM`, module
`Music`; any other selection blocks the upload.

- **Offline logic test:**
  ```bash
  python3 -m unittest test_soundmouse_uploader_delivery.py
  ```
- **Preview without opening the app:**
  ```bash
  python3 soundmouse_uploader_delivery.py --start-date 2026-09-12 --end-date 2026-09-25 --dry-run
  ```
- Before a live run, confirm Step 16 completed for the exact batch and the app
  has a retained sign-in. The live run refuses an existing active queue,
  explicitly selects and re-reads `UPPM` and `Music` in both the main window
  and Add panel, submits the complete release directory, and requires the newly
  inserted queue file URLs to match every local `MEDIA`, `Covers`, and
  `Metadata` file exactly. Every row must reach completed status before the
  uploader receipt is written and SoundMouse is marked `uploaded`, never
  `delivered`.
- Re-running the unified endpoint runner while SoundMouse is `uploaded` must
  return `awaiting_metadata_processing` and must not reopen or requeue the
  native Uploader.
- `--acknowledge-delivered soundmouse` must fail even when an uploader receipt
  exists. Delivery requires the separate website processor to match every
  metadata workbook and verify zero processing errors.
- A metadata-only correction may contain only `Metadata` and must queue only
  those corrected workbooks. Keep correction audit CSVs outside the selected
  package folder.

---

## 23. Post-packaging delivery runner (offline first)

The unified runner plans selected endpoints without opening a browser, app,
connector, Keychain item, or network connection:

```bash
python3 post_packaging_delivery.py --start-date 2026-09-12 --end-date 2026-09-25 --endpoints espn,soundexchange,qwire,scripps
python3 post_packaging_delivery.py --start-date 2026-09-12 --end-date 2026-09-25 --endpoints netmix
python3 post_packaging_delivery.py --delivery-date 2026-10-01 --endpoints qwire,scripps
python3 delivery_state.py --delivery-date 2026-10-01 --show
python3 -m unittest test_post_packaging_delivery.py test_netmix_portal_delivery.py
```

- Confirm dry-run creates no release-local checkpoint, receipt, draft, message,
  portal submission, or remote upload.
- ESPN requires the exact top-level folder, a matching completed transfer in My
  Transfers, and visibility beneath `from_killer_tracks/`. For a supervised
  live run, pass `--interactive-native-selection`, choose the exact canonical
  package in Signiant's native picker, and let the adapter verify the staged
  name before final Upload.
- SoundExchange processes MGB before Z Tunes, requires zero pre-existing
  pending rows, exact workbook-to-screen ISRC/count parity, and writes a
  private invalid-entry audit before refusing Submit Recordings. Use
  `--interactive-login` when a fresh login is needed; authentication and both
  submissions must remain in that one process. Verify each newest applicable
  history CSV against the exact expected ISRCs, not a displayed row count.
- Qwire preserves the original CSV when it is already below the connector
  limit, otherwise makes verified ZIP/split attachments. Every part stays
  below 500,000 records and 3 MiB, and reconstructs the original rows exactly.
- Scripps prepares one CSV or one verified single-CSV ZIP only.
- Netmix planning requires exactly one metadata CSV, exact case-insensitive
  `Filename` parity with unique WAV/AIFF basenames, and exactly one cover in
  every audio-bearing album directory. Confirm that an available API adapter is
  attempted before the portal, an uncertain API result blocks fallback, and a
  safe pre-mutation API failure can use the retained portal session. Portal
  success requires exact batch filenames and accepted terminal states from
  View Uploads before the receipt and `uploaded` state are written.
- The Outlook bridge must prepare exactly one attachment, create the draft
  through the connected Outlook Email app, and verify the Drafts copy before
  recording it. The connector has no Send action: native Outlook may send only
  after exact-release authorization, and the bridge must then reverify the
  resulting message and attachment in Sent Items before marking delivery.
  Graph reports attachment size with provider overhead, not raw local bytes;
  verify the local SHA-256 before drafting and require one exact-name,
  non-inline attachment that `fetch_attachment` successfully materializes.
  Compare connector-returned text after CRLF and trailing-space normalization
  only, because Outlook adds trailing spaces to stored plain-text lines.
- Live execution is unavailable without both `--execute` and the exact
  `--confirm-live-release` value. ESPN and SoundExchange require their
  supervised flags when native selection or a fresh login is needed; do not
  run them against a synthetic batch.
- Successful uploads become `delivered` as soon as the endpoint-specific remote
  verification succeeds. No separate partner acknowledgement is required.
  `--acknowledge-delivered` remains only for migrating older verified records
  left in the legacy `uploaded` state. Email and SoundExchange submissions
  become `delivered` only after Sent Items or Upload History verification.

---

## 24. Full end-to-end test

The real thing: all steps in order, through the orchestrator. Do a complete **dry-run first**, then the real run.

- **Command (dry-run):**
  ```bash
  python3 upm_release_workflow.py --year 2026 --month 5 --part 1 --dry-run
  ```
- **Command (real run):**
  ```bash
  python3 upm_release_workflow.py --year 2026 --month 5 --part 1
  ```
  HDF2 submits and monitors the Step 11 SourceAudio job through the HDF1
  login-session agent, then continues with Steps 13 and 15.
  - Step 13 deletes the non-maintracks in a normal run; `--dry-run` previews them only.
- **Command (real run, inline — launched ON USMPSMDHDF1):**
  ```bash
  cd "$HOME/Documents/Scripts/Python/UPM Release WorkFlow Automation/files"
  python3 upm_release_workflow.py --year 2026 --month 5 --part 1
  ```
  Run from the Soundminer machine, Step 11 runs **inline with no pause**.
  Confirm the run header shows `Machine: USMPSMDHDF1 (Soundminer machine)` and
  `Step 11 mode: inline`.
- **Command (previous-month full-month run):**
  ```bash
  python3 upm_release_workflow.py --previous-month --dry-run     # preview
  python3 upm_release_workflow.py --previous-month               # real run
  ```
  Confirm the header shows the correct prior month and a `2026-05-01 → 2026-05-31`
  full-month range, folders use explicit `UPM-2026-05-FULL` and
  `May 2026 Full` naming, and
  Domo uses its "Previous Month" preset. Inspect the same deliverables as below,
  under the full-month folders (e.g. `UPM-2026-05-FULL`).
- **Expected output:**
  - Each step logs start → end with a status; no `✗ FAILED` lines.
  - Final summary shows the full field list, every requested step `✓ completed`, `Overall status: ✓ completed`, exit code `0`.
- **Inspect (the deliverables):**
  - `{specials}/1-ORIGINAL/Metadata/` — all six Domo CSVs.
  - `{specials}/3-FINAL PACKAGING/` — all partner delivery folders populated.
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
