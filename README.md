# UPM Release Workflow

Automation for the twice-monthly Universal Production Music release process on
macOS — Domo exports through final packaging, BMAT delivery, and Monday status updates, with workflow units numbered through Step 18 driven by a
single orchestrator (`upm_release_workflow.py`).
NBCUniversal is retired; historical Steps 12 and 14 are no longer active.

## Machines

| Host | Role | Project path |
|------|------|--------------|
| **USMPSMDHDF2** | Pipeline machine (run the orchestrator here) | `~/Documents/Scripts/Python/UPM Release WorkFlow Automation/files` |
| **USMPSMDHDF1** | Soundminer machine (Step 11 runs here) | `~/Documents/Scripts/Python/UPM Release WorkFlow Automation/files` |

Both machines now use the **same** project path (`~/Documents/Scripts/Python/UPM
Release WorkFlow Automation/files`), so any `cd` / git command is identical on
either one.

The full pipeline can run end-to-end from USMPSMDHDF2 without switching Macs.
An HDF1 login-session agent watches its private local JSON queue, accepts
control/status traffic from HDF2 over SSH, runs Step 11 inside HDF1's real
Aqua session, and streams results back. SSH never drives the GUI. Soundminer is
never installed or launched on HDF2. Shared
storage is the two Pegasus volumes
(`/Volumes/Pegasus32 R8 - 1` and `- 2`), which live **outside** the repo.

Workflow preflight automatically recovers those exact known mounts after a
normal restart or update. HDF2 reconnects HDF1's saved SMB shares; HDF1 mounts
only uniquely identified attached volumes through Disk Arbitration. The
optional `Documents` compatibility share is also restored on HDF2, although
the workflow itself uses stable local `~/Documents` paths. Use
`--no-auto-mount`, `UPM_DISABLE_AUTO_MOUNT=1`, or create the private
`~/.upm_release_workflow/disable_auto_mount` sentinel before a run when a
volume was intentionally unmounted. Recovery never embeds credentials and a
missing required Pegasus volume still stops the workflow.

## Setup

```bash
make install          # pip install -r requirements.txt + playwright chromium
# or, pinned to the exact known-good versions once a lock exists:
pip install -r requirements.lock
```

Soundminer requires Accessibility + Screen Recording permissions on USMPSMDHDF1.

Each operator configures private authentication under their own macOS account:

```bash
python3 auth_manager.py --enroll-domo-keychain
python3 auth_manager.py --enroll-domo-api-keychain
python3 auth_manager.py --setup domo
python3 auth_manager.py --setup dams
python3 auth_manager.py --setup unisync
python3 auth_manager.py --enroll-bmat-keychain
python3 auth_manager.py --enroll-synchtank-keychain
python3 auth_manager.py --enroll-tunesat-keychain
python3 auth_manager.py --enroll-espn-keychain
python3 auth_manager.py --enroll-soundexchange-keychain
python3 auth_manager.py --enroll-monday-keychain
python3 auth_manager.py --status
```

