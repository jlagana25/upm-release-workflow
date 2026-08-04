"""
unisync_prefs.py — read/write UniSync's territory + cache/client paths directly
in its preferences file, so the pipeline can configure a job WITHOUT driving the
fragile macOS path-entry UI (Cmd+Shift+G / folder-icon clicks).

UniSync stores the last-used territory and the cache/client paths in:

    /Users/hdfuser/Library/SMUniSync/UniSync.xml

as attributes on an inner <userPrefs> element, e.g.:

    <userPrefs loginname="joseph.lagana@umusic.com" territory="United States (MP3)"
               cachePath="/Volumes/Pegasus32 R8 - 2/UPM-US-Cache/MP3"
               clientPath="/Volumes/Pegasus32 R8 - 1/_Specials/.../Music/MP3"/>

Writing these BEFORE launching UniSync should let it pick them up at startup,
which would eliminate the most failure-prone part of the whole automation.

IMPORTANT (must be validated on the real app): UniSync appears to read these at
launch and write them back on quit.  So the safe usage is:
    1. make sure UniSync is NOT running
    2. write the prefs for the job
    3. launch UniSync   (it reads territory/cache/client from here)
    4. load the CSV     (the auto-start trigger — still via the UI)

This module ONLY edits the file; it does not launch/quit UniSync.  The edit is
surgical: it changes the three attribute values on the inner <userPrefs> tag and
leaves loginname, the other <VALUE> entries, formatting, and CRLF line-endings
untouched.  A one-time .bak backup is made next to the file.
"""
from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
from pathlib import Path

DEFAULT_UNISYNC_XML = Path("/Users/hdfuser/Library/SMUniSync/UniSync.xml")

# The three attributes we manage on the inner <userPrefs> element.
_MANAGED = ("territory", "cachePath", "clientPath")


