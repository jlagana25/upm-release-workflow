# AGENTS.md — UPM Release Workflow

Guidance for AI coding agents (OpenAI Codex, etc.) working in this repository.
Read this before making changes. It captures how the project is structured, how
to validate edits **in a cloud sandbox that cannot run the real pipeline**, and
the hard-won invariants that are easy to break.

---

## 1. What this project is

A modular Python automation for Universal Production Music's **14-day rolling
release workflow** on macOS. One orchestrator (`upm_release_workflow.py`) runs
the workflow units numbered through Step 18 from Domo metadata exports through final partner packaging,
the independent SoundMouse and BMAT deliveries, and Monday status synchronization.
Steps 12 and 14 are intentionally retired with NBCUniversal and are not active
units or valid selector tokens.

- The August 2026 transition retains **Part 1 / Part 2** client labels.
- Normal ongoing deliveries use exact inclusive 14-day date ranges and may
  cross month boundaries.
- The one-time rolling transition covers September 1–11, 2026; the next range
  starts September 12 and resumes the normal 14-day cadence.
- August Part 2 and exact-date runs use compact start-date IDs
  (`UPMYYYYMMDD`); August keeps its established client-facing Part 2 label.
- Compact batch IDs do not control mounted-volume folder spelling. Every
  generated release component now lives beneath one Pegasus 1
  `UPM-YYYY-MM-DD` root. Hard Drive Updates lives beneath both canonical stage
  trees, while SoundMouse is a named partner package beneath
  `3-FINAL PACKAGING`.
- BMAT Step 17 packages live under the release-labeled BMAT folder in
  `3-FINAL PACKAGING`, retaining BMAT's required date/sequence subfolder names.
  Its shared delivery ledger remains under `BMAT_BASE/_WORKFLOW` and records
  each package's absolute path so resume and duplicate prevention span releases.
- Rolling Final Packaging partner folders abbreviate month names while keeping
  the full inclusive range, e.g.
  `Universal Production Music Sep 29–Oct 12 2026 Releases - SynchTank`;
  use that same abbreviated range in partner-facing delivery filenames,
  package labels, album lists, and message subjects. Internal IDs and audit
  date values retain their canonical forms.
- Everything is date-driven from `--start-date/--end-date`, legacy
  `--year/--month/--part`, `--previous-month`, or the standalone monthly
  workflow's `--delivery-date`;
  **no month/year is ever hardcoded**. Naming tokens and destination paths are all
  derived in `config.py`.

The canonical list of steps and their skip flags lives in one place:
`_STEP_UNITS` in `upm_release_workflow.py`. Treat it as the source of truth — the
`--only` / `--start-at` selectors and every `--skip-*` flag are derived from it.

---

## 2. ⚠️ READ FIRST — what you can and cannot do in this environment

This repo automates **physical macOS machines with attached storage and GUI
apps**. A cloud sandbox (Codex) has none of that. **Do not try to run the
pipeline here — it will fail, and that failure means nothing about your code.**

### Authorized local scheduled execution

Completed, validated packages for endpoints the operator has already approved
carry standing delivery authorization. Proceed as soon as their package and
endpoint-specific gates pass; do not pause for another approval. The exact
release-ID confirmation remains a machine safety interlock, not a request for
human confirmation. This standing authorization does not cover a new or
changed endpoint, changed destination, failed/ambiguous validation, changed
host key, credential/MFA challenge, deletion, or any package that is still
being built or uploaded by another process.

The `upm-overnight-workflow` Codex heartbeat is explicitly authorized to run
the live orchestrator on **USMPSMDHDF2** when it has the machine's real login
session, both Pegasus volumes, authenticated application sessions, and private
workflow state. This authorization includes the normal in-scope effects of
`python3 upm_release_workflow.py` for the exact date range pinned by the
heartbeat: mounted-volume writes, Step 13 cleanup, partner uploads, and Monday
status updates. It also authorizes persistent approval for that exact script
entry point so scheduled runs do not pause for repeated confirmation.

Before a live run, verify the host and required volumes, inspect the newest
structured reports and release state, and use the safest valid initial or
resume command. Preserve all correctness gates and fail closed. This standing
authorization does not permit fabricated media, bypassing a failed gate,
exposing credentials, inventing a different batch, or using unrelated commands.
It does not apply in a cloud/headless sandbox or on a machine without the real
GUI login session and mounted delivery storage.

The `upm-monthly-metadata-delivery` heartbeat is likewise authorized to run
`python3 monthly_delivery_workflow.py --delivery-date YYYY-MM-01` on the first
day of the delivery month when those same host, login-session, mounted-volume,
and private-authentication requirements are satisfied. That run owns only NTT
DATA, JMD/TSS, Qwire, and Scripps, and its content window is exactly the
previous calendar month. Beginning with the October 1, 2026 run, it opens the
14-day rolling batch containing that first day, builds only those four partner
packages inside that batch, and leaves every other partner for the normal
post-cutoff run. It may synchronize that rolling Monday item, but it may not send email, notify a MediaBox,
or perform another final external submission without explicit authorization
for that exact monthly release ID.

Specifically, these **cannot run in the sandbox** and must not be used as a
validation signal:

- **GUI automation** — Steps 5 and 16 (UniSync) and Step 11 (Soundminer) drive
  desktop apps via `pyautogui` + `opencv` screen-matching. They need a real macOS
  GUI session, the apps installed, and per-machine reference screenshots. None
  exist here.
- **Mounted storage** — every real input/output path is on two Thunderbolt
  volumes, `/Volumes/Pegasus32 R8 - 1` (consolidated release roots) and
  `/Volumes/Pegasus32 R8 - 2` (shared HD baselines/caches and legacy output).
  They are not present in the sandbox.