This is a one-time interactive enrollment. Domo SSO credentials are collected once
each with hidden prompts and stored only as workflow-owned items in the current user's
macOS Login Keychain. The independent Domo API enrollment validates a client ID
and client secret against Domo's OAuth token endpoint using the minimal `data`
scope before atomically replacing its two Keychain items; a rejected pair never
changes Keychain, and a failed multi-item write restores the previous pair. The
Monday personal API token is also stored as a workflow-owned secret; each operator enrolls their own token, whose board access
is limited by that Monday user's permissions. Enrollment validates the token
before changing Keychain, stores it through the native macOS Keychain API, and
verifies the stored value exactly. The Monday user must be an active
admin/member with a confirmed email. Normal Step 1, BMAT, and SoundMouse exports
use explicit public-Domo-API projection contracts and fail closed instead of
falling back to card downloads. The private Domo browser session is retained
only for DataFlow lineage/run recovery. UniSync reuses its app/Keychain session
without login prompts or Enter pauses. If UMG requires
fresh MFA, the run fails and reports the setup command instead of attempting to
bypass the challenge.
BMAT reuses its own private DAMS browser profile after UMG Employee SSO and
loads its SFTP pair only from the current user's Login Keychain. SynchTank
likewise loads its AWS access-key pair only from Keychain.
TuneSat loads its separate SFTP pair only from Keychain.
ESPN Media Shuttle and SoundExchange Direct load their separate portal pairs
only from Keychain when retained private browser sessions need authentication.
The unattended Microsoft→Domo redirect is allowed up to three minutes because
this environment can take roughly two minutes even with a valid retained session.
After bounded UniSync retries make zero progress, Step 5 refreshes the affected
card's Domo lineage. Known upstream catalog producers run before the card-owning
transform (Japan 4312 → 4278; SoundMouse 3691 → 4330). Every link must produce
a new successful History entry before the workflow replaces that card export
and retries only the refreshed missing manifest. Recovery is attempted once per
card and fails closed on ambiguous lineage or a failed ETL.
Each UniSync job also closes only completed Microsoft `Working...` tabs whose
URL carries UniSync's Azure client ID; unrelated browser tabs are untouched.

No credential or browser profile is stored in Git or on Pegasus. Local auth
directories are mode `0700`, files are `0600`, status/log output is redacted,
and the installed pre-commit scanner blocks identities, cookie databases,
UniSync preferences, literal secrets, and private keys. See
[`AUTHENTICATION.md`](AUTHENTICATION.md) for onboarding and recoverable reset.

Install the unattended Soundminer agent once, from HDF1's logged-in session:

```bash
cd "$HOME/Documents/Scripts/Python/UPM Release WorkFlow Automation/files"
python3 soundminer_agent.py --install
python3 soundminer_agent.py --status
```

The LaunchAgent starts automatically at login. HDF2 preflight refuses to begin
a real run if its heartbeat is missing or stale. `--no-soundminer-agent`
restores the legacy manual handoff only for recovery. Installation deploys a
small runtime copy under `~/Library/Application Support/UPM Soundminer Agent`;
this avoids macOS denying background processes access to the source repo in
`Documents`. The agent dispatches GUI commands into HDF1's logged-in Terminal
so they inherit its existing Screen Recording and Accessibility grants, then
tails their log/result unattended. Each dispatched job runs under macOS
`caffeinate` so the display stays awake for long imports. The agent also checks
the console lock state throughout the run and fails immediately—rather than
mistaking a wallpaper-only capture for progress—if HDF1 is manually locked or
a managed policy overrides the wake assertion. Re-run `--install` after pulling
workflow code updates on HDF1.

## Running

```bash
python3 upm_release_workflow.py --year 2026 --month 5 --part 1        # a normal release
python3 upm_release_workflow.py --year 2026 --month 8 --part 2 --full-month-content
python3 upm_release_workflow.py --start-date 2026-09-01 --end-date 2026-09-11
python3 upm_release_workflow.py --previous-month                     # full prior month
python3 upm_release_workflow.py --previous-month --dry-run           # preview, no writes
python3 upm_release_workflow.py --list-steps                         # the canonical step list
python3 upm_release_workflow.py --previous-month --only 15           # run one step
python3 upm_release_workflow.py --previous-month --only 16           # SoundMouse only
python3 upm_release_workflow.py --previous-month --only 17 --dry-run # preview BMAT delivery
python3 upm_release_workflow.py --previous-month --only 18 --dry-run # preview Monday changes
python3 upm_release_workflow.py --previous-month --only 10 --copy-workers 4
```

Step 10 defaults to four HDF2-local copy workers within each destination;
partner destinations are still processed sequentially to avoid flooding the
SMB-mounted Pegasus volumes. Each output is copied to a hidden temporary
sibling and atomically renamed into place, so an interrupted run cannot leave
a partial file that a restart mistakes for complete. Use `--copy-workers 1`
for the legacy serial behavior, or set another positive value when tuning a
specific HDF2/network-storage setup. This option does not delegate packaging
work to HDF1.

