# UPM Release Workflow

Automation for the twice-monthly Universal Production Music release process on
macOS — Domo exports through final packaging, driven by a single orchestrator
(`upm_release_workflow.py`). Historical step numbers remain CLI-compatible,
while runtime phases follow explicit artifact dependencies.

## Machines

| Host | Role | Project path |
|------|------|--------------|
| **USMPSMDHDF2** | Pipeline machine (run the orchestrator here) | `~/Documents/Scripts/Python/UPM Release WorkFlow Automation/files` |
| **USMPSMDHDF1** | Soundminer machine (Steps 11 & 12 run here) | `~/Documents/Scripts/Python/UPM Release WorkFlow Automation/files` |

Both machines now use the **same** project path (`~/Documents/Scripts/Python/UPM
Release WorkFlow Automation/files`), so any `cd` / git command is identical on
either one.

The full pipeline can run end-to-end on either; on USMPSMDHDF2 it pauses to hand
off the Soundminer steps. Shared storage is the two Pegasus volumes
(`/Volumes/Pegasus32 R8 - 1` and `- 2`), which live **outside** the repo.

## Setup

```bash
make install          # pip install -r requirements.txt + playwright chromium
# or, pinned to the exact known-good versions once a lock exists:
pip install -r requirements.lock
```

`ffmpeg` must be on PATH for Step 12.7 (`brew install ffmpeg`). Soundminer steps
require Accessibility + Screen Recording permissions on USMPSMDHDF1.

## Running

```bash
python3 upm_release_workflow.py --year 2026 --month 5 --part 1        # a normal release
python3 upm_release_workflow.py --previous-month                     # full prior month
python3 upm_release_workflow.py --previous-month --dry-run           # preview, no writes
python3 upm_release_workflow.py --list-steps                         # the canonical step list
python3 upm_release_workflow.py --previous-month --only 15           # run one step
python3 upm_release_workflow.py --previous-month --only 16           # SoundMouse only
python3 upm_release_workflow.py --year 2026 --month 5 --part 1 --start-at 12.7
```

Step 10 materializes Tunesat directly from its metadata keep-list; Step 13 is a
repair-only compatibility alias. Steps 10–15 are gated behind Step 9 verification
(escape with `--skip-verify`). See `TESTING_CHECKLIST.md` for the per-step
operating and testing guide.

Dry-run is write-free, including console-only logging and no audit-report files.
On real runs, missing Step 10 source trees and missing Step 15 required partner
media directories are failures rather than successful skips.

## Before you push: smoke test

```bash
make smoke      # imports every module, checks arg/step consistency + shared helpers
make test       # offline synthetic-filesystem regression tests
make verify     # smoke + unit tests + byte-compile everything
```

The smoke test is offline (no volumes needed) and runs in seconds. Run it after
every edit — it catches broken imports, arg/step-registry drift, and other
refactor breakage before they fail mid-release.

## Version control & two-machine sync

This project is tracked in git so changes are reviewable and a bad edit is one
`git revert` away — and so the two Macs stay in sync via `git pull` instead of
copying files by hand.

```bash
# one-time, on the canonical machine:
git init
git add -A
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
git add -A && git commit -m "Describe the change" && git push
```

## Pinning dependencies

`requirements.txt` is the install floor (`>=`). To make both machines run the
**identical** library set, generate a lock from the known-good environment and
commit it:

```bash
make lock             # = pip freeze > requirements.lock
git add requirements.lock && git commit -m "Pin dependency versions"
```

Then the other machine installs with `pip install -r requirements.lock`.

## Layout

- `upm_release_workflow.py` — orchestrator + CLI (canonical step registry is `_STEP_UNITS`).
- `config.py` — release context, all paths, partner destinations.
- `tracklist_columns.py` — shared CSV/XLSX column-name detection (one source for every module).
- `filesystem_names.py` — shared whitespace/case normalization for real label folders.
- `release_manifest.py` — process-local, automatically invalidated export-table cache.
- `cover_downloads.py` — shared atomic image validation and master-cache reuse.
- Step modules: `domo_exports`, `folder_setup`, `album_list_doc`, `unisync_automation`,
  `covers`, `verification`, `final_packaging`, `soundminer`, `audio_conversion`,
  `cleanup`, `final_metadata_verification`, `remediation`, `prune`.
- `soundmouse.py` — Step 16: SoundMouse tracklist/bucket exports, release
  directories, WAVs, covers, and bucket-selected metadata workbooks. Metadata
  remains XLSX but all downloaded workbook formatting is removed automatically.
  The step then validates every audio and cover filename referenced across the
  selected metadata workbooks and fails with a missing-items CSV if needed.
  A full run folds these exports into Step 1's authenticated Domo session and
  its WAV request into Step 5's UniSync batch; standalone Step 16 retains the
  same independent outcome and uses one Domo session. After selected exports
  succeed, metadata workbooks from buckets no longer
  selected for the run are removed using the exact generated-name pattern.
  Also runnable standalone with the normal date flags and `--dry-run`.
- `split_se_ingest_forms.py` — SoundExchange metadata → ISRC ingest-form workbooks.
  Runs **automatically as the second phase of Step 10** (final packaging) in a
  full pipeline run; also runnable standalone (`python3 split_se_ingest_forms.py
  --previous-month`, `--dry-run` supported). `--skip-soundexchange` skips just
  this phase; `--skip-final-packaging` skips the whole of Step 10.
  Reruns atomically replace current parts and remove only obsolete generated
  `Part N` files for the same entity.
- `smoke_test.py` — fast offline sanity check.
- `test_release_safety.py`, `test_soundmouse.py` — offline regression tests.
- `TESTING_CHECKLIST.md` — operating + per-step testing guide.
