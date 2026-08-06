#!/usr/bin/env bash
#
# Container setup for the UPM Release Workflow repo (Codex / any headless Linux
# sandbox).  Paste the body of this file into the Codex environment "setup
# script" field, or run it directly:  ./setup.sh
#
# Installs ONLY the dependencies needed for code work and tests.  The GUI
# (pyautogui, opencv-python, Pillow) and browser (playwright) packages are
# intentionally omitted: they can't run in a headless container, and every
# import of them in this codebase is lazy (inside functions), so the modules
# import fine without them.  See AGENTS.md §2 and §3.

set -euo pipefail

echo "==> Upgrading pip"
python3 -m pip install --upgrade pip

echo "==> Installing core dependencies (no GUI/browser libs)"
python3 -m pip install \
  "python-docx>=1.1.0" \
  "pandas>=2.0.0" \
  "openpyxl>=3.1.0" \
  "numpy>=1.24.0" \
  "requests>=2.31.0" \
  "urllib3<2" \
  "python-dateutil>=2.9.0"

echo "==> Sanity check: import every module + verify wiring"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git config core.hooksPath .githooks
fi
python3 auth_manager.py --permissions
python3 security_scan.py
python3 smoke_test.py

echo "==> Setup complete."
echo "    Validate changes with:  python3 smoke_test.py   (see AGENTS.md §4)"
echo "    Do NOT run the pipeline here — it needs the Macs' GUI apps + volumes."
