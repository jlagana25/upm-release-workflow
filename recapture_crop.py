#!/usr/bin/env python3
"""
Re-capture a UniSync crop FROM pyautogui's own screenshot, so it matches the
live screen exactly (no ⌘⇧4 color-profile / scaling mismatch).

USAGE (from files/):
    python3 recapture_crop.py unisync_hamburger_btn.png
    python3 recapture_crop.py unisync_choose_csv.png

It counts down 5 seconds (bring UniSync to the front, showing the control),
grabs the screen the way the automation does, opens it, and you drag a tight
box around the control.  On release it saves that region into this machine's
screenshots folder under the given name.  Reads the screen; writes one PNG.
"""
import socket
import sys
import time
from pathlib import Path

if len(sys.argv) < 2 or not sys.argv[1].endswith(".png"):
    print("Usage: python3 recapture_crop.py <crop_filename.png>")
    sys.exit(1)
CROP_NAME = sys.argv[1]

HOST = socket.gethostname().split(".")[0].strip().upper()
DEST_DIR = Path(__file__).resolve().parent / "screenshots" / HOST
DEST_DIR.mkdir(parents=True, exist_ok=True)
DEST = DEST_DIR / CROP_NAME

try:
    import pyautogui
    import tkinter as tk
    from PIL import Image, ImageTk
except Exception as exc:
    print("Missing dependency:", exc)
    print("Install:  pip install pyautogui pillow")
    sys.exit(1)

print(f"Saving to: {DEST}")
print("Bring UniSync to the FRONT now, showing the control you want to capture.")
for n in (5, 4, 3, 2, 1):
    print(f"  capturing in {n}...", end="\r", flush=True)
    time.sleep(1)
print("\nCapturing screen (pyautogui)...")
shot = pyautogui.screenshot().convert("RGB")
full_w, full_h = shot.size
print(f"Captured {full_w} x {full_h}. Drag a tight box around the control, then release.")

# Fit the image into a window no larger than this, keep aspect ratio.
MAX_W, MAX_H = 1500, 850
scale = min(MAX_W / full_w, MAX_H / full_h, 1.0)
disp_w, disp_h = round(full_w * scale), round(full_h * scale)
disp_img = shot.resize((disp_w, disp_h), Image.LANCZOS)

root = tk.Tk()
root.title(f"Drag a box around the control  →  {CROP_NAME}   (Esc to cancel)")
root.attributes("-topmost", True)
canvas = tk.Canvas(root, width=disp_w, height=disp_h, cursor="crosshair",
                   highlightthickness=0)
canvas.pack()
photo = ImageTk.PhotoImage(disp_img)
canvas.create_image(0, 0, anchor="nw", image=photo)

state = {"x0": None, "y0": None, "rect": None, "saved": False}

def on_press(e):
    state["x0"], state["y0"] = e.x, e.y
    if state["rect"]:
        canvas.delete(state["rect"])
    state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                             outline="red", width=2)

def on_drag(e):
    if state["rect"]:
        canvas.coords(state["rect"], state["x0"], state["y0"], e.x, e.y)

def on_release(e):
    x0, y0 = state["x0"], state["y0"]
    x1, y1 = e.x, e.y
    if x0 is None:
        return
    L, R = sorted((x0, x1)); T, B = sorted((y0, y1))
    if R - L < 4 or B - T < 4:
        print("Box too small — try again."); return
    # Map display coords back to full-resolution pixels.
    fl, fr = round(L / scale), round(R / scale)
    ft, fb = round(T / scale), round(B / scale)
    crop = shot.crop((fl, ft, fr, fb))
    crop.save(DEST)
    state["saved"] = True
    print(f"\n✓ Saved {crop.size[0]} x {crop.size[1]} px  →  {DEST}")
    root.destroy()

def on_escape(e):
    print("Cancelled — nothing saved."); root.destroy()

canvas.bind("<ButtonPress-1>", on_press)
canvas.bind("<B1-Motion>", on_drag)
canvas.bind("<ButtonRelease-1>", on_release)
root.bind("<Escape>", on_escape)
root.mainloop()

if not state["saved"]:
    print("No crop saved.")
