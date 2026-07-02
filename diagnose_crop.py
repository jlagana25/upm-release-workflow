#!/usr/bin/env python3
"""
One-shot diagnostic for the UniSync territory-dropdown crop mismatch.

WHY THIS EXISTS
  The workflow says 'unisync_territory_dropdown.png' not found on screen, even
  though the file exists and UniSync is visible.  This script tests the crop
  against your LIVE screen right now and reports the actual cause, so we stop
  guessing.

HOW TO RUN
  1. Open UniSync and get it to the SAME screen the automation sees when it
     logs "Setting Territory" — i.e. the territory dropdown visible, UniSync
     frontmost and unobscured.
  2. In Terminal, from the files/ folder:
         python3 diagnose_crop.py
  3. Paste the whole output back.

It matches nothing destructive — it only reads the screen and the crop file.
"""

import sys
import tempfile
from pathlib import Path
import socket

# ---- Resolve the crop exactly like the automation does (per-machine folder) --
HOST = socket.gethostname().split(".")[0].strip().upper()
SCREENSHOTS_DIR = Path(__file__).resolve().parent / "screenshots" / HOST
CROP = SCREENSHOTS_DIR / "unisync_territory_dropdown.png"

try:
    import pyautogui
    from PIL import Image
except Exception as exc:  # pragma: no cover
    print("Missing dependency:", exc)
    print("Install with:  pip install pyautogui pillow opencv-python")
    sys.exit(1)

# Pillow renamed the resample constants; support both.
try:
    LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # older Pillow
    LANCZOS = Image.LANCZOS

print("=" * 60)
print(f"Machine:        {HOST}")
print(f"Crop folder:    {SCREENSHOTS_DIR}")
print(f"Crop file:      {CROP.name}")
if not CROP.exists():
    print("  ✗ Crop file not found in this machine's folder.")
    sys.exit(1)

crop = Image.open(CROP)
print(f"Crop size (px): {crop.size[0]} x {crop.size[1]}")

try:
    shot = pyautogui.screenshot()
    print(f"Live screen (px): {shot.size[0]} x {shot.size[1]}")
except Exception as exc:
    print("  ✗ Could not capture the screen:", exc)
    print("  → Grant Terminal 'Screen Recording' permission and retry.")
    sys.exit(1)


def try_match(image_path, label):
    """Try to locate image_path on screen at descending confidences."""
    for conf in (0.90, 0.80, 0.70, 0.60):
        try:
            box = pyautogui.locateOnScreen(str(image_path), confidence=conf)
        except Exception:
            box = None
        if box:
            print(f"  ✓ {label}: MATCH at confidence {conf}")
            return conf
    print(f"  ✗ {label}: no match down to confidence 0.60")
    return None


def scaled_copy(factor):
    w, h = crop.size
    resized = crop.resize((max(1, round(w * factor)), max(1, round(h * factor))), LANCZOS)
    out = Path(tempfile.gettempdir()) / f"terr_scaled_{factor}.png"
    resized.save(out)
    return out


print("\n-- testing ORIGINAL crop --")
orig = try_match(CROP, "original")

print("\n-- testing 50% crop (checks Retina: screen captured at 1x, crop at 2x) --")
half = try_match(scaled_copy(0.5), "50%")

print("\n-- testing 200% crop (checks the reverse: screen 2x, crop 1x) --")
dbl = try_match(scaled_copy(2.0), "200%")

print("\n" + "=" * 60)
print("VERDICT")
print("=" * 60)
if orig and orig >= 0.85:
    print("Original crop matches at the automation's threshold (0.85+).")
    print("→ The crop is fine. If the real run still fails, UniSync wasn't")
    print("  frontmost/unobscured at that moment. Nothing to re-capture.")
elif orig:
    print(f"Original crop matches, but only at confidence {orig} (< 0.85).")
    print(f"→ FIX: lower LOCATE_CONFIDENCE to {orig - 0.05:.2f} in unisync_automation.py.")
    print("  Minor rendering/antialiasing difference — no re-capture needed.")
elif half:
    print("The 50%-scaled crop matches but full size doesn't.")
    print("→ RETINA 2x MISMATCH: your crop is double the scale pyautogui sees.")
    print("  FIX (code): make the matcher downscale-tolerant — no re-capture needed.")
elif dbl:
    print("The 200%-scaled crop matches but full size doesn't.")
    print("→ Reverse scale mismatch (screen 2x, crop 1x).")
    print("  FIX (code): make the matcher upscale-tolerant — no re-capture needed.")
else:
    print("Nothing matches at any scale or confidence.")
    print("→ The crop's CONTENT isn't on screen as captured (wrong window/state,")
    print("  or the box included something that changes). This is the ONE case")
    print("  where a re-capture is the right fix — and now we know for sure.")
print("=" * 60)