- **Domo exports** — Step 1, Step 16, and BMAT use explicit public Data API
  projections with the separate workflow-owned API Keychain pair and fail
  closed on API errors. The retained authenticated browser is used only for
  DataFlow lineage/run recovery, never as an export fallback.
- **SoundMouse CSV→XLSX normalization** — Step 16 exports metadata cards as CSV,
  builds clean XLSX workbooks with an OOXML shared-string table, then requires
  native Microsoft Excel to Clear Formats and save the installed copy because
  the SoundMouse uploader rejects Python-only serialization even when the OOXML
  is structurally valid. Excel is a real-machine-only dependency.
- **DOCX→PDF** — Step 4 shells out to LibreOffice/Word.

What you **can** do here (this is where you add value):

- Read, refactor, and improve any Python logic.
- Fix bugs in the pure-data code paths (CSV/XLSX handling with pandas/openpyxl,
  path derivation, filtering, verification, cleanup logic).
- Add/adjust CLI flags, logging, error handling.
- Write and run **unit-style tests against synthetic temp filesystems** (see §4).
- Run `smoke_test.py` (offline; imports every module and checks wiring).
- Update docs.

All heavy/GUI/browser imports (`pyautogui`, `cv2`, `PIL`, `playwright`) are
**imported lazily inside functions**, so every module imports cleanly headless.
Keep it that way — never move those imports to module top level, or you'll break
`smoke_test.py` and any headless use.

---

## 3. Environment setup

Run `./setup.sh` (or paste its contents into the Codex environment setup script).
It installs only the dependencies needed for code work and tests — the GUI
(`pyautogui`, `opencv-python`, `Pillow`) and browser (`playwright`) packages are
intentionally omitted because they can't run headless and every import of them is
lazy. Core deps: `python-docx`, `pandas`, `openpyxl`, `numpy`, `requests`,
`python-dateutil`, plus `urllib3<2` for Apple system Python's LibreSSL runtime.

---

## 4. How to validate changes (there is no "run the pipeline")

Use these three, in order of speed:

1. **Compile** what you touched:
   ```bash
   python3 -m py_compile <file>.py
   ```

2. **Smoke test** — imports every first-party module and checks cross-module
   wiring (the shared column detector, the step registry vs. skip flags, etc.).
   This is the fast regression guard; keep it green:
   ```bash
   python3 smoke_test.py
   ```
   If you add a new `--skip-*` flag, it **must** be registered in `_ALL_SKIP_ATTRS`
   or the smoke test fails by design.

3. **Synthetic-filesystem unit tests** — the real pipeline touches Pegasus
   volumes, so to test file-moving/filtering logic you build a throwaway tree in
   `tempfile.mkdtemp()`, run the function against it, and assert on the result.
   This is the established pattern in this project. Example shape:
   ```python
   import tempfile, shutil
   from pathlib import Path
   import cleanup as c

   tmp = Path(tempfile.mkdtemp())
   # ...build a fake MEDIA tree with a few .mp3 files...
   # ...call the function with dry_run where destructive...
   # ...assert copied/deleted/kept counts and that the right files exist...
   shutil.rmtree(tmp)
   ```
   Prefer `dry_run=True` first for anything destructive, then a real run against
   the temp tree. Delete the temp tree at the end.

**Never** treat "the workflow didn't run" as a failure of your change. Validate
with the three tools above.

---

## 5. The 18 steps (and where things live)

| Step | Name | Module | Runs in sandbox? |
|------|------|--------|------------------|
| 1 | Domo metadata exports | `domo_exports.py` | Logic testable; live API needs Keychain/network |
| 2 & 3 | Folder setup (Specials + HD Updates) | `folder_setup.py` | No (volumes) |
| 4 | Album list DOCX + PDF | `album_list_doc.py` | Partial (PDF needs LibreOffice) |
| 5 | UniSync music export | `unisync_automation.py` | No (GUI) |
| 6–8 | Album covers (download → flatten → into WAV w COVERS) | `covers.py` | Logic testable |
| 9 | Verification (**gate** for 10–15) | `verification.py`, `remediation.py` | Logic testable |
| 10 | Final packaging **+ SoundExchange ingest forms** | `final_packaging.py`, `split_se_ingest_forms.py` | Logic testable |
| 11 | SourceAudio AIFF mirror | `soundminer.py` | No (GUI) |
| 12 | **Retired — NBCUniversal** | — | Not an active workflow unit |
| 13 | Non-main-track cleanup | `cleanup.py` | Logic testable |
| 14 | **Retired — NBCUniversal** | — | Not an active workflow unit |
| 15 | Final metadata cross-check | `final_metadata_verification.py` | Logic testable |
| 16 | SoundMouse delivery | `soundmouse.py` | Data/filesystem logic testable; live Domo API + UniSync cannot run |
| 17 | BMAT custom-content delivery | `bmat_delivery.py` | Selection/package logic testable; live Domo API, DAMS, and SFTP need real services |
| 18 | Monday status synchronization | `monday_sync.py` | Planning testable; live API needs per-user token/network |

Steps **10–15 are gated behind Step 9 and Step 1**: if verification or a Domo
export fails on a real run, they're skipped. When Step 9
is skipped entirely (e.g. `--start-at 13`), `verify_failed` defaults to `False`,
so it does not block finalization; a failed Step 1 still does.

