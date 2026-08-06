# AGENTS.md — UPM Release Workflow

Guidance for AI coding agents (OpenAI Codex, etc.) working in this repository.
Read this before making changes. It captures how the project is structured, how
to validate edits **in a cloud sandbox that cannot run the real pipeline**, and
the hard-won invariants that are easy to break.

---

## 1. What this project is

A modular Python automation for Universal Production Music's **twice-monthly
release workflow** on macOS. One orchestrator (`upm_release_workflow.py`) runs
**16 ordered steps** from Domo metadata exports through final partner packaging
and the independent SoundMouse delivery.

- **Part 1** of a month = releases dated the **1st–14th**.
- **Part 2** = the **15th–end** of the month.
- Everything is date-driven from `--year/--month/--part` (or `--previous-month`);
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

Specifically, these **cannot run in the sandbox** and must not be used as a
validation signal:

- **GUI automation** — Steps 5 and 16 (UniSync) and Steps 11–12 (Soundminer) drive
  desktop apps via `pyautogui` + `opencv` screen-matching. They need a real macOS
  GUI session, the apps installed, and per-machine reference screenshots. None
  exist here.
- **Mounted storage** — every real input/output path is on two Thunderbolt
  volumes, `/Volumes/Pegasus32 R8 - 1` (Specials) and `/Volumes/Pegasus32 R8 - 2`
  (Hard Drive Updates). They are not present in the sandbox.
- **Domo exports** — Steps 1 and 16 drive an authenticated Domo browser session via
  Playwright. Needs credentials and interactive login.
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

## 5. The 16 steps (and where things live)

| Step | Name | Module | Runs in sandbox? |
|------|------|--------|------------------|
| 1 | Domo metadata exports | `domo_exports.py` | No (browser/auth) |
| 2 & 3 | Folder setup (Specials + HD Updates) | `folder_setup.py` | No (volumes) |
| 4 | Album list DOCX + PDF | `album_list_doc.py` | Partial (PDF needs LibreOffice) |
| 5 | UniSync music export | `unisync_automation.py` | No (GUI) |
| 6–8 | Album covers (download → flatten → into WAV w COVERS) | `covers.py` | Logic testable |
| 9 | Verification (**gate** for 10–15) | `verification.py`, `remediation.py` | Logic testable |
| 10 | Final packaging **+ SoundExchange ingest forms** | `final_packaging.py`, `split_se_ingest_forms.py` | Logic testable |
| 11 | SourceAudio AIFF mirror | `soundminer.py` | No (GUI) |
| 12 | Soundminer NBC (12.7 = NBC WAV→MP3) | `soundminer.py`, `audio_conversion.py` | Convert logic testable; mirror = GUI |
| 13 | Non-main-track cleanup | `cleanup.py` | Logic testable |
| 14 | Rename NBC files | `cleanup.py` | Logic testable |
| 15 | Final metadata cross-check | `final_metadata_verification.py` | Logic testable |
| 16 | SoundMouse delivery | `soundmouse.py` | Data/filesystem logic testable; Domo + UniSync cannot run |

Steps **10–15 are gated behind Step 9**: if verification fails on a real run
(`finalize_blocked = verify_failed and not dry_run`), they're skipped. When Step 9
is skipped entirely (e.g. `--start-at 13`), `verify_failed` defaults to `False`,
so the finalization steps are **not** blocked.

Step **16 is independent of the Step 9 gate**. It exports the SoundMouse
tracklist and bucket, builds one directory from the resolved workflow start and
end dates, runs additive US/Rest-of-World/Japan WAV UniSync jobs into `MEDIA`,
downloads flat covers, and exports only metadata workbooks selected by bucket
codes 01–10. Those metadata exports remain XLSX,
but Step 16 removes their Domo workbook formatting after download while keeping
cell values, formulas, worksheet names, and workbook structure. Raw Domo
`ActivationRange` values never control or split the delivery directory. Its final gate
unions the audio and cover filenames across every selected metadata workbook,
checks them against `MEDIA` and `Covers`, writes an auditable missing-items CSV,
and fails Step 16 when a referenced file is absent.

SoundMouse territory routing uses the canonical US tracklist as its partition:
matching rows go to United States, remaining rows go to Rest of World, and Japan
is a final fallback. Do not revert to sending the complete CSV to every
territory; that creates false not-found warnings and multi-minute stalls.

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
`soundminer_agent.py` (HDF1 Aqua LaunchAgent + shared request/status protocol),
`workflow_report.py` (structured JSON run report),
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
  Tokens come from `_STEP_UNITS` (e.g. `1,2,4,5,6,9,10,11,12,12.7,13,14,15,16`).
- **Per-step skips**: `--skip-domo`, `--skip-folder-setup`, `--skip-album-list-doc`,
  `--skip-unisync`, `--skip-covers`, `--skip-verify`, `--skip-final-packaging`,
  `--skip-soundexchange`, `--skip-sourceaudio`, `--skip-soundminer`,
  `--skip-nbc-mirror`, `--skip-non-maintrack-cleanup`, `--skip-rename`,
  `--skip-final-metadata-check`, `--skip-soundmouse`.
- **Step 13 deletes by default on real runs**: non-main-track cleanup passes
  `actually_delete = not args.dry_run`, so `--dry-run` is the preview/safety
  guard. `--delete-non-maintracks` is deprecated and ignored; it remains only
  so older commands do not error. Folder overwrite still requires `--overwrite`.
- Soundminer runs **unattended by default**. From HDF2, the HDF1 login-session
  agent must be healthy before preflight passes; never replace it with an
  SSH-spawned GUI process (macOS TCC blocks that capture context).
  `--soundminer-attended` re-adds
  optional supervision pauses. Because Soundminer persists one global Mirror
  Settings state, Step 11 explicitly applies the SourceAudio AIFF profile and
  Step 12 explicitly applies the NBC Broadcast Wave profile before every
  mirror; neither flow may trust the previously persisted state.

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
inline or are submitted to HDF1's shared-volume agent.

---

## 8. Not in the repo (external / per-machine)

- **The Pegasus volumes** (`/Volumes/Pegasus32 R8 - 1` and `- 2`) — all real
  release data. Paths are defined in `config.py`; the volumes themselves are not
  in git and not in the sandbox.
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
- **Every `--skip-*` flag** must appear in `_ALL_SKIP_ATTRS` (enforced by
  `smoke_test.py`). Sub-phase flags without their own step token (like
  `--skip-soundexchange`) may need special handling in `_apply_step_selectors`
  (e.g. `--only 10` explicitly un-skips SoundExchange).
- **Column detection** goes through `tracklist_columns.py`. Don't hardcode column
  names in individual modules.
- **Soundminer must fail closed.** Never restore count-only mirror success,
  generic OK/Yes clicking, or a no-activity timeout that proceeds anyway.
  Metadata/source and destination filename manifests are correctness gates.
- **Agent requests are atomic JSON.** HDF2 writes only to `pending/`; HDF1
  claims by rename, updates heartbeats/status, and archives the request. Keep
  GUI imports lazy so the queue/client remains testable headless. The installer
  deploys a runtime copy under HDF1's `~/Library/Application Support` because
  macOS denies a background LaunchAgent direct access to code in `Documents`;
  re-run `--install` after syncing code changes.
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
  cd "/Users/hdfuser/Documents/Scripts/Python/UPM Release WorkFlow Automation/files"
  git pull --no-edit
  git add <specific files>
  git commit -m "<message>"
  git push
  ```

---

_Last updated when SoundMouse was added as independent Step 16._
