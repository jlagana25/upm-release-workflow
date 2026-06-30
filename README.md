# UPM Release Workflow

Automation for the twice-monthly Universal Production Music release process on
macOS — Domo exports through final packaging, in 15 ordered steps driven by a
single orchestrator (`upm_release_workflow.py`).

## Machines

| Host | Role | Project path |
|------|------|--------------|
| **USMPSMDHDF2** | Pipeline machine (run the orchestrator here) | `~/Documents/Python/UPM Release WorkFlow Automation/files` |
| **USMPSMDHDF1** | Soundminer machine (Steps 11 & 12 run here) | `~/Documents/Scripts/Python/UPM Release WorkFlow Automation/files` |

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
python3 upm_release_workflow.py --year 2026 --month 5 --part 1 --start-at 12.7
```

A normal run **deletes** non-maintracks at Step 13; `--dry-run` is the only thing
that holds back to a preview. Steps 10–15 are gated behind Step 9 verification
(escape with `--skip-verify`). See `TESTING_CHECKLIST.md` for the per-step
operating and testing guide.

## Before you push: smoke test

```bash
make smoke      # imports every module, checks arg/step consistency + shared helpers
make verify     # smoke + byte-compile everything
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
- Step modules: `domo_exports`, `folder_setup`, `album_list_doc`, `unisync_automation`,
  `covers`, `verification`, `final_packaging`, `soundminer`, `audio_conversion`,
  `cleanup`, `final_metadata_verification`, `remediation`, `prune`.
- `split_se_ingest_forms.py` — SoundExchange metadata → ISRC ingest-form workbooks.
- `smoke_test.py` — fast offline sanity check.
- `TESTING_CHECKLIST.md` — operating + per-step testing guide.
