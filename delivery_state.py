"""Explicit per-release partner delivery state.

A populated delivery folder does not reveal whether it has already been sent.
Refresh behavior therefore uses this small release-local state file instead of
guessing: pending partners are reconciled in place; delivered partners receive
separate correction packages.
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
    "synchtank",
    "tunesat",
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
    entry = _load(release_root)["partners"].get(partner.casefold(), {})
    return entry.get("status") == "delivered"


def set_partner_status(release_root: Path, partner: str, delivered: bool) -> Path:
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
    data["partners"][key] = {
        "status": "delivered" if delivered else "pending",
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
        description="Mark release partners pending or delivered for refresh routing."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--mark-delivered", metavar="PARTNERS")
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
    raw = args.mark_delivered or args.mark_pending
    delivered = bool(args.mark_delivered)
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
            path = set_partner_status(ctx.specials_dir, partner, delivered)
            print(f"{partner}: {'delivered' if delivered else 'pending'} → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
