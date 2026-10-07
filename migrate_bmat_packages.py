"""Adopt workflow-owned BMAT packages into their release's Final Packaging."""

import argparse
import logging
from pathlib import Path

from bmat_delivery import load_ledger, save_ledger
from config import BMAT_BASE, ReleaseContext


def migrate(ctx, *, execute=False, root=BMAT_BASE):
    ledger_path = root / "_WORKFLOW" / "delivery_ledger.json"
    ledger = load_ledger(ledger_path)
    for delivery in ledger["deliveries"]:
        if delivery.get("workflow_id") != ctx.release_id:
            continue
        batch = delivery["batch_id"]
        if Path(batch).name != batch:
            raise ValueError("Unsafe BMAT batch ID")
        source = Path(delivery.get("package_dir") or root / batch)
        target = ctx.bmat_final_dir / batch
        if source == target:
            if not target.is_dir():
                raise ValueError(f"Recorded BMAT package is missing: {target}")
            continue
        logging.info("%s → %s", source, target)
        if target.exists() and source.exists():
            raise ValueError(f"BMAT target already exists: {target}")
        if not source.is_dir() and not target.is_dir():
            raise ValueError(f"BMAT package is missing: {source}")
        if not execute:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            if source.is_symlink() or source.stat().st_dev != target.parent.stat().st_dev:
                raise ValueError("BMAT migration requires a regular same-volume directory")
            source.rename(target)
        delivery["package_dir"] = str(target)
        save_ledger(ledger_path, ledger)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    migrate(ReleaseContext.for_date_range(args.start_date, args.end_date), execute=args.execute)
