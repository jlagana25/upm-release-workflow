"""
audio_conversion.py — Step 12.7: NBC WAV → MP3 Conversion
==========================================================

Converts the NBC WAV files mirrored by Soundminer (Step 12.6) to MP3,
preserving the subfolder structure.

Source:
    ctx.partner_dirs["nbc_wav_music"]
        …/3-FINAL PACKAGING/Universal Production Music {mdf} Release - NBC/Music/WAV

Destination (mirrors the source's subfolder layout):
    ctx.partner_dirs["nbc_mp3_music"]
        …/3-FINAL PACKAGING/Universal Production Music {mdf} Release - NBC/Music/MP3

Encoding:
    Codec:    libmp3lame
    Bitrate:  320k
    Channels & sample-rate: preserved from source where possible (ffmpeg
              defaults to copying the source rate/channel count for MP3,
              and we pass them explicitly when ffprobe can read them so the
              intent is unambiguous).

Requires ffmpeg on PATH (checked during preflight).  ffprobe is used when
present to read the source rate/channels; if it's unavailable we let
ffmpeg carry the source values across implicitly.

Honours --dry-run (lists work, encodes nothing) and --overwrite (re-encode
files that already exist; otherwise existing MP3s are skipped).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from config import ReleaseContext, context_from_cli_args


# MP3 encoding parameters
MP3_CODEC   = "libmp3lame"
MP3_BITRATE = "320k"

# Sample rates libmp3lame (MPEG-1/2/2.5 Layer III) can actually encode.
# Anything above 48000 (e.g. 96k/192k production masters) is NOT valid for
# MP3, so we map a too-high source rate down to the highest supported rate
# rather than letting ffmpeg fail.  "Preserve source rate WHERE POSSIBLE."
_MP3_VALID_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)


def _mp3_safe_rate(source_rate: Optional[int]) -> Optional[int]:
    """
    Return an MP3-valid sample rate for `source_rate`.

    - None  → None (let ffmpeg decide).
    - A rate MP3 supports → unchanged (preserved).
    - A rate above 48000 → 48000 (the highest MP3 rate; downsampled).
    - An odd rate MP3 can't do → the nearest supported rate ≤ source,
      falling back to the lowest supported rate.
    """
    if source_rate is None:
        return None
    if source_rate in _MP3_VALID_RATES:
        return source_rate
    if source_rate > 48000:
        return 48000
    # Unusual sub-48k rate not in the valid set: pick the closest supported
    # rate at or below it (never upsample), else the minimum.
    candidates = [r for r in _MP3_VALID_RATES if r <= source_rate]
    return max(candidates) if candidates else min(_MP3_VALID_RATES)


def _probe_audio_stream(ffprobe: str, wav_path: Path) -> dict:
    """
    Return {"sample_rate": int|None, "channels": int|None} for the first
    audio stream in `wav_path`, using ffprobe.  Returns Nones if ffprobe
    isn't available or the probe fails — callers then let ffmpeg preserve
    the source values implicitly.
    """
    if not ffprobe:
        return {"sample_rate": None, "channels": None}
    try:
        # Capture as bytes; decode stdout with errors="replace" before JSON
        # parsing.  Avoids UnicodeDecodeError when ffprobe emits non-UTF-8
        # bytes (e.g. from source metadata).  We don't pass check=True so a
        # non-zero exit just yields the fallback Nones below.
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=sample_rate,channels",
                "-of", "json",
                str(wav_path),
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            return {"sample_rate": None, "channels": None}
        stdout = result.stdout.decode("utf-8", "replace")
        data = json.loads(stdout or "{}")
        streams = data.get("streams") or [{}]
        s = streams[0]
        sr = int(s["sample_rate"]) if s.get("sample_rate") else None
        ch = int(s["channels"]) if s.get("channels") else None
        return {"sample_rate": sr, "channels": ch}
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return {"sample_rate": None, "channels": None}


def _build_ffmpeg_cmd(
    ffmpeg: str,
    wav_path: Path,
    mp3_path: Path,
    *,
    overwrite: bool,
    sample_rate: Optional[int],
    channels: Optional[int],
) -> list[str]:
    """Assemble the ffmpeg command line for one file."""
    cmd = [
        ffmpeg,
        "-y" if overwrite else "-n",
        "-i", str(wav_path),
        "-codec:a", MP3_CODEC,
        "-b:a", MP3_BITRATE,
    ]
    # Preserve source sample rate / channels explicitly when known.
    # MP3 can't encode above 48k, so clamp the rate to a supported value
    # (a no-op for the common 44.1k/48k cases).
    safe_rate = _mp3_safe_rate(sample_rate)
    if safe_rate:
        cmd += ["-ar", str(safe_rate)]
    if channels:
        cmd += ["-ac", str(channels)]
    # Carry metadata across (Soundminer embeds BWF/ID3-relevant tags).
    cmd += ["-map_metadata", "0", str(mp3_path)]
    return cmd


def _flatten_mirrored_media(
    wav_root: Path,
    logger: logging.Logger,
    *,
    dry_run: bool,
) -> bool:
    """
    Undo Soundminer's "Mirror Source Folder Structure" nesting.

    When the Mirror export uses Destination Folder Structure = 'Mirror Source
    Folder Structure', Soundminer recreates the source path under WAV/, so
    the audio ends up nested like:

        WAV/_Specials/UPM/{root}/2-STAGING/SME WAV 48K NBC/MEDIA/<labels>/*.wav

    This lifts that MEDIA folder up to WAV/MEDIA and removes the leftover
    scaffold (the WAV/_Specials/… chain), leaving:

        WAV/MEDIA/<labels>/*.wav

    Self-gating: it looks for a `MEDIA` directory nested below WAV/ (i.e. not
    already WAV/MEDIA).  If none exists — a flat mirror, or already
    flattened — it's a no-op.  This is how we honour "only when Destination
    Folder Structure = Mirror Source Folder Structure" without reading
    Soundminer's setting: the nested MEDIA folder IS the signal that the
    setting was used.

    If WAV/MEDIA already exists (e.g. a prior partial run), it is OVERWRITTEN.

    Honours dry_run (describes the move, changes nothing).  Returns True on
    success or no-op, False on error.
    """
    # A nested MEDIA dir = named "MEDIA", below wav_root, but NOT directly
    # wav_root/MEDIA (which is the already-flattened target).
    nested = [
        d for d in wav_root.rglob("MEDIA")
        if d.is_dir() and d.parent != wav_root
    ]
    if not nested:
        logger.debug("  No nested MEDIA folder under WAV/ — no flatten needed.")
        return True

    # Prefer nested MEDIA dirs that actually contain audio (guards against an
    # incidental empty dir named MEDIA somewhere).
    with_audio = [
        d for d in nested
        if next(d.rglob("*.wav"), None) or next(d.rglob("*.WAV"), None)
    ]
    chosen = with_audio or nested
    if len(chosen) > 1:
        logger.error(
            "  ✗  Multiple nested MEDIA folders found under WAV/; refusing to "
            "guess which to flatten:\n"
            + "\n".join(f"       {d}" for d in chosen)
        )
        return False

    nested_media = chosen[0]
    target       = wav_root / "MEDIA"
    # The top of the leftover scaffold = the first path component below
    # wav_root on the way to the nested MEDIA (e.g. wav_root/_Specials).
    top_scaffold = wav_root / nested_media.relative_to(wav_root).parts[0]

    logger.info("  Detected 'Mirror Source Folder Structure' nesting:")
    logger.info(f"      nested: {nested_media}")
    logger.info(f"      →   to: {target}")

    if dry_run:
        logger.info(
            "  [DRY RUN] Would move that MEDIA folder up to WAV/MEDIA "
            f"(overwriting any existing) and remove the scaffold under "
            f"{top_scaffold}."
        )
        return True

    try:
        if target.exists():
            logger.info(f"      Overwriting existing {target}")
            shutil.rmtree(target)
        shutil.move(str(nested_media), str(target))
        logger.info("      MEDIA folder moved up to WAV/MEDIA.")

        # Remove the now-empty scaffold (WAV/_Specials/…), but only if no
        # audio remains under it — never delete files unexpectedly.
        if top_scaffold.exists() and top_scaffold != target:
            leftover = (
                next(top_scaffold.rglob("*.wav"), None)
                or next(top_scaffold.rglob("*.WAV"), None)
            )
            if leftover:
                logger.warning(
                    f"      ⚠ Scaffold {top_scaffold} still contains audio; "
                    "leaving it in place for inspection."
                )
            else:
                shutil.rmtree(top_scaffold)
                logger.info(f"      Removed leftover scaffold: {top_scaffold}")

        logger.info("  ✓ Flatten complete — WAV/MEDIA/<labels>/ ready.")
        return True
    except OSError as exc:
        logger.error(f"  ✗ Failed to flatten mirrored MEDIA folder: {exc}")
        return False


def convert_nbc_wav_to_mp3(
    ctx: ReleaseContext,
    dry_run: bool,
    overwrite: bool,
    logger: logging.Logger,
) -> bool:
    """
    Walk the NBC WAV directory and convert each WAV to MP3, mirroring the
    subfolder structure under the NBC MP3 directory.

    - Recursive over the WAV tree; subfolder layout preserved in the output.
    - libmp3lame @ 320k; source sample rate & channels preserved.
    - Existing MP3s skipped unless `overwrite` is True.
    - dry_run lists the work without encoding.

    Returns True on success (including a clean no-op or dry-run), False if
    any conversion errors or a required tool/path is missing.
    """
    wav_root = ctx.partner_dirs.get("nbc_wav_music")
    mp3_root = ctx.partner_dirs.get("nbc_mp3_music")

    logger.info("─── Step 12.7 — NBC WAV → MP3 Conversion ──────────────────")
    logger.info(f"  Source (WAV): {wav_root}")
    logger.info(f"  Output (MP3): {mp3_root}")

    # --- Scope / safety guards ---------------------------------------------
    if wav_root is None or mp3_root is None:
        logger.error(
            "  ✗  nbc_wav_music / nbc_mp3_music not defined in "
            "ctx.partner_dirs; refusing to run."
        )
        return False

    # Confirm both paths sit under the expected NBC Music tree, so a config
    # change can't repoint conversion at an unintended location.
    nbc_music_tail = (
        Path("3-FINAL PACKAGING")
        / f"Universal Production Music {ctx.month_display_folder} Release - NBC"
        / "Music"
    )
    if not (str(wav_root).endswith(str(nbc_music_tail / "WAV"))
            and str(mp3_root).endswith(str(nbc_music_tail / "MP3"))):
        logger.error(
            "  ✗  WAV/MP3 paths don't match the expected NBC Music structure:\n"
            f"     WAV: {wav_root}\n"
            f"     MP3: {mp3_root}\n"
            f"     expected to end with: {nbc_music_tail}/WAV and …/MP3\n"
            "     Refusing to run."
        )
        return False

    if not wav_root.exists():
        msg = (
            f"  NBC WAV directory not found:\n     {wav_root}\n"
            "     Run Step 12.6 (Soundminer Mirror) before audio conversion."
        )
        if dry_run:
            logger.warning("  ⚠ " + msg.strip())
            logger.info(
                "  [DRY RUN] Skipping conversion preview "
                "(NBC WAV tree not present yet — produced by Step 12)."
            )
            return True
        logger.error("  ✗ " + msg)
        return False

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.error(
            "  ✗  ffmpeg not found on PATH.  Install it (e.g. `brew install "
            "ffmpeg`) and re-run."
        )
        return False
    ffprobe = shutil.which("ffprobe") or ""
    if not ffprobe:
        logger.warning(
            "  ⚠ ffprobe not found; ffmpeg will preserve source rate/channels "
            "implicitly (no explicit -ar/-ac)."
        )

    # If Soundminer mirrored with 'Mirror Source Folder Structure', the audio
    # is nested under a replicated source path inside WAV/.  Lift it up to
    # WAV/MEDIA before converting (no-op for a flat mirror).
    if not _flatten_mirrored_media(wav_root, logger, dry_run=dry_run):
        return False

    # Collect WAV files (both .wav and .WAV — macOS is case-insensitive on
    # the filesystem, but rglob is case-sensitive, so match both to be safe).
    wav_files = sorted(
        {p for p in wav_root.rglob("*.wav")} | {p for p in wav_root.rglob("*.WAV")}
    )
    if not wav_files:
        logger.warning(f"  No WAV files found under {wav_root} — nothing to do.")
        return True  # not an error

    logger.info(
        f"  Found {len(wav_files)} WAV file(s).  Encoding {MP3_CODEC} @ "
        f"{MP3_BITRATE}{' (dry run)' if dry_run else ''}…"
    )

    converted = 0
    skipped   = 0
    errors    = 0

    for wav_path in wav_files:
        relative = wav_path.relative_to(wav_root)
        mp3_path = (mp3_root / relative).with_suffix(".mp3")

        if mp3_path.exists() and not overwrite:
            skipped += 1
            logger.debug(f"  Skipping (exists): {relative.with_suffix('.mp3')}")
            continue

        if dry_run:
            logger.info(f"  [DRY RUN] {relative}  →  {mp3_path.relative_to(mp3_root)}")
            converted += 1
            continue

        mp3_path.parent.mkdir(parents=True, exist_ok=True)

        probe = _probe_audio_stream(ffprobe, wav_path)
        src_rate = probe["sample_rate"]
        safe_rate = _mp3_safe_rate(src_rate)
        if src_rate and safe_rate and safe_rate != src_rate:
            logger.info(
                f"      (source {src_rate} Hz exceeds MP3's max; encoding at "
                f"{safe_rate} Hz)"
            )
        cmd = _build_ffmpeg_cmd(
            ffmpeg, wav_path, mp3_path,
            overwrite=overwrite,
            sample_rate=probe["sample_rate"],
            channels=probe["channels"],
        )

        try:
            # Capture as BYTES (no text=True): ffmpeg's stderr can contain
            # non-UTF-8 bytes from source ID3/BWF metadata, and strict UTF-8
            # decoding (text=True) crashes on them.  We decode ourselves with
            # errors="replace" only when we need to show a message.
            proc = subprocess.run(cmd, capture_output=True)
            if proc.returncode != 0:
                errors += 1
                tail = proc.stderr.decode("utf-8", "replace")[-500:]
                logger.error(f"  ✗ ffmpeg failed on {relative}:\n     {tail}")
            else:
                converted += 1
                logger.info(f"  ✎ {relative}  →  {mp3_path.relative_to(mp3_root)}")
        except OSError as exc:
            errors += 1
            logger.error(f"  ✗ Error converting {relative}: {exc}")

    # --- Summary ------------------------------------------------------------
    logger.info("  ─── Step 12.7 summary ───")
    logger.info(f"    WAV files found:      {len(wav_files)}")
    logger.info(
        f"    {'Would convert' if dry_run else 'Converted'}:        {converted}"
    )
    logger.info(f"    Skipped (exists):     {skipped}")
    if errors:
        logger.info(f"    Errors:               {errors}")

    if errors:
        logger.error(
            f"  ✗  Step 12.7 finished with {errors} conversion error(s)."
        )
        return False

    if dry_run:
        logger.info("  ✓  Step 12.7 dry-run complete (nothing encoded).")
    else:
        logger.info("  ✓  Step 12.7 complete — NBC MP3s written.")
    return True


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def _run_cli(argv: Optional[list[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        description="Step 12.7 — convert NBC Music WAV files to 320k MP3, "
                    "mirroring the subfolder structure.",
    )
    p.add_argument("--test", action="store_true", required=True,
                   help="Confirm intent (parity with other modules).")
    p.add_argument("--year",  type=int)
    p.add_argument("--month", type=int)
    p.add_argument("--part",  type=int, choices=[1, 2])
    p.add_argument(
        "--previous-month", action="store_true",
        help="Full-month run for the previous month "
             "(no Part split). Relative to today, or to "
             "--year/--month if given.")
    p.add_argument("--dry-run", action="store_true",
                   help="List the conversions without encoding anything.")
    p.add_argument("--overwrite", action="store_true",
                   help="Re-encode MP3s that already exist (default: skip).")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("audio_conversion")

    ctx = context_from_cli_args(args)
    logger.info(f"Release context: {ctx}")

    ok = convert_nbc_wav_to_mp3(ctx, args.dry_run, args.overwrite, logger)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_run_cli())
