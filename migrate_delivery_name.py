"""Safely relabel an existing completed delivery without changing its content ID.

This is intentionally separate from the normal workflow: source-date roots,
tracklists, reports, and SoundMouse ActivationRange folders remain untouched.
Only client-facing package folders and display-name files are renamed.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from config import HD_FINAL_BASE, HD_STAGING_BASE, SPECIALS_BASE, is_retired_partner_name


def build_operations(release_id: str, old_label: str, new_label: str) -> list[tuple[Path, Path]]:
    specials = SPECIALS_BASE / release_id
    final = specials / "3-FINAL PACKAGING"
    staging = HD_STAGING_BASE / release_id
    hd_final = HD_FINAL_BASE / release_id
    operations: list[tuple[Path, Path]] = []

    # Display-name files are shallow by design; avoid walking the many-terabyte
    # MEDIA trees just to rename a handful of metadata and album-list files.
    candidates: list[Path] = []
    candidates.extend(final.glob("*/Metadata/*"))
    candidates.extend(final.glob("*/*"))
    candidates.extend(staging.glob("*"))
    candidates.extend(hd_final.glob("*/*"))
    candidates.extend(hd_final.glob("*/*/*"))
    for path in candidates:
        if any(is_retired_partner_name(parent.name) for parent in path.parents):
            continue
        if old_label in path.name:
            new_name = path.name.replace(old_label, new_label)
            new_name = new_name.replace(f"{new_label} Release (SM)", f"{new_label} (SM)")
            new_name = new_name.replace(f"{new_label} Release (SW)", f"{new_label} (SW)")
            operations.append((path, path.with_name(new_name)))

    for path in final.glob("*"):
        if not path.is_dir():
            continue
        name = path.name
        if is_retired_partner_name(name):
            archive = specials / "2-STAGING" / "_RECOVERY_ARCHIVE"
            operations.append((path, archive / f"{release_id}-retired-{name.rsplit(' - ', 1)[-1]}"))
        elif name.startswith(f"Universal Production Music {old_label} Release - "):
            partner = name.split(" - ", 1)[1]
            operations.append((path, final / f"Universal Production Music {new_label} - {partner}"))
        elif name == f"UPM Japan NTT DATA {old_label} Release":
            operations.append((path, final / f"Universal Production Music {new_label} - Japan NTT DATA"))
        elif name == f"UPM Japan JMD and TSS {old_label} Release":
            operations.append((path, final / f"Universal Production Music {new_label} - Japan JMD and TSS"))

    # De-duplicate paths found by overlapping shallow globs; children must move
    # before their parent directories.
    unique = {(src, dst) for src, dst in operations if src != dst}
    return sorted(unique, key=lambda pair: len(pair[0].parts), reverse=True)


def migrate(release_id: str, old_label: str, new_label: str, apply: bool) -> int:
    operations = build_operations(release_id, old_label, new_label)
    if not operations:
        print("No matching client-facing artifacts found.")
        return 0

    collisions = [dst for src, dst in operations if dst.exists() and dst != src]
    if collisions:
        raise FileExistsError("refusing to overwrite existing target(s):\n" + "\n".join(map(str, collisions)))

    for src, dst in operations:
        print(f"{'RENAME' if apply else 'WOULD RENAME'}\n  {src}\n  -> {dst}")
    if not apply:
        print(f"Dry run only: {len(operations)} rename(s). Pass --apply to execute.")
        return 0

    completed: list[dict[str, str]] = []
    try:
        for src, dst in operations:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            completed.append({"from": str(src), "to": str(dst)})
    except Exception:
        # Best-effort rollback in reverse order keeps this migration recoverable.
        for item in reversed(completed):
            current, original = Path(item["to"]), Path(item["from"])
            if current.exists() and not original.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                current.rename(original)
        raise

    archive = SPECIALS_BASE / release_id / "2-STAGING" / "_RECOVERY_ARCHIVE"
    archive.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest = archive / f"{release_id}-delivery-rename-{stamp}.json"
    manifest.write_text(json.dumps({
        "release_id": release_id,
        "old_label": old_label,
        "new_label": new_label,
        "renames": completed,
    }, indent=2) + "\n", encoding="utf-8")
    print(f"Completed {len(completed)} rename(s). Rollback manifest: {manifest}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--from-label", required=True)
    parser.add_argument("--to-label", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    return migrate(args.release_id, args.from_label, args.to_label, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