For an authentic SourceAudio screen-recording demo, `ai_team_demo.py` runs one
isolated 20-track US path: Domo SourceAudio metadata, WorkAudioId-based UniSync
WAV retrieval, WAV w COVERS preparation, the normal HDF1 Soundminer agent's AIFF
mirror, manifest verification, and a Finder reveal of the final SourceAudio
partner package. Codex can remain visible beside the active app to show written
step explanations. See **Authentic SourceAudio AI-team demo** in
`TESTING_CHECKLIST.md`.

Every run writes a structured JSON report under
`~/Documents/Scripts/Python/_Logs/UPM Release Workflow/reports/<release-id>/`
with step results, timing, diagnostics, artifact paths, and key output counts.
Use `--soundminer-resume` after a failed HDF1 phase; each checkpoint is trusted
only after its destination manifest is revalidated.
Fast SourceAudio scans that finish during the initial dialog-watch window are
recognized from the populated record-grid change, while a visible Soundminer
Log Window remains an immediate failure. The final filename manifest still
decides whether a mirror is complete.

Internal IDs ordinarily describe the source content period. Client delivery names
follow the delivery schedule: the transition uses `Universal Production Music
August 2026 Part 1` and `Universal Production Music August 2026 Part 2`. The
first rolling transition covers September 1–11, 2026. Every later delivery
uses an exact 14-day range. Final Packaging partner folders use the abbreviated
month names while retaining the inclusive range, for example
`Universal Production Music Sep 1–11 2026 Releases - SynchTank` and
`Universal Production Music Sep 29–Oct 12 2026 Releases - SynchTank`. The same
abbreviated range is used in partner-facing metadata filenames, album lists,
package labels, and delivery-message subjects. NTT DATA, JMD/TSS, Qwire, and
Scripps instead run through `monthly_delivery_workflow.py` on the 1st, entirely
outside the rolling cadence. The October 1 run, for example, exports exactly
September 1–30 content into `UPM-2026-09-MONTHLY`, uses Monday batch
`UPM20260901`, and labels folders, filenames, MediaBoxes, and email subjects
`September 2026`. The November 1, 2026 Qwire and Scripps deliveries use the
one-time label `October 2026 Part 2`; the Japan endpoints retain `October 2026`.
That transition uses root `UPM-2026-10-MONTHLY-P2` so it cannot collide with
the historical October-named September delivery, while its Monday batch stays
keyed to the November 1 delivery date.
NTT downloads the complete previous-calendar-month Japan audio
manifest directly; it never carries audio or metadata from a rolling release.
Every rolling run omits all four monthly packages and the Japan UniSync job.
Internal batch IDs and audit date values remain unchanged.
August Part 2 and exact-date runs use the compact start-date ID `UPMYYYYMMDD`
(`UPM20260801` for the full-month transition and `UPM20260901` for the first
rolling bridge); the inclusive end date remains stored in the release context
and reports. Cross-month ranges are supported. `--full-month-content` lets the
transition Part 2 include all August releases without changing its Part 2
client label.
Mounted-volume working directories retain the hyphenated date form, such as
`UPM-2026-08-01` and `UPM-2026-09-01`. The compact value is the workflow and
Monday/Domo batch ID, not the filesystem folder name.
The internal `UPM-2026-07-FULL` release is permanently mapped to the Part 1
client label, so re-running `--previous-month` during August continues to update
the existing August Part 1 partner folders instead of creating July Full paths.
MTV-Viacom and NBCUniversal are retired from the workflow. Folder setup filters
matching legacy folders out of the shared Specials baseline, so fresh and additively
resumed release trees do not create that delivery. Existing historical release
folders are left untouched.

