"""Explicit per-release partner delivery state.

A populated delivery folder does not reveal whether it is still being built,
has been uploaded into a partner system, or has been officially delivered.
Refresh behavior therefore uses this small release-local state file instead of
guessing.  ``uploaded`` is the correction-package boundary for partner systems
whose ingest maps metadata to media (SourceAudio, Netmix, and SoundMouse).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from config import ReleaseContext, context_from_cli_args


STATE_DIRNAME = "_WORKFLOW"
STATE_FILENAME = "delivery_status.json"
KNOWN_PARTNERS = frozenset({
    "discovery",
    "espn",
    "hd_updates",
    "japan_ntt",
    "nbc",
    "netmix",
    "sourceaudio",
    "sourceaudio_exus",
    "soundmouse",
    "synchtank",
    "tunesat",
})
VALID_STATUSES = frozenset({"pending", "uploaded", "delivered"})
CORRECTION_PACKAGE_PARTNERS = frozenset({
    "netmix",
    "soundmouse",
    "sourceaudio",
    "sourceaudio_exus",
})


def state_path(release_root: Path) -> Path:
    return Path(release_root) / STATE_DIRNAME / STATE_FILENAME


def _load(release_root: Path) -> dict:
    path = state_path(release_root)
    if not path.is_file():
        return {"partners": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid delivery-state file {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("partners", {}), dict):
        raise ValueError(f"Invalid delivery-state structure: {path}")
    data.setdefault("partners", {})
    return data


def partner_is_delivered(release_root: Path, partner: str) -> bool:
    return partner_status(release_root, partner) == "delivered"


def partner_status(release_root: Path, partner: str) -> str:
    entry = _load(release_root)["partners"].get(partner.strip().casefold(), {})
    status = str(entry.get("status", "pending")).casefold()
    return status if status in VALID_STATUSES else "pending"


def partner_needs_correction_package(release_root: Path, partner: str) -> bool:
    """True once a correction-package partner has crossed its upload boundary.

    ``delivered`` also qualifies because an officially delivered package has,
    by definition, already been uploaded.  Other partner types never use a
    Missing package; their ``delivered`` state simply protects them from a
    later in-place refresh.
    """
    key = partner.strip().casefold()
    return (
        key in CORRECTION_PACKAGE_PARTNERS
        and partner_status(release_root, key) in {"uploaded", "delivered"}
    )


def set_partner_status(
    release_root: Path,
    partner: str,
    status: str | bool,
) -> Path:
    root = Path(release_root)
    data = _load(root)
    key = partner.strip().casefold()
    if not key:
        raise ValueError("Partner name cannot be empty")
    if key not in KNOWN_PARTNERS:
        raise ValueError(
            f"Unknown partner {partner!r}; expected one of: "
            + ", ".join(sorted(KNOWN_PARTNERS))
        )
    # Preserve compatibility with the original bool API used by older scripts.
    normalized = (
        ("delivered" if status else "pending")
        if isinstance(status, bool)
        else str(status).strip().casefold()
    )
    if normalized not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status {status!r}; expected one of: "
            + ", ".join(sorted(VALID_STATUSES))
        )
    data["partners"][key] = {
        "status": normalized,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)
    return path


def _main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Mark release partners pending, uploaded, or delivered for refresh routing."
        )
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--mark-delivered", metavar="PARTNERS")
    action.add_argument("--mark-uploaded", metavar="PARTNERS")
    action.add_argument("--mark-pending", metavar="PARTNERS")
    action.add_argument("--show", action="store_true")
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--part", type=int, choices=(1, 2))
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--full-month-content", action="store_true")
    args = parser.parse_args()
    ctx = context_from_cli_args(args)
    if args.show:
        data = _load(ctx.specials_dir)["partners"]
        for partner in sorted(KNOWN_PARTNERS):
            status = data.get(partner, {}).get("status", "pending")
            print(f"{partner}: {status}")
        return 0
    raw = args.mark_delivered or args.mark_uploaded or args.mark_pending
    status = (
        "delivered" if args.mark_delivered
        else "uploaded" if args.mark_uploaded
        else "pending"
    )
    requested = [item.strip().casefold() for item in raw.split(",") if item.strip()]
    if "all" in requested:
        if len(requested) != 1:
            parser.error("use 'all' by itself")
        requested = sorted(KNOWN_PARTNERS)
    unknown = sorted(set(requested) - KNOWN_PARTNERS)
    if unknown:
        parser.error(
            "unknown partner(s): " + ", ".join(unknown)
            + "; expected: " + ", ".join(sorted(KNOWN_PARTNERS))
        )
    for partner in requested:
        if partner:
            path = set_partner_status(ctx.specials_dir, partner, status)
            print(f"{partner}: {status} → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
