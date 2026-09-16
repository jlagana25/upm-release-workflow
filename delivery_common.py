"""Shared safety primitives for post-packaging delivery endpoints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from config import LOGS_DIR, ReleaseContext


IGNORED_NAMES = frozenset({".DS_Store", "delivery.complete"})


class DeliverySafetyError(RuntimeError):
    """A local manifest, gate, authorization, or resume invariant failed."""


@dataclass(frozen=True)
class ManifestFile:
    path: Path
    relative: str
    size: int
    sha256: str

    def public_dict(self) -> dict[str, object]:
        return {"path": self.relative, "size": self.size, "sha256": self.sha256}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def collect_manifest(
    root: Path,
    *,
    include_root: bool = False,
    allowed_suffixes: frozenset[str] | None = None,
) -> tuple[ManifestFile, ...]:
    """Return a deterministic content manifest and reject unsafe local input."""
    root = Path(root)
    if not root.exists():
        raise DeliverySafetyError(f"Delivery input is missing: {root}")
    if root.is_symlink():
        raise DeliverySafetyError(f"Delivery input is a symlink: {root}")
    candidates = (root,) if root.is_file() else tuple(sorted(root.rglob("*")))
    files: list[ManifestFile] = []
    for path in candidates:
        if path.is_symlink():
            raise DeliverySafetyError(f"Delivery input contains a symlink: {path}")
        if not path.is_file() or path.name in IGNORED_NAMES:
            continue
        suffix = path.suffix.casefold()
        if allowed_suffixes is not None and suffix not in allowed_suffixes:
            raise DeliverySafetyError(f"Unexpected delivery file type {suffix or '<none>'}: {path}")
        size = path.stat().st_size
        if size <= 0:
            raise DeliverySafetyError(f"Delivery input contains an empty file: {path}")
        relative = path.name if root.is_file() else path.relative_to(root).as_posix()
        if include_root and root.is_dir():
            relative = f"{root.name}/{relative}"
        files.append(ManifestFile(path.resolve(), relative, size, _sha256(path)))
    if not files:
        raise DeliverySafetyError(f"Delivery input contains no files: {root}")
    return tuple(files)


def manifest_digest(files: Iterable[ManifestFile]) -> str:
    payload = [item.public_dict() for item in files]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def latest_workflow_gates(
    ctx: ReleaseContext,
    required: Iterable[str],
    *,
    logs_dir: Path | None = None,
) -> tuple[bool, str]:
    """Require the newest real non-skipped result for every named step."""
    wanted = set(required)
    statuses: dict[str, str] = {}
    report_dir = Path(logs_dir or LOGS_DIR) / "reports" / ctx.release_id
    if report_dir.is_dir():
        for path in sorted(report_dir.glob("run-*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if bool((data.get("run") or {}).get("dry_run")):
                continue
            steps = data.get("steps") or {}
            for name in wanted - statuses.keys():
                status = str((steps.get(name) or {}).get("status") or "")
                if status and status != "skipped":
                    statuses[name] = status
            if wanted <= statuses.keys():
                break
    missing = sorted(wanted - statuses.keys())
    if missing:
        return False, "no real-run result for " + ", ".join(missing)
    failed = sorted(name for name, status in statuses.items() if status != "completed")
    if failed:
        return False, "newest result is not completed for " + ", ".join(failed)
    return True, "completed: " + ", ".join(sorted(wanted))


def require_live_authorization(ctx: ReleaseContext, confirmation: str | None) -> None:
    if confirmation != ctx.release_id:
        raise DeliverySafetyError(
            f"Live delivery requires exact authorization for {ctx.release_id}"
        )


def private_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def endpoint_state_path(ctx: ReleaseContext, endpoint: str) -> Path:
    return ctx.specials_dir / "_WORKFLOW" / "endpoint_runs" / f"{endpoint}.json"


def load_endpoint_state(ctx: ReleaseContext, endpoint: str) -> dict | None:
    path = endpoint_state_path(ctx, endpoint)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeliverySafetyError(f"Invalid endpoint state {path}: {exc}") from exc
    if data.get("release_id") != ctx.release_id or data.get("endpoint") != endpoint:
        raise DeliverySafetyError(f"Endpoint state is bound to another delivery: {path}")
    return data


def checkpoint(
    ctx: ReleaseContext,
    endpoint: str,
    files: Iterable[ManifestFile],
    phase: str,
    detail: str,
    *,
    remote_ids: Iterable[str] = (),
) -> Path:
    """Write a manifest-bound restart checkpoint without storing secrets."""
    manifest = tuple(files)
    digest = manifest_digest(manifest)
    prior = load_endpoint_state(ctx, endpoint)
    if prior and prior.get("manifest_digest") != digest:
        raise DeliverySafetyError(
            f"{endpoint} package changed after delivery began; operator review is required"
        )
    now = datetime.now(timezone.utc).isoformat()
    history = list((prior or {}).get("history") or [])
    history.append({"phase": phase, "at": now, "detail": detail})
    return private_json(endpoint_state_path(ctx, endpoint), {
        "schema_version": 1,
        "release_id": ctx.release_id,
        "endpoint": endpoint,
        "phase": phase,
        "manifest_digest": digest,
        "manifest": [item.public_dict() for item in manifest],
        "remote_ids": sorted({str(item) for item in remote_ids if str(item)}),
        "created_at": (prior or {}).get("created_at", now),
        "updated_at": now,
        "history": history,
    })


def receipt(
    ctx: ReleaseContext,
    endpoint: str,
    files: Iterable[ManifestFile],
    details: dict[str, object],
) -> Path:
    manifest = tuple(files)
    return private_json(ctx.specials_dir / "_WORKFLOW" / f"{endpoint}_delivery_receipt.json", {
        "schema_version": 1,
        "release_id": ctx.release_id,
        "endpoint": endpoint,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "manifest_digest": manifest_digest(manifest),
        "manifest": [item.public_dict() for item in manifest],
        "details": details,
    })