A normal run **deletes** non-maintracks at Step 13; `--dry-run` is the only thing
that holds back to a preview. Steps 10–15 are gated behind Step 9 verification
(escape with `--skip-verify`). See `TESTING_CHECKLIST.md` for the per-step
operating and testing guide.

## Before you push: smoke test

```bash
make smoke      # imports every module, checks arg/step consistency + shared helpers
make security   # reject private auth artifacts or literal credentials
make verify     # security + smoke + byte-compile everything
```

The smoke test is offline (no volumes needed) and runs in seconds. Run it after
every edit — it catches broken imports, arg/step-registry drift, and other
refactor breakage before they fail mid-release.

Step 16 queries the owning SoundMouse DataSet through the enrolled Domo API
first, applies the exact date and territory projections locally, writes CSV,
and converts each CSV to a clean shared-string XLSX package. It then uses Microsoft Excel to apply
Clear Formats and perform the final native save required by the SoundMouse
uploader. The workflow verifies that every metadata value is unchanged and
fails closed if Excel was not the final writer. It opens through Excel's
standard POSIX-file command, verifies the resulting workbook name, and saves
the bound workbook object even if another workbook later becomes active. Excel needs one-time access to
the SoundMouse delivery location; normal runs are unattended afterward.
SoundMouse audio routing is derived from each row's `Territory List`.
Australia runs first for every row containing `OZ`; the workflow adds only the
other country passes needed to cover rows Australia cannot supply. If the
final required country also makes no progress, the workflow refreshes and
re-exports the SoundMouse tracklist's owning Domo ETL once, recalculates the
manifest, and fails closed if the audio is still unavailable.

## Version control & two-machine sync

This project is tracked in git so changes are reviewable and a bad edit is one
`git revert` away — and so the two Macs stay in sync via `git pull` instead of
copying files by hand.

```bash
# one-time, on the canonical machine:
git init
git add <specific files>
git commit -m "Initial commit: UPM release workflow"
git branch -M main
git remote add origin git@github.com:<org-or-user>/upm-release-workflow.git
git push -u origin main

# the other machine:
git clone git@github.com:<org-or-user>/upm-release-workflow.git
```

Day-to-day:

```bash
git pull              # get the latest before a run
# …edit…
make smoke            # verify
git add <specific files>
git commit -m "Describe the change"
git push
```

## Pinning dependencies

`requirements.txt` is the install floor (`>=`). To make both machines run the
**identical** library set, generate a lock from the known-good environment and
commit it:

```bash
make lock             # = pip freeze > requirements.lock
git add requirements.lock
git commit -m "Pin dependency versions"
```

Then the other machine installs with `pip install -r requirements.lock`.

## Layout

- `upm_release_workflow.py` — orchestrator + CLI (canonical step registry is `_STEP_UNITS`).
- `config.py` — release context, all paths, partner destinations.
- `tracklist_columns.py` — shared CSV/XLSX column-name detection (one source for every module).
- `soundminer_agent.py` — HDF1 Aqua LaunchAgent plus the SSH JSON/status control
  channel used by HDF2 (SSH never drives the GUI).
- `workflow_report.py` — structured end-of-run JSON reports.
- `auth_manager.py` — redacted per-user Domo/DAMS SSO, BMAT SFTP, SynchTank S3,
  UniSync, and Monday setup, status, permission
  repair, and recoverable reset.
- `security_scan.py` — worktree/index/history credential guard used by the
  pre-commit hook and `make verify`.
