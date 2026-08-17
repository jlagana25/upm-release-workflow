# UPM Release Workflow

Automation for the twice-monthly Universal Production Music release process on
macOS — Domo exports through final packaging, in 16 ordered steps driven by a
single orchestrator (`upm_release_workflow.py`).

## Machines

| Host | Role | Project path |
|------|------|--------------|
| **USMPSMDHDF2** | Pipeline machine (run the orchestrator here) | `~/Documents/Scripts/Python/UPM Release WorkFlow Automation/files` |
| **USMPSMDHDF1** | Soundminer machine (Steps 11 & 12 run here) | `~/Documents/Scripts/Python/UPM Release WorkFlow Automation/files` |

Both machines now use the **same** project path (`~/Documents/Scripts/Python/UPM
Release WorkFlow Automation/files`), so any `cd` / git command is identical on
either one.

The full pipeline can run end-to-end from USMPSMDHDF2 without switching Macs.
An HDF1 login-session agent watches its private local JSON queue, accepts
control/status traffic from HDF2 over SSH, runs Steps 11–12 inside HDF1's real
Aqua session, and streams results back. SSH never drives the GUI. Soundminer is
never installed or launched on HDF2. Shared
storage is the two Pegasus volumes
(`/Volumes/Pegasus32 R8 - 1` and `- 2`), which live **outside** the repo.

## Setup

```bash
make install          # pip install -r requirements.txt + playwright chromium
# or, pinned to the exact known-good versions once a lock exists:
pip install -r requirements.lock
```

`ffmpeg` must be on PATH for Step 12.7 (`brew install ffmpeg`). Soundminer steps
require Accessibility + Screen Recording permissions on USMPSMDHDF1.

Each operator configures private authentication under their own macOS account:

```bash
python3 auth_manager.py --enroll-domo-keychain
python3 auth_manager.py --setup domo
python3 auth_manager.py --setup unisync
python3 auth_manager.py --status
```