Step **16 is independent of the Step 9 gate**. It exports the SoundMouse
tracklist and bucket, builds one directory from the resolved workflow start and
end dates, runs additive country-specific WAV UniSync jobs into `MEDIA`,
downloads flat covers, and exports only metadata cards selected by bucket
codes 01–10. Those metadata cards are downloaded as CSV and converted into
clean XLSX workbooks, so Domo formatting is never carried forward. Raw Domo
`ActivationRange` values never control or split the delivery directory. Its final gate
unions the audio and cover filenames across every selected metadata workbook,
checks them against `MEDIA` and `Covers`, writes an auditable missing-items CSV,
and fails Step 16 when a referenced file is absent.

SoundMouse territory routing is controlled by its own tracklist's
`Territory List`, not by membership in the US/Ex-US/Japan delivery catalogs.
Australia is the first pass for every row containing `OZ`; a greedy cover adds
only the other country passes required for rows Australia cannot supply. The
passes share one destination and form a fallback chain, so each later country
requests only the unresolved manifest. If the final required country has zero
progress, Step 16 refreshes its tracklist's owning Domo ETL/card once and
retries the recalculated manifest. Do not restore Rest-of-World/Japan routing
for SoundMouse: those are separate catalog partitions, not its delivery
territories.

**SoundExchange note:** `split_se_ingest_forms.py` runs automatically as the
**second phase of Step 10** via `run_soundexchange_split(ctx, dry_run, logger)`.
It's also runnable standalone (`python3 split_se_ingest_forms.py --previous-month`,
`--dry-run` supported). `--skip-soundexchange` skips just that phase;
`--skip-final-packaging` skips all of Step 10. `--only 10` runs both phases.

