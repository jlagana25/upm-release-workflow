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

## Notes

- This versions the **code only**. Release runs, Soundminer GUI automation, and
  the Pegasus volumes are unaffected.
- `requirements.lock` (from `make lock`) IS committed so both Macs pin identical
  dependency versions; regenerate it after changing dependencies.
- Hidden files like `.gitignore` travel with git automatically — no more
  hand-copying them between machines.