def _xml_escape(v: str) -> str:
    """Escape the few characters that aren't legal raw inside an XML attribute.
    UPM paths normally contain none of these, but we escape defensively."""
    return (
        v.replace("&", "&amp;")
         .replace('"', "&quot;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


def _find_userprefs_tag(text: str) -> re.Match | None:
    """Locate the inner self-closing <userPrefs .../> element (the one that
    carries cachePath/clientPath), not the outer <VALUE name="userPrefs">."""
    # The inner element is self-closing and carries cachePath; match the tag
    # that contains it so we never touch the outer wrapper.
    for m in re.finditer(r"<userPrefs\b[^>]*?/>", text, re.S):
        if "cachePath" in m.group(0) or "clientPath" in m.group(0) or "loginname" in m.group(0):
            return m
    # Fall back to the first self-closing userPrefs if attributes are absent.
    return re.search(r"<userPrefs\b[^>]*?/>", text, re.S)


def read_unisync_xml_prefs(xml_path: Path | str = DEFAULT_UNISYNC_XML) -> dict:
    """Return {loginname, territory, cachePath, clientPath} as found in the
    file (missing keys simply absent).  Returns {} if the file or the
    <userPrefs> element can't be read."""
    try:
        text = Path(xml_path).read_bytes().decode("utf-8")
    except OSError:
        return {}
    m = _find_userprefs_tag(text)
    if not m:
        return {}
    tag = m.group(0)
    out: dict[str, str] = {}
    for attr in ("loginname",) + _MANAGED:
        am = re.search(rf'\b{attr}="([^"]*)"', tag)
        if am:
            out[attr] = am.group(1)
    return out


def write_unisync_xml_prefs(
    territory: str,
    cache_path: str,
    client_path: str,
    xml_path: Path | str = DEFAULT_UNISYNC_XML,
    logger: logging.Logger | None = None,
    dry_run: bool = False,
    backup: bool = True,
) -> bool:
    """
    Set territory / cachePath / clientPath on the inner <userPrefs> element,
    preserving everything else (loginname, other VALUEs, formatting, CRLFs).

    Returns True on success (including a no-op when values already match),
    False if the file or element can't be read/written.
    """
    log = logger or logging.getLogger("unisync_prefs")
    p = Path(xml_path)

    try:
        text = p.read_bytes().decode("utf-8")
    except OSError as exc:
        log.error(f"  ✗  Cannot read UniSync.xml ({p}): {exc}")
        return False

    m = _find_userprefs_tag(text)
    if not m:
        log.error(f"  ✗  No <userPrefs .../> element found in {p}.")
        return False

    tag = m.group(0)
    new_tag = tag
    wanted = {"territory": territory, "cachePath": cache_path, "clientPath": client_path}

    for attr, val in wanted.items():
        esc = _xml_escape(val)
        if re.search(rf'\b{attr}="[^"]*"', new_tag):
            new_tag = re.sub(
                rf'(\b{attr}=")[^"]*(")',
                lambda mm: mm.group(1) + esc + mm.group(2),
                new_tag,
                count=1,
            )
        else:
            # Attribute absent — insert it just before the closing '/>'.
            new_tag = new_tag[:-2].rstrip() + f' {attr}="{esc}"/>'

    if new_tag == tag:
        log.info("  UniSync.xml already matches the requested paths — no change.")
        return True

    new_text = text[: m.start()] + new_tag + text[m.end():]

    log.info("  Updating UniSync.xml prefs →")
    log.info(f"    territory  = {territory}")
    log.info(f"    cachePath  = {cache_path}")
    log.info(f"    clientPath = {client_path}")
    if dry_run:
        log.info("  [dry-run] not writing.")
        return True

    try:
        bak = p.with_suffix(".xml.bak")
        if backup and not bak.exists():
            shutil.copy2(p, bak)
            log.info(f"    backup → {bak.name}")
        # write_bytes preserves the original CRLF line-endings exactly
        p.write_bytes(new_text.encode("utf-8"))
        log.info("  ✓  UniSync.xml updated.")
        return True
    except OSError as exc:
        log.error(f"  ✗  Cannot write UniSync.xml ({p}): {exc}")
        return False


# ---------------------------------------------------------------------------
# Validation CLI — run this on the pipeline machine to prove that UniSync picks
# up the paths from the file, before we wire it into the orchestrator.
#
#   # show what UniSync currently has
#   python3 unisync_prefs.py --show
#
#   # dry-run a change (prints the new values, writes nothing)
#   python3 unisync_prefs.py --territory "United States" \
#       --cache-path "/Volumes/Pegasus32 R8 - 2/UPM-US-Cache/WAV" \
#       --client-path "/Volumes/Pegasus32 R8 - 1/_Specials/UPM/UPM-2026-05-P1/1-ORIGINAL/Music/WAV"
#
#   # actually write it, then QUIT-and-relaunch UniSync and check the fields
#   python3 unisync_prefs.py --apply --territory "United States" \
#       --cache-path "…/WAV" --client-path "…/Music/WAV"
# ---------------------------------------------------------------------------

def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Read/write UniSync territory + cache/client paths in "
                    "UniSync.xml (validate before integrating)."
    )
    ap.add_argument("--xml-path", default=str(DEFAULT_UNISYNC_XML),
                    help=f"Path to UniSync.xml (default: {DEFAULT_UNISYNC_XML})")
    ap.add_argument("--show", action="store_true",
                    help="Print the current prefs and exit.")
    ap.add_argument("--territory", help="Territory string, e.g. 'United States'")
    ap.add_argument("--cache-path", help="UniSync CACHE drive path")
    ap.add_argument("--client-path", help="UniSync CLIENT drive (output) path")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write the file (default is a dry-run).")
    ap.add_argument("--no-backup", action="store_true",
                    help="Do not create a .bak backup.")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("unisync_prefs")

    if args.show or not (args.territory or args.cache_path or args.client_path):
        cur = read_unisync_xml_prefs(args.xml_path)
        if not cur:
            log.error(f"Could not read prefs from {args.xml_path}")
            return 2
        log.info(f"Current UniSync.xml prefs ({args.xml_path}):")
        for k in ("loginname",) + _MANAGED:
            log.info(f"  {k:11s}= {cur.get(k, '(absent)')}")
        return 0

    missing = [n for n, v in (("--territory", args.territory),
                              ("--cache-path", args.cache_path),
                              ("--client-path", args.client_path)) if not v]
    if missing:
        log.error("To write, all three are required: " + ", ".join(missing))
        return 2

    ok = write_unisync_xml_prefs(
        args.territory, args.cache_path, args.client_path,
        xml_path=args.xml_path, logger=log,
        dry_run=not args.apply, backup=not args.no_backup,
    )
    if ok and not args.apply:
        log.info("\n(dry-run — re-run with --apply to write. Then QUIT UniSync, "
                 "relaunch it, and confirm the territory/cache/client fields "
                 "show these values.)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_main())
