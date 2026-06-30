#!/usr/bin/env python3
"""
make_soundminer_crops.py
========================

Capture the four reference screenshot crops that soundminer.py
image-matches against, at the NATIVE screen scale pyautogui will see at
run time.

Why this exists
---------------
pyautogui's locateOnScreen() compares a small reference PNG against a
live screen capture.  The match is scale-sensitive: a crop taken at a
different resolution (e.g. a screenshot that got downscaled when emailed
or uploaded) will silently fail to match even though it "looks right" to
a human.  The only reliable references are ones captured on THIS machine,
on THIS display, via macOS's own `screencapture`.

This helper walks you through capturing each of the four crops with
`screencapture -i` (the same drag-to-select crosshair as Cmd+Shift+4),
saving each directly to soundminer.py's SCREENSHOTS_DIR under the exact
filename the module expects.

The crosshair is a system overlay, so it can sit on top of an open
context menu without dismissing it — that's how we capture the transient
"Embed selected records" menu item.  A short countdown after you press
Enter gives you time to open that menu before the crosshair appears.

Usage
-----
    python3 make_soundminer_crops.py
    python3 make_soundminer_crops.py --delay 6      # longer countdown
    python3 make_soundminer_crops.py --only embed   # recapture one crop

Re-running is safe: it overwrites only the crops you capture this pass.
Press Escape during the crosshair to skip a crop (leaves any existing
file untouched).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

# Pull the canonical destination + filenames straight from soundminer.py so
# the two never drift apart.  This import only needs `config` (soundminer
# imports pyautogui lazily inside functions, never at module top), so it
# works on a machine where pyautogui isn't installed yet.
try:
    from soundminer import SCREENSHOTS_DIR, REQUIRED_SCREENSHOTS
except Exception as exc:  # pragma: no cover - only hit on a broken checkout
    print(f"ERROR: could not import soundminer.py: {exc}", file=sys.stderr)
    print(
        "  Run this from the same folder as soundminer.py and config.py.",
        file=sys.stderr,
    )
    sys.exit(2)


# Per-crop guidance.  `needs_menu` flags the one transient capture so the
# instructions can tell the operator to open the context menu during the
# countdown.  `state` is what the UI should look like; `box` is what to
# drag the selection rectangle around.
CROP_GUIDE: dict[str, dict] = {
    "db_nbc_selected": {
        "state": "Soundminer open, NBCUniversal selected (press \u23196 if not).",
        "box":   "the toolbar 'NBCUniversal \u25be' dropdown control "
                 "(the labelled box near the top-left, next to ColumnView).",
        "needs_menu": False,
    },
    "mirror_title": {
        "state": "Open Database \u2192 Mirror so the Mirror Settings dialog is showing.",
        "box":   "just the 'Mirror Settings' title-bar text at the top of "
                 "the dialog (a small, distinctive strip).",
        "needs_menu": False,
    },
    "mirror_ok": {
        "state": "Mirror Settings dialog still open.",
        "box":   "the OK button in the bottom-right of the Mirror Settings "
                 "dialog (just the button, a little padding is fine).",
        "needs_menu": False,
    },
    "embed_menu": {
        "state": "Records imported and selected (\u2318A). You'll open the "
                 "right-click menu during the countdown.",
        "box":   "just the 'Embed selected records' row in the context menu.",
        "needs_menu": True,
    },
}


def _key_for_filename(filename: str) -> str:
    """Reverse-lookup the REQUIRED_SCREENSHOTS key for a given filename."""
    for key, fname in REQUIRED_SCREENSHOTS.items():
        if fname == filename:
            return key
    return ""


def _capture_one(key: str, filename: str, delay: int) -> bool:
    """
    Walk the operator through capturing one crop.  Returns True if a file
    was written, False if skipped/aborted.
    """
    guide = CROP_GUIDE.get(key, {})
    dest = SCREENSHOTS_DIR / filename

    print("\n" + "=" * 70)
    print(f"  CROP: {filename}")
    print("=" * 70)
    print(f"  Get the UI ready:  {guide.get('state', '(see soundminer.py)')}")
    print(f"  You will box:      {guide.get('box', '(the relevant element)')}")
    print()

    if guide.get("needs_menu"):
        print(f"  This is a CONTEXT MENU capture.  After you press Enter you")
        print(f"  have {delay} seconds to:")
        print(f"     1. Click into Soundminer.")
        print(f"     2. Right-click a selected record to open the menu.")
        print(f"     3. Leave the menu open.")
        print(f"  The crosshair will appear over the open menu — then drag a")
        print(f"  box around just the 'Embed selected records' row.")
    else:
        print(f"  After you press Enter you have {delay} seconds to bring the")
        print(f"  element into view, then the crosshair appears — drag a box")
        print(f"  around it.  (Escape cancels without saving.)")

    try:
        input("\n  Press Enter when you're ready to start the countdown… ")
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        return False

    for remaining in range(delay, 0, -1):
        print(f"\r  Crosshair in {remaining}s — get the UI ready now… ",
              end="", flush=True)
        time.sleep(1)
    print("\r  Drag a box now (Escape to cancel).                    ")

    # -i = interactive selection.  Saves the cropped PNG to `dest`.
    # If the operator presses Escape, screencapture exits 0 but writes
    # nothing, so we check for the file afterwards.
    existed_before = dest.exists()
    mtime_before = dest.stat().st_mtime if existed_before else 0.0

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["screencapture", "-i", str(dest)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  ✗ screencapture failed: {result.stderr.strip()}")
        return False

    # Determine whether a NEW capture landed
    if dest.exists():
        mtime_after = dest.stat().st_mtime
        if (not existed_before) or (mtime_after > mtime_before):
            try:
                from PIL import Image
                with Image.open(dest) as im:
                    w, h = im.size
                print(f"  ✓ Saved {filename}  ({w}×{h}px) → {dest}")
            except Exception:
                print(f"  ✓ Saved {filename} → {dest}")
            return True

    print(f"  ⚠ No new capture saved (Escape pressed?). "
          f"{'Existing file kept.' if existed_before else 'Nothing written.'}")
    return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Capture Soundminer reference crops at native scale.",
    )
    p.add_argument(
        "--delay", type=int, default=5,
        help="Seconds between pressing Enter and the crosshair appearing "
             "(default 5; bump for the context-menu capture if you need "
             "more time).",
    )
    p.add_argument(
        "--only", default=None, metavar="SUBSTR",
        help="Capture only the crop whose key contains SUBSTR "
             "(e.g. --only embed, --only mirror_ok). Omit to capture all four.",
    )
    args = p.parse_args(argv)

    if sys.platform != "darwin":
        print("ERROR: this helper uses macOS `screencapture`; run it on the Mac.",
              file=sys.stderr)
        return 2

    # Resolve which crops to capture
    items = list(REQUIRED_SCREENSHOTS.items())  # [(key, filename), ...]
    if args.only:
        needle = args.only.lower()
        items = [
            (k, f) for k, f in items
            if needle in k.lower() or needle in f.lower()
        ]
        if not items:
            print(f"No crop matched --only {args.only!r}. Available keys:")
            for k in REQUIRED_SCREENSHOTS:
                print(f"  {k}")
            return 1

    print("Soundminer reference-crop capture")
    print(f"  Destination: {SCREENSHOTS_DIR}")
    print(f"  Crops to capture this pass: {[k for k, _ in items]}")
    print()
    print("  TIP: these references must be captured on the same Mac/display")
    print("       you'll run soundminer.py on, or image-matching may miss.")

    captured = 0
    for key, filename in items:
        if _capture_one(key, filename, args.delay):
            captured += 1

    print("\n" + "=" * 70)
    print(f"  Done — {captured}/{len(items)} crop(s) captured this pass.")
    # Report overall readiness
    missing = [
        f for _, f in REQUIRED_SCREENSHOTS.items()
        if not (SCREENSHOTS_DIR / f).exists()
    ]
    if missing:
        print(f"  Still missing for a real run: {missing}")
        print(f"  Re-run with --only <name> to capture them.")
    else:
        print("  ✓ All four reference crops are present. soundminer.py is ready")
        print("    to image-match. Do an attended (non --unattended) run first.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