This is a one-time interactive enrollment. Domo credentials are collected with
hidden prompts and stored only as workflow-owned items in the current user's
macOS Login Keychain; normal runs select the account and fill the password in
memory. They also reuse Domo's private persistent browser session and UniSync's
app/Keychain session without login prompts or Enter pauses. If UMG requires
fresh MFA, the run fails and reports the setup command instead of attempting to
bypass the challenge.
The unattended Microsoft→Domo redirect is allowed up to three minutes because
this environment can take roughly two minutes even with a valid retained session.

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
python3 upm_release_workflow.py --start-date 2026-09-01 --end-date 2026-09-14
python3 upm_release_workflow.py --previous-month                     # full prior month
python3 upm_release_workflow.py --previous-month --dry-run           # preview, no writes
python3 upm_release_workflow.py --list-steps                         # the canonical step list
python3 upm_release_workflow.py --previous-month --only 15           # run one step
python3 upm_release_workflow.py --previous-month --only 16           # SoundMouse only
python3 upm_release_workflow.py --year 2026 --month 5 --part 1 --start-at 12.7
```

Every run writes a structured JSON report under
`~/Documents/Scripts/Python/_Logs/UPM Release Workflow/reports/<release-id>/`
with step results, timing, diagnostics, artifact paths, and key output counts.
Use `--soundminer-resume` after a failed HDF1 phase; each checkpoint is trusted
only after its destination manifest is revalidated.

Internal IDs always describe the source content period. Client delivery names
follow the delivery schedule: the transition uses `Universal Production Music
August 2026 Part 1` and `Universal Production Music August 2026 Part 2`. From
September 2026 onward, pass an exact 14-day range; names contain no commas, for
example `Universal Production Music September 1–14 2026 Releases - NBC` and
`Universal Production Music September 29–October 12 2026 Releases - NBC`.
Cross-month ranges are supported. `--full-month-content` lets the transition
Part 2 include all August releases without changing its Part 2 client label.
The internal `UPM-2026-07-FULL` release is permanently mapped to the Part 1
client label, so re-running `--previous-month` during August continues to update
the existing August Part 1 partner folders instead of creating July Full paths.
NBC Domo exports also discard Domo's `GRAND TOTAL` summary footer before the
CSV is installed, preventing Soundminer from treating it as a missing filename.

MTV-Viacom is retired from the workflow. Folder setup filters any matching
legacy folder out of the shared Specials baseline, so fresh and additively
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

Step 16 exports SoundMouse metadata from Domo as CSV first and converts each
CSV to a clean shared-string XLSX package required by the SoundMouse uploader.
The conversion is unattended, preserves every CSV field as text, and does not
require or control Microsoft Excel.

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
- `auth_manager.py` — redacted per-user Domo/UniSync setup, status, permission
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
  deleting existing media.
- Catalog refreshes are delivery-state aware. A partner is `pending` unless it
  has explicitly been marked `uploaded` or `delivered` in the release-local
  `_WORKFLOW/delivery_status.json`. Re-running Steps 1, 5–8, and 10 replaces its
  metadata, retrieves newly referenced masters through the normal UniSync
  territory/cache/client route, refreshes covers, adds new media, and removes
  files no longer present in the refreshed source trees. `uploaded` is the
  correction boundary for SourceAudio US/Ex-US, Netmix, and SoundMouse because
  those systems map uploaded metadata to media before official delivery. They
  receive an audited `Missing` correction package once uploaded (and remain in
  correction mode if later marked delivered). Other uploaded partners continue
  to refresh their original folders in place; other delivered partners are
  protected from mutation. Step 15 validates SourceAudio and Netmix against the
  union of original media and the current correction package. SoundMouse applies
  the same union in its Step 16 gate. A SoundMouse `Missing` package contains
  only added WAVs, uploader-compatible metadata workbooks filtered to the added
  audio or cover rows, and only genuinely new cover files. Audio-only additions
  and filename corrections do not duplicate unchanged album artwork. Record or
  inspect state with:
  ```bash
  python3 delivery_state.py --year 2026 --month 9 --part 1 --mark-uploaded sourceaudio,sourceaudio_exus
  python3 delivery_state.py --year 2026 --month 9 --part 1 --show
  python3 delivery_state.py --year 2026 --month 9 --part 1 --mark-pending sourceaudio
  ```
  Accepted partner keys are `discovery`, `espn`, `hd_updates`, `japan_ntt`,
  `nbc`, `netmix`, `soundmouse`, `sourceaudio`, `sourceaudio_exus`, `synchtank`, and
  `tunesat`; use `all` by itself to change every key. When refreshed metadata
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
  period directory, WAVs from the US/Rest-of-World/Japan UniSync territories,
  covers, and bucket-selected metadata workbooks. The
  directory dates come from the resolved workflow period, never from raw Domo
  `ActivationRange` values. Audio rows found in the canonical US tracklist are
  requested from the US territory; remaining rows go to Rest of World, with
  Japan retained as a fallback. Metadata is exported from Domo as CSV and then
  converted into clean upload-compatible XLSX workbooks automatically.
  The step then validates every audio and cover filename referenced across the
  selected metadata workbooks and fails with a missing-items CSV if needed.
  Also runnable standalone with the normal date flags and `--dry-run`.
- `split_se_ingest_forms.py` — SoundExchange metadata → ISRC ingest-form workbooks.
  Runs **automatically as the second phase of Step 10** (final packaging) in a
  full pipeline run; also runnable standalone (`python3 split_se_ingest_forms.py
  --previous-month`, `--dry-run` supported). `--skip-soundexchange` skips just
  this phase; `--skip-final-packaging` skips the whole of Step 10.
- `smoke_test.py` — fast offline sanity check.
- `TESTING_CHECKLIST.md` — operating + per-step testing guide.
