# Git Setup & Daily Workflow

Version control and two-machine sync for the UPM Release Workflow. This replaces
the old `tar | ssh | wc -c` file-copy dance — code moves between the machines
with `git push` / `git pull`.

- **Repo:** https://github.com/jlagana25/upm-release-workflow (private)
- **Account:** `jlagana25`

| Machine | Role | Project path |
|---------|------|--------------|
| **USMPSMDHDF2** | Pipeline machine | `~/Documents/Python/UPM Release WorkFlow Automation/files` |
| **USMPSMDHDF1** | Soundminer machine | `~/Documents/Scripts/Python/UPM Release WorkFlow Automation/files` |

Only the code under `files/` is tracked. The Pegasus volumes, `_Exports`, logs,
and `__pycache__` are **not** in the repo (see `.gitignore`).

---

## One-time setup on a machine

Needed once per Mac (already done on USMPSMDHDF2).

```bash
# tools
git --version          # if missing: xcode-select --install
gh --version           # if missing: brew install gh

# authenticate GitHub (browser login: GitHub.com -> HTTPS -> web browser)
gh auth status || gh auth login

# commit identity
git config --global user.name  "jlagana25"
git config --global user.email "18154835+jlagana25@users.noreply.github.com"
```

---

## Second machine (USMPSMDHDF1): switch to the repo

The code already exists there, so back it up and clone into the same path so
nothing downstream changes.

```bash
cd "/Users/hdfuser/Documents/Scripts/Python/UPM Release WorkFlow Automation"
mv files "files_backup_$(date +%Y%m%d)"
git clone https://github.com/jlagana25/upm-release-workflow.git files
cd files
python3 smoke_test.py           # confirm it runs from the clone
```

When the smoke test passes, the `files_backup_...` folder is just a safety net
you can delete later.

---

## Daily loop (either machine)

```bash
git pull                        # ALWAYS start here — get the latest
# ...make edits (by you, or Claude Code)...
make smoke                      # or: python3 smoke_test.py
git add -A
git commit -m "Short description of the change"
git push
# then on the other machine:
git pull
```

**Habit:** run `git status` whenever you sit down.
- "up to date" — good to go.
- "behind" — `git pull` before editing.
- "ahead" — you have local commits to `git push`.

---

## Common situations

- **Push rejected ("updates were rejected")** — the other machine pushed first.
  Fix: `git pull` (merges), then `git push` again. Normal.
- **See what changed before committing** — `git diff` (unstaged) / `git status`.
- **Undo uncommitted edits to a file** — `git restore <file>`.
- **Undo the last commit but keep the edits** — `git reset --soft HEAD~1`.
- **Who can see the repo** — private; add teammates at
  GitHub -> repo -> Settings -> Collaborators.

---

## Editing with Claude Code (optional)

Claude Code is Anthropic's terminal coding agent. It edits the real files in
`files/` directly, can run the smoke test / dry-runs itself, and makes git
commits — so code changes skip the download-and-verify round trip. It works on
top of the git setup above: its edits are normal `git diff`s you review before
committing.

**Install** (on the machine you edit from — the native installer needs no Node.js):

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

Docs: https://docs.claude.com/en/docs/claude-code/overview
It signs in with your Claude subscription (Pro/Max) or an API key on first launch.

**Use it** — run it from inside the repo and talk to it in plain English:

```bash
cd "/Users/hdfuser/Documents/Python/UPM Release WorkFlow Automation/files"
claude
```

Example asks: "add a `--skip-...` flag for X", "make this error message clearer",
"run the smoke test and fix whatever breaks". It reads the files, edits them, and
can run `python3 smoke_test.py` / `make verify` itself and iterate until green.

**The loop with Claude Code:**

```bash
git pull                        # start current
claude                          # make changes by asking; review its diffs
make smoke                      # (it can run this for you)
git add -A && git commit -m "what changed" && git push
# other machine:  git pull
```

