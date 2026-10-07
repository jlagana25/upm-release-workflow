#!/usr/bin/env python3
"""Move legacy SoundMouse and HD output into the Pegasus 1 release root."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from config import (
    LEGACY_HD_FINAL_BASE,
    LEGACY_HD_STAGING_BASE,
    LEGACY_SOUNDMOUSE_BASE,
    LOGS_DIR,
    ReleaseContext,
    context_from_cli_args,
)
from logging_utils import get_logger


class ConsolidationError(RuntimeError):
    """A release component cannot be moved without risking data loss."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_manifest(root: Path) -> tuple[tuple[str, str, int, str], ...]:
    """Return deterministic directory/file metadata with full file hashes."""
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ConsolidationError(f"Component root is missing or unsafe: {root}")
    entries: list[tuple[str, str, int, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ConsolidationError(f"Component contains a symlink: {path}")
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append((relative, "dir", 0, ""))
        elif path.is_file():
            entries.append((relative, "file", path.stat().st_size, _sha256(path)))
        else:
            raise ConsolidationError(f"Unsupported filesystem object: {path}")
    return tuple(entries)


def _copy_tree_resumable(
    source: Path,
    staging: Path,
    logger: logging.Logger,
) -> tuple[tuple[str, str, int, str], ...]:
    """Copy once while hashing source bytes, then verify destination bytes."""
    staging.mkdir(parents=True, exist_ok=True)
    manifest: list[tuple[str, str, int, str]] = []
    files: list[Path] = []
    for src in sorted(source.rglob("*")):
        if src.is_symlink():
            raise ConsolidationError(f"Component contains a symlink: {src}")
        relative = src.relative_to(source).as_posix()
        dst = staging / relative
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            manifest.append((relative, "dir", 0, ""))
            continue
        if not src.is_file():
            raise ConsolidationError(f"Unsupported filesystem object: {src}")
        files.append(src)

    def copy_one(src: Path) -> tuple[str, str, int, str]:
        relative = src.relative_to(source).as_posix()
        dst = staging / relative
        before = src.stat()
        size = before.st_size
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_file() and dst.stat().st_size == size:
            digest = _sha256(src)
            if _sha256(dst) != digest:
                raise ConsolidationError(
                    f"Existing staged file differs from source: {relative}"
                )
        else:
            temporary = dst.with_name(f".{dst.name}.consolidating-partial")
            temporary.unlink(missing_ok=True)
            digest_builder = hashlib.sha256()
            try:
                with src.open("rb") as input_handle, temporary.open("wb") as output_handle:
                    for block in iter(
                        lambda: input_handle.read(4 * 1024 * 1024), b""
                    ):
                        digest_builder.update(block)
                        output_handle.write(block)
                shutil.copystat(src, temporary)
                digest = digest_builder.hexdigest()
                after = src.stat()
                if (
                    after.st_size != before.st_size
                    or after.st_mtime_ns != before.st_mtime_ns
                ):
                    raise ConsolidationError(
                        f"Source changed while it was copied: {relative}"
                    )
                if temporary.stat().st_size != size or _sha256(temporary) != digest:
                    raise ConsolidationError(
                        f"Copied file failed verification: {relative}"
                    )
                temporary.replace(dst)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        return (relative, "file", size, digest)

    with ThreadPoolExecutor(max_workers=4) as executor:
        for index, entry in enumerate(executor.map(copy_one, files), start=1):
            manifest.append(entry)
            if index % 250 == 0:
                logger.info("    verified %d/%d files...", index, len(files))
    manifest.sort(key=lambda item: item[0])
    expected = {
        relative for relative, _kind, _size, _digest in manifest
    }
    unexpected = [
        path for path in staging.rglob("*")
        if path.relative_to(staging).as_posix() not in expected
    ]
    if unexpected:
        raise ConsolidationError(
            "Consolidation staging contains unexpected content: "
            + ", ".join(str(path) for path in unexpected[:10])
        )
    result = tuple(manifest)
    logger.info("  ✓ Copied and individually verified %d objects", len(result))
    return result


def _replace_strings(value, replacements: tuple[tuple[str, str], ...]):
    if isinstance(value, str):
        for old, new in replacements:
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_replace_strings(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_strings(item, replacements) for key, item in value.items()}
    return value


def _rewrite_json_roots(roots: tuple[Path, ...], old: Path, new: Path) -> int:
    changed = 0
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.json")):
            try:
                original = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ConsolidationError(f"Invalid workflow JSON {path}: {exc}") from exc
            updated = _replace_strings(original, ((str(old), str(new)),))
            if updated == original:
                continue
            temporary = path.with_name(f".{path.name}.consolidating")
            temporary.write_text(
                json.dumps(updated, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            temporary.replace(path)
            changed += 1
    return changed


def component_operations(ctx: ReleaseContext) -> tuple[tuple[str, Path, Path], ...]:
    return (
        (
            "SoundMouse",
            LEGACY_SOUNDMOUSE_BASE / ctx.soundmouse_activation_range,
            ctx.soundmouse_release_dir,
        ),
        ("HD Staging", LEGACY_HD_STAGING_BASE / ctx.hd_folder, ctx.hd_staging_dir),
        ("HD Final", LEGACY_HD_FINAL_BASE / ctx.hd_folder, ctx.hd_final_dir),
    )


def consolidate_release(
    ctx: ReleaseContext,
    *,
    execute: bool,
    confirmation: str | None,
    logger: logging.Logger,
    logs_dir: Path = LOGS_DIR,
) -> dict[str, str]:
    if execute and confirmation != ctx.release_id:
        raise ConsolidationError(
            f"Execution requires --confirm-release {ctx.release_id}"
        )
    if not ctx.specials_dir.is_dir():
        raise ConsolidationError(f"Main Pegasus 1 release root is missing: {ctx.specials_dir}")
    results: dict[str, str] = {}
    audit_entries: list[dict[str, object]] = []
    report_root = Path(logs_dir) / "reports" / ctx.release_id
    for label, source, target in component_operations(ctx):
        if not source.exists() and target.is_dir():
            rewritten = _rewrite_json_roots(
                (ctx.specials_dir / "_WORKFLOW", report_root), source, target
            )
            logger.info("  ✓ %s already consolidated: %s", label, target)
            results[label] = "already_consolidated"
            audit_entries.append({
                "component": label,
                "source": str(source),
                "target": str(target),
                "objects": None,
                "json_records_rewritten": rewritten,
                "status": "already_consolidated",
            })
            continue
        if not source.is_dir():
            logger.info("  - %s has no legacy component to migrate: %s", label, source)
            results[label] = "not_present"
            continue
        if target.exists() and not target.is_dir():
            raise ConsolidationError(f"Consolidation target is not a directory: {target}")
        logger.info("  %s\n    %s\n    → %s", label, source, target)
        if not execute:
            results[label] = "would_consolidate"
            continue
        if target.is_dir():
            source_manifest = tree_manifest(source)
            if tree_manifest(target) != source_manifest:
                raise ConsolidationError(
                    f"Existing target differs from legacy source for {label}: {target}"
                )
        else:
            staging = target.with_name(f".{target.name}.consolidating")
            source_manifest = _copy_tree_resumable(source, staging, logger)
            target.parent.mkdir(parents=True, exist_ok=True)
            staging.replace(target)
        rewritten = _rewrite_json_roots(
            (ctx.specials_dir / "_WORKFLOW", report_root), source, target
        )
        # Every staged file was hash-verified immediately before the atomic
        # directory rename above. Re-reading hundreds of gigabytes here would
        # not add a distinct safety property and would multiply SMB traffic.
        if not target.is_dir():
            raise ConsolidationError(f"Atomic target rename failed for {label}")
        shutil.rmtree(source)
        if source.exists() or not target.is_dir():
            raise ConsolidationError(f"Could not verify source cleanup for {label}")
        results[label] = "consolidated"
        audit_entries.append({
            "component": label,
            "source": str(source),
            "target": str(target),
            "objects": len(source_manifest),
            "json_records_rewritten": rewritten,
        })

    if execute and audit_entries:
        audit = ctx.specials_dir / "_WORKFLOW" / "storage_consolidation.json"
        audit.parent.mkdir(parents=True, exist_ok=True)
        prior: dict[str, object] = {}
        if audit.is_file():
            try:
                prior = json.loads(audit.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ConsolidationError(f"Invalid prior consolidation audit: {exc}") from exc
        history = list(prior.get("history") or [])
        history.append({
            "at": datetime.now(timezone.utc).isoformat(),
            "release_id": ctx.release_id,
            "components": audit_entries,
        })
        temporary = audit.with_name(f".{audit.name}.tmp")
        temporary.write_text(json.dumps({
            "schema_version": 1,
            "release_id": ctx.release_id,
            "history": history,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(audit)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--part", type=int, choices=(1, 2))
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--full-month-content", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-release")
    args = parser.parse_args()
    ctx = context_from_cli_args(args)
    logger, _ = get_logger(ctx.year, ctx.month, ctx.part, release_label="CONSOLIDATE")
    try:
        consolidate_release(
            ctx,
            execute=args.execute,
            confirmation=args.confirm_release,
            logger=logger,
        )
    except ConsolidationError as exc:
        logger.error("  ✗ Storage consolidation failed closed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