- Step 1 replaces every Domo-managed delivery metadata template with the
  current export. This includes dedicated SourceAudio US and SourceAudio Ex-US
  cards; a failed export blocks final packaging instead of shipping baseline
  metadata. When either SourceAudio card is exported again after AIFF media
  already exists, `sourceaudio_delta.py` compares the refreshed metadata by
  External Id. Additions and filename revisions are prepared as AIF files in a
  sibling `Missing` folder, removals and superseded filenames are removed from
  the local `Music` folder only after the replacement package is complete, and
  an audit CSV lists the SourceAudio service entries that still require manual
  removal. If an added master is not already staged, the refresh automatically
  reuses the canonical initial-download route: US uses territory `United
  States`, the US WAV cache, and `Music/WAV`; Ex-US uses `Rest of World` and
  `Music/Ex-US (WAV)`. The downloaded WAVs are then propagated into the normal
  SourceAudio staging tree. New US album covers derive their `.webp` download
  URL from the current tracklist's `CDNAlbumArt` structure while retaining the
  metadata cover filename locally. Missing source masters fail closed without
  deleting existing media. Before either API export replaces its local CSV,
  `sourceaudio_keyword_audit.py` compares Keywords by External Id and preserves
  immutable revisions under `_WORKFLOW/sourceaudio_keyword_revisions/us` or
  `exus`. These keyword-only candidates do not require audio conversion or
  resend. Description changes are ignored. Repeated exports retain outstanding
  evidence; a local export is not proof of remote metadata, and missing prior
  exports are explicitly recorded as unverified baselines. Resolve the remote
  SourceAudio ID and compare current Keywords before applying a sparse update,
  then read back the result. Keep correction receipts separate from ordinary
  release delivery status. A revision remains outstanding until its sibling
  `<revision>.receipt.json` has `status: remote_verified`, the exact
  `revision_sha256`, and all changed `verified_external_ids` (every current ID
  for an unverified baseline). Only create that receipt after remote readback.
  Blank replacements require explicit clear review.
- Catalog refreshes are delivery-state aware. A partner is `pending` unless it
  has explicitly been marked `uploaded` or `delivered` in the release-local
  `_WORKFLOW/delivery_status.json`. Re-running Steps 1, 5–8, and 10 replaces its
  metadata, retrieves newly referenced masters through the normal UniSync
  territory/cache/client route, refreshes covers, adds new media, and removes
  files no longer present in the refreshed source trees. New verified uploads
  are marked `delivered`; the older `uploaded` state remains accepted. Both are
  correction boundaries for SourceAudio US/Ex-US, Netmix, and SoundMouse because
  those systems map uploaded metadata to media. They receive an audited `Missing`
  correction package after that boundary. Other delivered partners are protected
  from mutation. Step 15 validates SourceAudio and Netmix against the
  union of original media and the current correction package. SoundMouse applies
  the same union in its Step 16 gate. A SoundMouse `Missing` package contains
  only added WAVs, uploader-compatible metadata workbooks filtered to the added
  audio or cover rows, and only genuinely new cover files. Audio-only additions
  and filename corrections do not duplicate unchanged album artwork. A
  metadata-only correction uploads only its corrected workbook(s), without
  resending MEDIA or Covers. Correction audits remain under `_WORKFLOW` and
  outside the native uploader's recursively selected package. Record or
  inspect state with:
  ```bash
  python3 delivery_state.py --year 2026 --month 9 --part 1 --mark-uploaded sourceaudio,sourceaudio_exus
  python3 delivery_state.py --year 2026 --month 9 --part 1 --show
  python3 delivery_state.py --year 2026 --month 9 --part 1 --mark-pending sourceaudio
  python3 delivery_state.py --delivery-date 2026-10-01 --show
  ```
  Accepted partner keys are `discovery`, `espn`, `hd_updates`, `japan_jmdtss`,
  `japan_ntt`, `netmix`, `qwire`, `scripps`, `soundexchange`, `soundmouse`,
  `sourceaudio`, `sourceaudio_exus`, `synchtank`, and `tunesat`; use `all` by
  itself to change every key. When refreshed metadata
  removes catalog items, run with `--prune-music --prune-mode archive` before
  final packaging so the canonical `1-ORIGINAL/Music` trees are reconciled
  recoverably as well. Standalone Step 13 also accepts
  `--archive-extras <directory>` to remove Tunesat non-keepers from its Music
  folder without permanently deleting them.