Supporting modules: `config.py` (release context + **all** paths + partner
destinations), `tracklist_columns.py` (shared CSV/XLSX column-name detection — the
single source; don't reinvent per module), `logging_utils.py` (step logging
helpers), `unisync_prefs.py` (writes UniSync's XML prefs), `remote_runner.py`
(`soundminer_agent.py` supersedes its SSH/manual path for normal runs),
`soundminer_agent.py` (HDF1 Aqua LaunchAgent + SSH JSON/status protocol),
`delivery_state.py` (release-local pending/uploaded/delivered partner status),
`workflow_report.py` (structured JSON run report), `sourceaudio_delta.py`
(post-delivery SourceAudio metadata/audio reconciliation),
`monday_sync.py` (exact-batch Monday Status planning and API writes),
`bmat_delivery.py` (date-scoped custom releases, DAMS downloads, ledger, SFTP),
`synchtank_delivery.py` (standalone direct-root S3 upload with final trigger),
`tunesat_delivery.py` (standalone complete-package SFTP delivery),
`soundmouse_uploader_delivery.py` (standalone native-app SoundMouse upload),
`soundmouse_web_delivery.py` (UPPM website workbook processing and final receipt),
`post_packaging_delivery.py` (unified guarded endpoint runner),
`monthly_release_migration.py` (one-time fail-closed adoption of completed
standalone monthly artifacts and receipts into their rolling owner),
`release_storage_consolidation.py` (copy/hash/verify migration of legacy
SoundMouse and HD output into the main Pegasus 1 release root),
`release_archiver.py` (same-volume `.tar.zst` retention with full manifest
verification; keeps the current and previous two calendar months),
`delivery_common.py` (manifest/checkpoint/receipt safety primitives),
`espn_delivery.py` (Media Shuttle folder delivery),
`soundexchange_delivery.py` (two-registrant portal submission),
`email_deliveries.py` (Qwire/Scripps Outlook handoff preparation),
`outlook_connector_bridge.py` and `skills/outlook-delivery-bridge/SKILL.md`
(Codex Outlook app handoff, send boundary, and Sent Items verification),
`prune.py` (removes files from prior months the current tracklist no longer
references — the counterpart to verification).

Dev/setup utilities (not part of the pipeline): `smoke_test.py`,
`make_soundminer_crops.py` / `recapture_crop.py` (capture the per-machine
reference screenshots), `diagnose_crop.py` (screen-match diagnostic).

---

## 6. CLI

```bash
# Full run for an explicit month/part:
python3 upm_release_workflow.py --year 2026 --month 5 --part 1

# Full run for the previous calendar month (no year/month needed):
python3 upm_release_workflow.py --previous-month

# Resume from a step (runs it and everything after):
python3 upm_release_workflow.py --previous-month --start-at 13

# Run exactly one step/unit:
python3 upm_release_workflow.py --previous-month --only 10

# Preview without changing anything:
python3 upm_release_workflow.py ... --dry-run
```

`--previous-month` is the full-month shortcut. Every run has a canonical ID:
`UPM-2026-07-P1`, `UPM-2026-07-P2`, or `UPM-2026-07-FULL`. Partner-facing
folders use explicit `Part 1`, `Part 2`, or `Full` labels and always describe
the release/content month, even when processing happens in the next month.

- **Selectors** (mutually exclusive): `--start-at <token>`, `--only <token>`.
  Tokens come from `_STEP_UNITS` (`1,2,4,5,6,9,10,11,13,15,16,17,18`).
- **Per-step skips**: `--skip-domo`, `--skip-folder-setup`, `--skip-album-list-doc`,
  `--skip-unisync`, `--skip-covers`, `--skip-verify`, `--skip-final-packaging`,
  `--skip-soundexchange`, `--skip-sourceaudio`, `--skip-non-maintrack-cleanup`,
  `--skip-final-metadata-check`, `--skip-soundmouse`, `--skip-bmat`, `--skip-monday`.
- **Step 13 deletes by default on real runs**: non-main-track cleanup passes
  `actually_delete = not args.dry_run`, so `--dry-run` is the preview/safety
  guard. `--delete-non-maintracks` is deprecated and ignored; it remains only
  so older commands do not error. Folder overwrite still requires `--overwrite`.
- Soundminer runs **unattended by default**. From HDF2, the HDF1 login-session
  agent must be healthy before preflight passes; never replace it with an
  SSH-spawned GUI process (macOS TCC blocks that capture context).
  `--soundminer-attended` re-adds
  optional supervision pauses. Because Soundminer persists one global Mirror
  Settings state, Step 11 explicitly applies the SourceAudio AIFF profile
  before every mirror and may not trust the previously persisted state.

When adding a step: update `_STEP_UNITS`, add the block in the orchestrator, set a
`results[...]` status in every branch (run/skip/blocked), add a summary row in
`_render_final_summary`, and register any new skip flag in `_ALL_SKIP_ATTRS`.

---

## 7. The two machines (context — you can't touch them from here)

| Host | Role | Project path |
|------|------|--------------|
| **USMPSMDHDF2** | Pipeline machine (submits/monitors Soundminer agent jobs) | `~/Documents/Scripts/Python/UPM Release WorkFlow Automation/files` |
| **USMPSMDHDF1** | Soundminer machine (login-session agent executes Steps 11–12) | `~/Documents/Scripts/Python/UPM Release WorkFlow Automation/files` |

Both now use the **same path**, so any shell/git command is identical on either.
Code locates its own files relative to `Path(__file__)` (`_REPO_ROOT` /
`_FILES_DIR`), so moving the folder doesn't break anything. `config.py`'s
`is_soundminer_machine()` uses the hostname to decide whether Steps 11–12 run
inline or are submitted to HDF1's login-session agent.

---

## 8. Not in the repo (external / per-machine)

- **The Pegasus volumes** (`/Volumes/Pegasus32 R8 - 1` and `- 2`) — all real
  release data. Paths are defined in `config.py`; the volumes themselves are not
  in git and not in the sandbox.
- **Known-volume recovery is automatic.** `volume_mounts.py` runs before the
  main workflow, standalone folder setup, and every HDF1 Soundminer job. HDF2
  reconnects only the exact HDF1 SMB shares for the two Pegasus roots plus the
  optional `Documents` compatibility share; HDF1 uses `diskutil` only for an
  exact, uniquely discovered attached volume name. No credential is stored or
  passed by the workflow. An intentional unmount must use
  `--no-auto-mount`, `UPM_DISABLE_AUTO_MOUNT=1`, or the private
  `~/.upm_release_workflow/disable_auto_mount` sentinel. Required Pegasus
  failures remain fail-closed; optional Documents/UPM Builds failures warn.
- **One release means one physical root and one stage hierarchy.** Generated
  SoundMouse and Hard Drive output may not be restored to the legacy Pegasus 2
  output folders or placed in parallel stage trees. Shared HD baselines and UPM
  caches remain on Pegasus 2. Every new release writes Hard Drive Updates to
  `2-STAGING/Hard Drive Updates` and `3-FINAL PACKAGING/Hard Drive Updates`,
  and SoundMouse to its named partner package under `3-FINAL PACKAGING`.
  Legacy moves use `release_storage_consolidation.py`: use a conflict-checked
  atomic rename on the same volume, or copy to a hidden sibling and hash every
  source/destination file across volumes; then rewrite private path references
  and remove the old folder only after the new location is verified.
- **Retention is three calendar months on the same Pegasus disk.** The current
  month and previous two calendar months stay expanded. A release whose end
  date predates that window may be archived only when its newest real report is
  completed and every recorded delivery state is delivered. The archive is a
  verified `.tar.zst` plus a private full-file manifest under `_ARCHIVE`; the
  expanded source is removed only after every archived file hashes identically.
  Baselines, caches, audit/recovery folders, active or incomplete releases,
  correction work, and unrecognized legacy folders are never auto-selected.
- **Release CSVs/tracklists** — the Domo exports live under
  `~/Documents/UPM Tracklists/Release Lists/` **per machine**, not in git.
- **Reference screenshots** — GUI matching reads crops from
  `screenshots/<HOSTNAME>/` (per machine). These *are* in git but are only used at
  real runtime on the Macs.

---

## 9. Invariants & hard-won gotchas (don't regress these)

- **Whitespace in source folder names.** Real deliveries occasionally carry a
  stray leading/trailing space in a label folder (e.g. `"BTV "` with a `pitch`
  sub-folder). Label matching in `final_packaging.py` and index/keeper logic in
  `cleanup.py` compare on the **whitespace-stripped** name. An exact match silently
  drops a whole album. Keep the `.strip()` comparisons.
- **Step 10 concurrency is bounded and HDF2-local.** File copies use four
  workers by default within the active destination, while partner destinations
  remain sequential. `--copy-workers 1` restores serial behavior. Do not turn
  this into whole-operation fan-out or HDF1 delegation: the Pegasus paths are
  SMB mounts on HDF2 and unbounded concurrent destinations can overwhelm them.
  Preserve the hidden temporary-sibling copy plus atomic rename so interrupted
  transfers cannot leave partial files that an idempotent restart would skip.
- **The Tunesat keep-list spans two deliveries.** `cleanup.py`'s auto-fill for
  missing keepers searches **both** `1-ORIGINAL/Music/MP3/MEDIA` (US) **and**
  `1-ORIGINAL/Music/Ex-US (MP3)/MEDIA` (Ex-US), because the Tunesat folder holds
  US MP3 plus Ex-US eligible labels. Matching is by normalized **basename**
  (extension stripped, lowercased), not full path.
- **Per-machine screenshots.** `_img(name)` resolves to
  `screenshots/<current_hostname()>/name`. The pipeline machine (HDF2) needs only
  the 10 UniSync crops; the Soundminer machine (HDF1) needs those plus 3 required
  + 4 optional Soundminer crops. There are no root-level shared crops.
- **Lazy GUI/browser imports** (see §2) — never move them to module top level.
- **Authentication is unattended only through retained per-user sessions.**
  Interactive Domo/Microsoft MFA is allowed only in `auth_manager.py --setup`;
  normal workflow runs use bounded silent SSO plus the two workflow-owned Domo
  Login Keychain items and fail rather than pausing for MFA. Credential values
  must never enter argv, logs, URLs, screenshots, reports, or repository files.
  UniSync reuses its own current-user Keychain session.
- **Recover an expired UniSync browser session through its normal SSO route.**
  If launching UniSync opens a Chrome `DAMS SSO` tab and the app remains on a
  login screen, do not diagnose the covered UniSync menu as a shifted crop.
  In the newest UniSync-created tab, click `UMG Employee`, select the saved UMG
  work account on Microsoft's `Pick an account` page, use the browser's saved
  password autofill, and click `Sign in`. Wait until UniSync receives the
  callback and can open its CSV picker. Never read, copy, log, or expose the
  password. Close only the UniSync-created `DAMS SSO` and Microsoft
  `Working...` tabs after the callback; preserve all pre-existing browser tabs.
- **Clean up agent-created working artifacts when they are no longer needed.**
  Close browser tabs, app windows, and terminal sessions created for the task
  once they are idle. Remove temporary staging folders, caches, partial files,
  diagnostic captures, and downloaded installers or code copies after the
  result is verified. Periodically include retained worktrees, Downloads, and
  the user's private temporary directory in this audit, not only the current
  checkout. Prefer moving obsolete user-visible downloads to Trash so they are
  recoverable. Preserve pre-existing sessions and files, active processes,
  unfinished work (including SourceAudio checkpoints and review outputs),
  delivery packages, receipts, audit evidence, logs, and structured reports.
- **MTV-Viacom and NBCUniversal are retired.** The shared Specials baseline may
  still contain their historical folders, so both fresh `copytree` and additive baseline merges must
  filter names through `config.is_retired_partner_name()`. Do not delete old
  release folders as part of setup.
- **Every `--skip-*` flag** must appear in `_ALL_SKIP_ATTRS` (enforced by
  `smoke_test.py`). Sub-phase flags without their own step token (like
  `--skip-soundexchange`) may need special handling in `_apply_step_selectors`
  (e.g. `--only 10` explicitly un-skips SoundExchange).
- **Column detection** goes through `tracklist_columns.py`. Don't hardcode column
  names in individual modules.
- **Refreshed SourceAudio metadata reconciles by External Id.** Once uploaded
  AIFFs exist, a later US or Ex-US Domo export rebuilds the sibling `Missing`
  package with additions and filename replacements. It may remove metadata
  removals and superseded filenames from the local `Music` tree only after all
  replacement files are ready. It never deletes from the SourceAudio service;
  those actions remain in `SourceAudio Missing Audit.csv` for manual handling.
  Missing additions must use the original UniSync routes (US = United States →
  `Music/WAV`; Ex-US = Rest of World → `Music/Ex-US (WAV)`) before propagation
  into SourceAudio staging. Never point UniSync directly at `WAV w COVERS` or
  Ex-US staging. New US covers derive a `.webp` URL from the current
  `CDNAlbumArt` structure and retain the metadata cover filename locally.
  Missing/ambiguous masters fail closed and preserve the existing local media.
- **Catalog refresh behavior is explicit, not inferred from folder contents.**
  `delivery_state.py` stores per-partner pending/uploaded/delivered state in the
  release's `_WORKFLOW/delivery_status.json`. A fully verified upload is marked
  `delivered` immediately. Both `uploaded` (legacy/intermediate) and `delivered`
  are correction boundaries for SourceAudio US/Ex-US, Netmix, and SoundMouse;
  those partners use audited `Missing` packages because metadata has already
  been mapped to media in the remote service. Delivered ordinary partners are
  skipped. Step 15 validates
  SourceAudio and Netmix against original media plus `Missing`; Step 16 applies
  the same union to SoundMouse. A refresh that includes removals should also use
  recoverable `--prune-music --prune-mode archive` so canonical sources match the new
  tracklists before Step 10. Never assume a populated folder was delivered.
  Standalone Tunesat cleanup can use `--archive-extras` when recovery is safer
  than permanent deletion.
- **Repeated UniSync zero-progress refreshes Domo once.** When a reduced retry
  repeatedly returns every requested track as NOT FOUND, Step 5 treats the
  mapped source Domo card/ETL as stale. It first runs configured upstream
  source DataFlows (Japan 4312; SoundMouse 3691), then follows Card → DataSet →
  Edit ETL to run the directly owning transform (Japan 4278; SoundMouse 4330).
  Every run must produce a newest `SUCCESSFUL` History row before replacing
  only that card export and retrying the recalculated missing set.
  Lineage must resolve exactly once, and each card may refresh only once per
  Step 5 run. A failed/ambiguous refresh or second stall fails closed. Explicit
  `--skip-domo` disables this recovery. After each UniSync job, close only
  Microsoft `Working...` tabs matching UniSync's exact Azure client ID.
- **Delivery metadata must replace baseline templates.** Step 1 has dedicated
  SourceAudio US and SourceAudio Ex-US cards in addition to the other partner
  metadata cards. A failed export blocks Steps 10–15, and `--skip-domo` must not
  accept an unchanged baseline metadata file as a valid current export.
- **Soundminer must fail closed.** Never restore count-only mirror success,
  generic OK/Yes clicking, or a no-activity timeout that proceeds anyway.
  Metadata/source and destination filename manifests are correctness gates.
  A small scan can finish during the initial dialog watcher; the record-grid
  change from the pre-scan empty database is a valid positive activity signal,
  but the scan poll must still stop on a Soundminer Log Window. Embed activity
  is measured in the central progress-sheet region with its lower dedicated
  threshold; whole-screen averaging can miss a visibly advancing percentage.
- **SoundMouse XLSX must fail closed.** CSV conversion is only an intermediate
  stage. Every installed full or correction workbook must finish with native
  Excel Clear Formats + Save, retain identical metadata values, contain shared
  strings with no inline/empty-string cells, and identify Microsoft Macintosh
  Excel as the final writer. A structurally valid Python-only XLSX is not upload
  compatible. Open through Excel's standard POSIX-file command, bind the
  resulting active workbook once, verify its exact filename, and drive that
  bound object for Clear Formats, save, and close. Excel's `open workbook`
  command can silently leave an unrelated workbook active.
  An incremental refresh may proceed only when the destination is a strict
  subset of the expected manifest (missing only); unexpected, duplicate, or
  wrong-format outputs still fail before GUI mutation.
- **SoundMouse Domo reads are API-only.** Query the owning SoundMouse catalog
  DataSet for the exact release dates, project the tracklist, bucket, and
  metadata card schemas locally, and fail closed on API errors. Never use card
  download as an automatic fallback or put API credentials or tokens in argv,
  environment variables, URLs, logs, reports, or repository files.
- **Soundmouse Uploader must target UPPM/Music.** The post-Step-16 native-app
  delivery always explicitly selects and re-verifies workspace `UPPM` and
  module `Music` before submitting the complete SoundMouse release directory.
  Require the newest real Step 16 result to be completed, refuse an existing
  active queue, compare every newly inserted queue file URL with the exact
  local `MEDIA`/`Covers`/`Metadata` manifest, and require every row to reach the
  app's completed state. Native-uploader completion marks SoundMouse only
  `uploaded`; it is not delivered until every metadata workbook has also been
  processed successfully in the SoundMouse website. Do not trust a generic
  completion dialog or count, read credentials, or accept a different
  workspace/module.
  A metadata-only correction uploads only nonempty `Metadata`; do not require
  or resend MEDIA/Covers. Keep correction audits outside the selected package,
  because the native app recursively queues every file beneath that folder.
- **SoundMouse delivery runs both phases end to end.**
  `soundmouse_web_delivery.py` opens every exact uploaded Metadata workbook in
  UPPM/Music, preserves its saved territories and mappings, requires zero
  blocking errors, signs it off, and rejects a newly generated spreadsheet-
  error report. Recommended-metadata warnings are recorded but do not block.
  Manifest-bound per-workbook checkpoints allow safe resume without processing
  a completed sheet twice. Only this website receipt advances SoundMouse from
  `uploaded` to `delivered`.
- **Agent requests are atomic JSON.** HDF2 sends control JSON over SSH into
  HDF1's local `pending/`; HDF1 claims by rename, updates heartbeats/status,
  and archives the request. SSH never owns or drives the GUI. Keep
  GUI imports lazy so the queue/client remains testable headless. The installer
  deploys a runtime copy under HDF1's `~/Library/Application Support` because
  macOS denies a background LaunchAgent direct access to code in `Documents`;
  re-run `--install` after syncing code changes. The LaunchAgent dispatches the
  actual GUI command into the logged-in Terminal so Screen Recording and
  Accessibility use Terminal's existing TCC grants; it tails the job log and
  alone marks queue status complete/failed. The Terminal wrapper holds a
  `caffeinate` assertion for the job lifetime, and `soundminer.py` independently
  checks for a locked console or missing Soundminer window. Do not remove those
  guards: a locked Mac yields wallpaper-only captures that can otherwise look
  like progress when a dynamic wallpaper is enabled.
  The `sourceaudio_us_only` option is reserved for an isolated demo/recovery
  request that also pins a separate `specials_dir_override`; normal Step 11
  must continue to process both US and Ex-US pairs. Override requests use the
  override folder name for checkpoints and request de-duplication so they can
  never attach to or reset the canonical release's SourceAudio job.
- **Authentication is per macOS user and never repository data.** Domo cookies
  live only in the user's private Playwright profile; UniSync owns its own
  login/Keychain and local XML. Code may change only UniSync territory/cache/
  client fields and must never print its login identity. Auth directories are
  `0700`, files/screenshots are `0600`; `security_scan.py` and the pre-commit
  hook reject identities, auth databases, and literal secrets. Keep
  `AUTHENTICATION.md` synchronized with auth behavior.
  Domo API clients use a separate workflow-owned Keychain ID/secret pair.
  Enrollment validates the pair through HTTP Basic authentication against the
  OAuth token endpoint before storage, requests only the minimal `data` scope,
  verifies both stored values exactly, and restores the prior pair after any
  partial write. API credentials and short-lived tokens must never enter argv,
  URLs, environment variables, logs, reports, screenshots, or repository files.
- **SoundMouse web processing has separate unattended credentials.** The
  native Soundmouse Uploader continues to own its remembered app session. The
  SoundMouse website pair lives only in workflow-owned Keychain items and
  `soundmouse_web_auth.py` uses it to recover a fresh retained web session
  before metadata processing. Require the protected UPPM Music page after
  login and fail closed on ambiguous controls, rejection, CAPTCHA, or MFA.
  Never log, screenshot, persist, or pass either credential through argv or
  environment variables.
- **BMAT is date-scoped and ledger-gated.** Step 17 uses the resolved workflow
  range for the custom-releases Domo card and keeps the submission card as a
  full inventory. Accepted and ingestion-pending catalogues in the local
  Pegasus ledger are excluded. Retryable prepared/failed batches are resumed,
  not duplicated. DAMS automation may only navigate and download from album
  Audio pages. Exact manifests are validated before SFTP; remote sizes are
  checked and `delivery.complete` is always the final upload. The first-seen
  host key is persisted privately and later changes fail closed.
- **SynchTank S3 retains batch-scoped deliveries.** Upload each complete final
  SynchTank package beneath a prefix exactly matching its package-folder name.
  Historical prefixes at the bucket root are expected and must never be
  removed or treated as part of the active manifest. Reject unexpected objects
  inside the active prefix, verify every expected key by exact byte size, and
  upload the empty lower-case `delivery.complete` object inside that prefix
  only as the final mutation. AWS access-key values live only in the current
  user's Keychain and memory; never place either value in repository data,
  argv, environment variables, logs, or reports.
- **TuneSat SFTP retains complete package folders.** Upload each final package
  beneath `/AudioFiles/<exact package-folder name>/`, preserving its `Music/`
  and `Metadata/` directories. Historical package folders alongside it are
  expected and must remain untouched. Do not add a completion marker. Pin the
  first-seen host key, reject later changes and unexpected files inside the
  active package folder, use `.part` temporary siblings plus atomic renames,
  and verify exact remote byte sizes before marking the partner delivered.
  TuneSat credentials live only in the current user's Keychain and memory.
  Routine authentication and connection requests from the configured
  SynchTank and TuneSat endpoints are pre-authorized for an otherwise
  authorized delivery. This does not authorize accepting a changed pinned
  host key, a changed destination, broader permissions, or unrelated system
  requests; those remain fail-closed boundaries.
- **Post-packaging delivery is exact-batch authorized.** The unified runner is
  non-mutating unless live execution is explicitly selected and the supplied
  confirmation equals `ctx.release_id`. It skips partners already marked
  delivered, preserves content-addressed restart checkpoints, and does not
  treat a prepared Outlook connector handoff as a sent message. Once the user
  has approved a partner endpoint, that approval is standing authorization for
  routine future deliveries to the same configured endpoint as soon as the
  exact package passes every readiness and correctness gate; do not pause for a
  redundant approval. Complete the delivery, verify the endpoint result, record
  the receipt and delivery state, and synchronize Monday. This standing
  authorization does not cover an unapproved or manual-only endpoint, a changed
  destination or pinned host key, an uncertain prior submission, or bypassing a
  failed gate. A fully verified upload is delivered without a separate partner
  acknowledgement; Qwire/Scripps
  require an exact Sent Items match, and SoundExchange requires both registrants
  in Upload History before those endpoints become delivered. A send/submit intent whose
  result cannot yet be verified is uncertain state and must never be retried as
  a fresh submission.
- **Netmix transport is API-first.** `netmix_portal_delivery.py` is the guarded
  interim browser route and eventual fallback. A configured API gateway always
  runs first; browser fallback is permitted only after an explicit safe,
  pre-mutation API result. The portal uploads the complete canonical master
  folder and may mark Netmix delivered only after exact metadata/audio parity,
  one cover per album, and exact accepted filenames in View Uploads. Retained
  browser authentication is private per-user state and never repository data.
- **The Outlook Email app cannot send.** It creates and reads drafts and Sent
  Items, so Qwire/Scripps use a repository skill plus
  `outlook_connector_bridge.py`. Require one attachment below 3 MiB, verify the
  connector-created draft, record an exact-release send intent, use native
  Outlook only for that one authorized send, and verify the exact Sent Items
  result through the connector before writing the receipt. Never equate a
  prepared handoff or draft with delivery, and never resend an uncertain send.
  Outlook/Graph may add trailing spaces to every stored plain-text line and its
  attachment `size_bytes` includes provider overhead. Normalize CRLF and strip
  line-ending spaces for body comparison; do not collapse other whitespace.
  Hash the local attachment before drafting, then require one exact-name,
  non-inline file attachment and successful connector materialization rather
  than comparing Graph's reported size to the raw local byte count.
- **Monday writes are exact-batch and fail closed.** Step 18 stores each
  operator's personal API token only in that macOS user's Login Keychain. It
  collects the token once through a hidden Python TTY prompt, validates it
  before changing Keychain, stores it through Security.framework (not a
  `security` subprocess), and verifies the stored value exactly without ever
  putting the secret in argv, the environment, logs, or files. It
  validates the board/group/column schema and stable status-label IDs before
  writing. The active source-board automation keys off `Batch Master` and uses
  the compact `UPMYYYYMMDD` Batch value; `Catalog` and `Release Date` replace
  the retired Part input for newly imported rows. Compact runs—including the
  August 2026 Part 2 bridge—require exactly one item in each of Content Updates,
  Hard Drive Updates, and SoundMouse Updates under that same batch, with no Part
  suffix. For historical legacy month/part records, Part 1 requires
  exactly one Content Updates and Hard Drive Updates item for `YYYYMM` plus one
  SoundMouse item for `YYYYMM -1`; Part 2 requires only the automation-created
  SoundMouse item for `YYYYMM -2`. It never creates
  labels, re-reads both batches to verify writes, and never downgrades final
  subitem or main-item statuses. Prepared
  packages become `Ready to Deliver`; explicit delivered state becomes `Complete`.
  Every fetched subitem also has a separate `Next Action` (`status3`) value.
  Keep it action-oriented and synchronized with the planned lifecycle status;
  terminal statuses use `No Action Needed`. Do not collapse those instructions
  back into the primary Status column.
  Rolling Content Updates also require a `BMAT` subitem. Source preflight adds
  it through the Monday API because the external recipe omits it. Step 17 owns
  its full delivery: failure is `Blocked`; successful upload or the valid empty
  no-op is `Complete` with `No Action Needed`.
  NTT DATA, JMD/TSS, Qwire, and Scripps are built only by
  `monthly_delivery_workflow.py` on the 1st for the complete previous calendar
  month. Beginning October 1, 2026, its root and Monday batch are the rolling
  window containing the delivery date (for example, October 1 belongs to
  `UPM20260926`, covering September 26–October 9). Source preflight preserves
  those four subitems only in the month-owning rolling batch and removes them
  from every other rolling batch. The early phase uses `In Progress` while
  building and `Ready to Deliver` when verified. While the batch is open early,
  all ordinary rolling, SoundMouse,
  BMAT, and hard-drive work must remain `Not Started` with `Wait for Schedule`,
  and all three parent items must read `Preparing Content`; do not let Monday's
  automation defaults make unstarted work appear active or complete. The
  later full run resumes the same batch/root and leaves completed monthly
  packages untouched. Never carry
  monthly NTT audio or metadata forward from a rolling release.
  After the monthly NTT DATA and JMD/TSS MediaBoxes are verified, prepare two
  Outlook drafts only. Resolve the approved Japan delivery recipient and CC
  from private operator configuration; never store either identity here. Use
  the package's content-month label in the
  subjects (`UPM Japan JMD / TSS Data <month year> Delivery` and
  `NTT DATA <month year>`), never the following month's execution date or a
  Part suffix. Insert the link and newly generated password from the exact
  current MediaBox; never reuse, persist, or log historical credentials. The
  canonical bodies and paragraph spacing live in `DELIVERY_ENDPOINTS.md`.
  During a full run, live checkpoints advance Hard Drive after Step 10,
  Digital Fulfillment after Step 15, and the three preparation subitems inside
  SoundMouse as their Step 16 phases complete. These checkpoints must pass
  `include_history=False`; otherwise an older successful report could advance
  work that the current process has not finished. Step 18 remains the final
  reconciliation and the history-aware `--only 18` recovery path.
  An `--only 18` recovery may use the newest non-skipped gate result from private
  non-dry-run reports; a newer failure must override an older success.
  Use a dry-run before a live update and use `--monday-batch` only to resolve an
  intentional legacy `YYYYMM` or exact-date `UPMYYYYMMDD` mismatch.
  The Domo Audio Batch card (`1143680792`) is a combined Monday control
  inventory: it contains the union of albums included in UPM-US, UPM-ExUS, or
  SoundMouse and uses `Catalog` to describe that routing. It must never replace
  the separate UPM-US, UPM-ExUS, or SoundMouse tracklists used to build and
  validate the actual partner deliveries. Source-board import groups retain the
  historical display pattern with a compact date suffix, e.g.
  `UPPM Audio Batch 260801`, while every row's Batch value remains the canonical
  `UPM20260801` workflow key.
  Before starting the release workflow, refresh/export the date-filtered Domo
  Audio Batch card, load it into the `UPPM Audio Batch Releases` source board,
  and let the active `Batch Master` automation create the destination records.
  Verify through the Monday API that exactly one Content Updates, Hard Drive
  Updates, and SoundMouse Updates item exists for the compact batch and that
  each has its required subitems. Treat this as a required preflight gate, not
  a Step 18 repair. All Monday reads and authorized mutations—including source
  batch loading and repair—must use the Monday API; do not drive the Monday
  website UI when the API can perform the operation.
- **Interrupted Step 2 copies are recoverable.** `_safe_copytree` archives a
  partially copied Specials destination on `KeyboardInterrupt`. If an older
  partial tree already exists with unresolved `MMMM YYYY` names, Step 2 treats
  it as incomplete and resumes the baseline merge additively instead of
  classifying it as a completed prior release.

---

## 10. Coding conventions

- Python 3, standard library + the deps in `requirements.txt`. Prefer `pathlib`.
- Functions that do real file work take `dry_run: bool` and a `logger`, and log
  what they *would* do under dry-run. Return `bool` (or a small result object) so
  the orchestrator can record status.
- Keep destructive behavior behind explicit flags; default to safe.
- Match the surrounding style (there are extensive explanatory comments — keep
  them accurate when you change behavior).
- Update the docs in the same change (see §11).

---

## 11. Docs to keep in sync

When behavior changes, update these so they don't drift:

- `README.md` — overview, layout, machine table, dependency notes.
- `TESTING_CHECKLIST.md` — the operating + per-step testing guide (per-step
  commands, expected output, the skip-flag list, the step-flow line).
- `SETUP_GIT.md` — git setup for the two machines.
- **This `AGENTS.md`** — if you change the architecture, the validation story, or
  an invariant above.

---

## 12. Git workflow (how the human collaborates)

The human is a git novice and commits by hand from the machine where files were
saved. When proposing commits, follow these rules:

- **One command per line, no trailing `#` comments** (their shell mishandles
  inline comments on pasted lines).
- **Never `git add .` or `git add -A`** — list the exact changed files. (The repo
  has per-machine assets and local-only files that must not be swept in.)
- **Pull before you push**: `git pull --no-edit` first. If a pull reports a
  `CONFLICT`, stop and surface it rather than resolving blind.
- Both machines share the path, so a commit block is identical on either:
  ```bash
  cd "$HOME/Documents/Scripts/Python/UPM Release WorkFlow Automation/files"
  git pull --no-edit
  git add <specific files>
  git commit -m "<message>"
  git push
  ```

---

_Last updated when NBCUniversal Steps 12/14 were retired and Monday source-board loading became a required API preflight._