**Boundaries:**
- It edits code on the machine it runs on — it will **not** drive the Soundminer
  GUI, mount the Pegasus volumes, or execute a real release. Those are unchanged.
- Review its diffs before committing; `git restore <file>` discards a change you
  don't want.
- It's tied to your paid Claude plan / API billing.

---

## New machine bootstrap (first-time setup from scratch)

Cloning the repo gets the code **and** the reference crops, but a machine that
has never run this workflow needs more than code. Full checklist:

1. **Clone the repo** (see "One-time setup" above for `gh auth`, then clone into
   the canonical `files/` path).
2. **Python dependencies:** `pip install -r requirements.lock` (or
   `requirements.txt`). Then `python3 -m playwright install chromium`.
3. **ffmpeg:** `brew install ffmpeg` (needed for Step 12.7 WAV→MP3).
4. **Apps:** install and sign into **Soundminer** and **UniSync**, and confirm
   their databases/licenses are set up (e.g. the SourceAudio DB on ⌘8).
5. **macOS permissions** (System Settings → Privacy & Security): grant the
   Terminal app both **Accessibility** and **Screen Recording** — the GUI
   automation silently fails without them.
6. **Mount the Pegasus volumes** (`Pegasus32 R8 - 1` and `- 2`).
7. **Reference crops:** they arrive with the clone in `files/screenshots/`. If
   the display/resolution differs from where they were captured, the Soundminer
   ones can be re-captured with `python3 make_soundminer_crops.py`; UniSync crops
   are re-cropped by hand (see the next section).
8. **Domo/Microsoft login:** the first `--test` Domo run opens a browser to sign
   in.
9. **Verify:** `python3 smoke_test.py`, then a dry run:
   `python3 upm_release_workflow.py --previous-month --dry-run`.

Run `python3 upm_release_workflow.py --list-steps` any time for the current step
map.

---

## Reference screenshots (crops)

The GUI automation (Soundminer, UniSync) finds on-screen buttons by matching
against tight PNG **crops** in `files/screenshots/` — now versioned in git, so a
clone has them. They're captured from a specific Mac's display; on very different
hardware/resolution some may need re-capturing. `soundminer.py` fails loudly
naming any missing required crop.

**Soundminer — required** (run `python3 make_soundminer_crops.py`, which walks you
through these):
- `soundminer_db_nbc_selected.png`, `soundminer_mirror_title.png`,
  `soundminer_mirror_ok.png`

**Soundminer — optional dialogs** (only auto-dismissed if present; crop tightly
around the distinctive text/button):
- `soundminer_importing_text.png`, `soundminer_unmatched_fields.png`,
  `soundminer_dupes_warning.png`, `soundminer_log_window.png`

**UniSync** (no capture helper yet — crop each tightly around the named UI
element and save with the exact filename into `files/screenshots/`):
- `unisync_hamburger_btn.png`, `unisync_choose_csv.png`,
  `unisync_cache_btn.png`, `unisync_client_btn.png`,
  `unisync_territory_dropdown.png`, `unisync_terr_united_states.png`,
  `unisync_terr_united_states_mp3.png`, `unisync_terr_rest_of_world.png`,
  `unisync_terr_rest_of_world_mp3.png`, `unisync_terr_japan.png`

To capture a crop by hand: press ⌘⇧4, drag a tight box around just the element,
and rename the resulting Desktop PNG to the exact filename above. Keep crops
small and distinctive (the button/label only, no surrounding chrome).

---

## Notes

- The repo versions the **code and the reference crops**. Release runs, the
  Soundminer/UniSync GUI apps themselves, logs, and the Pegasus volumes are not
  in git.
- `requirements.lock` (from `make lock`) IS committed so both Macs pin identical
  dependency versions; regenerate it after changing dependencies.
- Hidden files like `.gitignore` travel with git automatically — no more
  hand-copying them between machines.