- Step modules: `domo_exports`, `folder_setup`, `album_list_doc`, `unisync_automation`,
  `covers`, `verification`, `final_packaging`, `soundminer`, `audio_conversion`,
  `cleanup`, `final_metadata_verification`, `remediation`, `prune`,
  `sourceaudio_delta`, `delivery_state`.
- `soundmouse.py` — Step 16: SoundMouse tracklist/bucket exports, release
  period directory, WAVs from the country passes selected by `Territory List`,
  covers, and bucket-selected metadata workbooks. The
  directory dates come from the resolved workflow period, never from raw Domo
  `ActivationRange` values. Australia is prioritized for `OZ` rows, then only
  the additional countries required by uncovered rows are tried. Metadata is
  exported from Domo as CSV and then
  converted into clean upload-compatible XLSX workbooks automatically.
  The step then validates every audio and cover filename referenced across the
  selected metadata workbooks and fails with a missing-items CSV if needed.
  Also runnable standalone with the normal date flags and `--dry-run`.
- `soundmouse_uploader_delivery.py` — standalone post-Step-16 transfer through
  the installed Soundmouse Uploader. It requires the retained app login,
  explicitly selects and re-verifies workspace `UPPM` and module `Music`, adds
  the complete release directory, and validates every newly created queue row
  against the exact local manifest before accepting completed status. This is
  only the transport phase and marks SoundMouse `uploaded`; website processing
  of every metadata workbook with zero errors is still required before
  `delivered`.
- `soundmouse_web_delivery.py` — the second SoundMouse phase. It resumes the
  retained UPPM website session, processes the exact uploaded workbook
  manifest with saved territory/mapping choices, accepts only non-blocking
  recommended-metadata warnings, verifies that no new spreadsheet-error report
  appeared, writes the website receipt, and marks the endpoint delivered.
- `bmat_delivery.py` — Step 17: exports the date-filtered custom-release list
  and full BMAT submission inventory, excludes accepted or ingestion-pending
  catalogues using the Pegasus-local delivery ledger, resumes retryable batches,
  downloads each selected album's exact audio manifest from its DAMS Audio page,
  validates the package, uploads with pinned host-key behavior, and creates
  `delivery.complete` only after every remote file passes size verification.
- `synchtank_delivery.py` — standalone post-packaging delivery: uploads the
  verified SynchTank package beneath its exact package-name prefix in the
  configured S3 bucket, ignores retained historical prefixes, resumes only
  exact size-matched partial uploads inside the active prefix, verifies that
  complete manifest, and writes `delivery.complete` inside the prefix last.
- `tunesat_delivery.py` — standalone post-packaging delivery: uploads the
  complete TuneSat package beneath its exact package-name folder in
  `/AudioFiles`, preserving historical package folders, with first-seen
  host-key pinning, temporary-sibling uploads, exact active-folder size
  verification, and safe resume.
- `netmix_portal_delivery.py` — API-priority Netmix delivery adapter. Until CND
  API access is available, its guarded browser fallback uploads the complete
  master folder, verifies exact metadata/audio parity before mutation, and
  requires every expected audio filename to reach an accepted terminal state
  in View Uploads before writing a receipt or advancing delivery state.
- `post_packaging_delivery.py` — unified post-packaging runner. It supports
  endpoint selection, non-mutating planning, safe resume through
  manifest-bound checkpoints, duplicate-send prevention, and exact-release
  authorization for live execution. Use `--delivery-date YYYY-MM-01` to reopen
  the exact standalone monthly NTT/JMD-TSS/Qwire/Scripps batch. It dispatches the standalone upload,
  browser, and Outlook-connector endpoints. A fully verified upload is recorded
  as `delivered`; SoundMouse uses `uploaded` as the active boundary between its
  native transfer and website metadata processing. A single SoundMouse run now
  performs both phases, while a resume from `uploaded` starts directly at the
  website phase. For other endpoints it remains a legacy/correction-routing
  state.
