#!/usr/bin/env python3
"""Losslessly archive completed consolidated releases on Pegasus 1."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import tarfile
from datetime import date, datetime, timezone
from pathlib import Path

from config import LOGS_DIR, SPECIALS_BASE


ARCHIVE_DIRNAME = "_ARCHIVE"


class ArchiveError(RuntimeError):
    """A retention or archive verification gate failed."""


def retention_cutoff(as_of: date, months: int = 3) -> date:
    """First retained calendar day, including the current calendar month."""
    if months < 1:
        raise ValueError("retention months must be positive")
    year, month = as_of.year, as_of.month - (months - 1)
    while month <= 0:
        year -= 1
        month += 12
    return date(year, month, 1)


def release_end_from_root(root: Path) -> date:
    """Resolve known consolidated release windows from their physical root."""
    name = root.name
    try:
        start = date.fromisoformat(name.removeprefix("UPM-"))
    except ValueError as exc:
        raise ArchiveError(f"Unrecognized consolidated release root: {root}") from exc
    if start == date(2026, 8, 1):
        return date(2026, 8, 31)
    if start == date(2026, 9, 1):
        return date(2026, 9, 11)
    from datetime import timedelta
    return start + timedelta(days=13)


def _sha256_stream(handle) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def source_manifest(root: Path) -> tuple[dict[str, object], ...]:
    if not root.is_dir() or root.is_symlink():
        raise ArchiveError(f"Release root is missing or unsafe: {root}")
    result: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ArchiveError(f"Release contains a symlink: {path}")
        if path.is_file():
            with path.open("rb") as handle:
                digest = _sha256_stream(handle)
            result.append({
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": digest,
            })
        elif not path.is_dir():
            raise ArchiveError(f"Unsupported release object: {path}")
    if not result:
        raise ArchiveError(f"Release contains no files: {root}")
    return tuple(result)


def _latest_real_report(release_id: str, logs_dir: Path) -> dict | None:
    report_dir = Path(logs_dir) / "reports" / release_id
    for path in sorted(report_dir.glob("run-*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not bool((payload.get("run") or {}).get("dry_run")):
            return payload
    return None


def _release_id_for_root(root: Path) -> str:
    start = date.fromisoformat(root.name.removeprefix("UPM-"))
    return f"UPM{start.strftime('%Y%m%d')}"


def validate_archive_eligibility(
    root: Path,
    *,
    as_of: date,
    retention_months: int,
    logs_dir: Path = LOGS_DIR,
) -> tuple[str, date]:
    end = release_end_from_root(root)
    cutoff = retention_cutoff(as_of, retention_months)
    if end >= cutoff:
        raise ArchiveError(
            f"{root.name} ends {end}; retention keeps releases through {cutoff}"
        )
    release_id = _release_id_for_root(root)
    report = _latest_real_report(release_id, Path(logs_dir))
    if not report or str((report.get("run") or {}).get("overall")) != "completed":
        raise ArchiveError(f"Newest real report is not completed for {release_id}")
    state_path = root / "_WORKFLOW" / "delivery_status.json"
    if state_path.is_file():
        try:
            partners = json.loads(state_path.read_text(encoding="utf-8")).get("partners") or {}
        except (OSError, json.JSONDecodeError) as exc:
            raise ArchiveError(f"Invalid delivery state: {state_path}: {exc}") from exc
        unfinished = sorted(
            key for key, value in partners.items()
            if str((value or {}).get("status") or "pending") != "delivered"
        )
        if unfinished:
            raise ArchiveError("Delivery state is unfinished: " + ", ".join(unfinished))
    return release_id, end


def discover_release_roots(base: Path = SPECIALS_BASE) -> tuple[Path, ...]:
    return tuple(sorted(
        path for path in Path(base).glob("UPM-????-??-??")
        if path.is_dir() and not path.is_symlink()
    ))


def archive_release(
    root: Path,
    *,
    as_of: date,
    retention_months: int = 3,
    execute: bool,
    confirmation: str | None,
    logs_dir: Path = LOGS_DIR,
    logger: logging.Logger,
) -> Path:
    root = Path(root)
    release_id, end = validate_archive_eligibility(
        root,
        as_of=as_of,
        retention_months=retention_months,
        logs_dir=logs_dir,
    )
    archive_dir = root.parent / ARCHIVE_DIRNAME
    archive = archive_dir / f"{root.name}.tar.zst"
    record = archive_dir / f"{root.name}.json"
    logger.info("  Eligible: %s (ends %s) → %s", root, end, archive)
    if not execute:
        return archive
    if confirmation != release_id:
        raise ArchiveError(f"Execution requires --confirm-release {release_id}")
    if archive.exists() or record.exists():
        raise ArchiveError(f"Archive target already exists for {root.name}")
    manifest = source_manifest(root)
    archive_dir.mkdir(parents=True, exist_ok=True)
    partial = archive.with_name(f".{archive.name}.partial")
    partial.unlink(missing_ok=True)
    try:
        with tarfile.open(partial, mode="w:zst") as bundle:
            bundle.add(root, arcname=root.name, recursive=True)
        with partial.open("rb") as handle:
            archive_sha = _sha256_stream(handle)
        expected = {str(item["path"]): item for item in manifest}
        observed: dict[str, tuple[int, str]] = {}
        with tarfile.open(partial, mode="r:zst") as bundle:
            prefix = f"{root.name}/"
            for member in bundle:
                if not member.isfile():
                    continue
                if not member.name.startswith(prefix):
                    raise ArchiveError(f"Archive member escaped release root: {member.name}")
                relative = member.name[len(prefix):]
                extracted = bundle.extractfile(member)
                if extracted is None:
                    raise ArchiveError(f"Could not read archive member: {member.name}")
                observed[relative] = (member.size, _sha256_stream(extracted))
        expected_values = {
            path: (int(item["size"]), str(item["sha256"]))
            for path, item in expected.items()
        }
        if observed != expected_values:
            raise ArchiveError("Archive content manifest did not match the release")
        partial.replace(archive)
        payload = {
            "schema_version": 1,
            "release_id": release_id,
            "release_root": root.name,
            "release_end": end.isoformat(),
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "retention_months": retention_months,
            "archive": str(archive),
            "archive_size": archive.stat().st_size,
            "archive_sha256": archive_sha,
            "files": manifest,
        }
        temporary = record.with_name(f".{record.name}.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(record)
        shutil.rmtree(root)
        if root.exists() or not archive.is_file() or not record.is_file():
            raise ArchiveError(f"Archive cleanup verification failed for {root}")
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    logger.info("  ✓ Archived and removed original release root: %s", root.name)
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--retention-months", type=int, default=3)
    parser.add_argument("--release-root")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-release")
    args = parser.parse_args()
    as_of = date.fromisoformat(args.as_of)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("release_archiver")
    roots = (
        (Path(args.release_root),)
        if args.release_root
        else discover_release_roots()
    )
    failed = False
    for root in roots:
        try:
            archive_release(
                root,
                as_of=as_of,
                retention_months=args.retention_months,
                execute=args.execute,
                confirmation=args.confirm_release,
                logger=logger,
            )
        except ArchiveError as exc:
            logger.info("  - Not archived: %s", exc)
            if args.release_root:
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