- `espn_delivery.py` — submits the complete ESPN directory as one Media Shuttle
  folder, resumes only an interrupted matching transfer, and requires both an
  `Uploaded 1 file(s)` history result and the exact destination folder before
  writing its receipt. `--interactive-native-selection` keeps the guarded
  browser run alive while the exact folder is selected in Signiant App.
- `soundexchange_delivery.py` — handles the MGB then Z Tunes registrants,
  uploads every contiguous workbook part, audits invalid entries without
  submitting them, checks exact ISRC/count parity, clicks Submit Recordings
  only after validation, and verifies the generated Upload History CSV.
  `--interactive-login` keeps a fresh sign-in and both submissions in one
  browser session.
- `email_deliveries.py` — prepares Qwire and Scripps Outlook attachments,
  including deterministic Qwire splitting and verified single-file ZIPs under
  the connector limit. It verifies the complete draft and Sent Items message
  through an Outlook gateway before marking either endpoint delivered.
- `outlook_connector_bridge.py` plus
  `skills/outlook-delivery-bridge/SKILL.md` — connects the local delivery
  contract to Codex's Outlook Email app. The connector creates and verifies the
  exact one-attachment draft; because that app has no Send action, the bridge
  records a separate authorized native-Outlook send intent and advances state
  only after the connector verifies the message in Sent Items.
- `monday_sync.py` — before workflow steps, exact-date runs export Domo Audio
  Batch card `1143680792`, load/repair the compact batch in the
  `UPPM Audio Batch Releases` source board through the Monday API, write
  `Batch Master` last, and wait until the automation has created exactly one
  Content, Hard Drive, and SoundMouse destination item with required subitems.
  Step 18 then preserves the source-board automation for legacy
  month/part runs: Part 1 resolves
  Content Updates and Hard Drive Updates by exact `YYYYMM` plus SoundMouse by
  `YYYYMM -1`; Part 2 resolves only its automation-created SoundMouse
  `YYYYMM -2` row. Exact-date runs instead resolve Content, Hard Drive, and
  SoundMouse under one compact `UPMYYYYMMDD` batch. It validates
  the expected groups, columns, and stable status-label IDs, then advances only
  matching subitems. Prepared packages become `Clear to Send`; verified local
  uploaded or delivered states become `Complete`. Rolling rows keep NTT DATA,
  JMD/TSS, Qwire, and Scripps out of rolling Content Updates items. The source
  automation may initially create those template subitems, but source preflight
  removes them through the Monday API before validating the rolling item. The
  standalone monthly item uses `Working On It` while building and `Clear to Send` when verified;
  genuinely retired endpoints become `Not Needed`.
  Main items are derived from their subitems.
  It fails closed on missing or duplicate rows and never creates
  labels, and re-reads both batches to verify every write. Full runs also issue
  live-progress checkpoints: Hard Drive after Step 10, Digital Fulfillment
  after Step 15, and individual SoundMouse media, cover, and metadata subitems
  as those Step 16 phases finish. Checkpoints use only results observed by the
  active process, never an older report. A recovery `--only 18` run reads the most recent non-skipped gate
  results from private non-dry-run workflow reports, so a current failure wins
  over an older success. Use `--monday-batch YYYYMM` for a legacy override or
  `--monday-batch UPMYYYYMMDD` for an exact-date override, and `--skip-monday`
  to omit this step.
- `split_se_ingest_forms.py` — SoundExchange metadata → ISRC ingest-form workbooks.
  Runs **automatically as the second phase of Step 10** (final packaging) in a
  full pipeline run; also runnable standalone (`python3 split_se_ingest_forms.py
  --previous-month`, `--dry-run` supported). `--skip-soundexchange` skips just
  this phase; `--skip-final-packaging` skips the whole of Step 10.
- `smoke_test.py` — fast offline sanity check.
- `TESTING_CHECKLIST.md` — operating + per-step testing guide.
