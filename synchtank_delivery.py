#!/usr/bin/env python3
"""Upload the verified SynchTank package to its batch-scoped S3 prefix.

The bucket retains historical deliveries. Each package is uploaded beneath a
prefix matching its local package-folder name, checked by exact key and byte
size within that prefix, and the empty ``delivery.complete`` trigger is written
inside that prefix last. AWS credentials are supplied from the current user's
macOS Keychain and are never logged or persisted here.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from auth_manager import load_synchtank_s3_credentials
from config import LOGS_DIR, ReleaseContext, context_from_cli_args
from delivery_state import set_partner_status


SYNCHTANK_BUCKET = "synchtank-delivery-upm"
COMPLETION_MARKER = "delivery.complete"
IGNORED_NAMES = frozenset({".DS_Store", COMPLETION_MARKER})


class SynchTankDeliveryError(RuntimeError):
    """A validation or remote-delivery failure that must stop ingestion."""


@dataclass(frozen=True)
class PackageObject:
    key: str
    path: Path
    size: int


class SynchTankGateway(Protocol):
    def list_objects(self) -> dict[str, int]: ...
    def upload(self, local_path: Path, key: str) -> None: ...
    def object_size(self, key: str) -> int | None: ...
    def put_empty(self, key: str) -> None: ...


class Boto3SynchTankGateway:
    """Minimal S3 adapter; imports boto3 lazily for headless smoke tests."""

    def __init__(self, access_key_id: str, secret_access_key: str) -> None:
        try:
            import boto3
            from botocore.exceptions import ClientError
        except ImportError as exc:
            raise SynchTankDeliveryError(
                "SynchTank upload requires boto3; install requirements.txt"
            ) from exc
        self._client = boto3.client(
            "s3",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )
        self._client_error = ClientError

    def list_objects(self) -> dict[str, int]:
        found: dict[str, int] = {}
        continuation: str | None = None
        while True:
            request: dict[str, object] = {"Bucket": SYNCHTANK_BUCKET}
            if continuation:
                request["ContinuationToken"] = continuation
            response = self._client.list_objects_v2(**request)
            for item in response.get("Contents", []):
                found[str(item["Key"])] = int(item["Size"])
            if not response.get("IsTruncated"):
                return found
            continuation = response.get("NextContinuationToken")
            if not continuation:
                raise SynchTankDeliveryError(
                    "SynchTank returned a truncated object list without a continuation token"
                )

    def upload(self, local_path: Path, key: str) -> None:
        self._client.upload_file(str(local_path), SYNCHTANK_BUCKET, key)

    def object_size(self, key: str) -> int | None:
        try:
            response = self._client.head_object(Bucket=SYNCHTANK_BUCKET, Key=key)
        except self._client_error as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return int(response["ContentLength"])

    def put_empty(self, key: str) -> None:
        self._client.put_object(Bucket=SYNCHTANK_BUCKET, Key=key, Body=b"")


def package_root(ctx: ReleaseContext) -> Path:
    return (
        ctx.specials_dir
        / "3-FINAL PACKAGING"
        / ctx.partner_folder_name("SynchTank")
    )


def package_prefix(ctx: ReleaseContext) -> str:
    """Return the exact batch folder prefix used in the shared S3 bucket."""
    return package_root(ctx).name.rstrip("/") + "/"


def collect_package(root: Path) -> tuple[PackageObject, ...]:
    """Return a deterministic root-relative manifest and reject unsafe input."""
    root = Path(root)
    if not root.is_dir():
        raise SynchTankDeliveryError(f"SynchTank package is missing: {root}")
    objects: list[PackageObject] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SynchTankDeliveryError(f"SynchTank package contains a symlink: {path}")
        if not path.is_file() or path.name in IGNORED_NAMES:
            continue
        size = path.stat().st_size
        if size <= 0:
            raise SynchTankDeliveryError(f"SynchTank package contains an empty file: {path}")
        key = path.relative_to(root).as_posix()
        if key.startswith("/") or ".." in Path(key).parts:
            raise SynchTankDeliveryError(f"Unsafe SynchTank object key: {key}")
        objects.append(PackageObject(key=key, path=path, size=size))
    if not objects:
        raise SynchTankDeliveryError(f"SynchTank package contains no deliverable files: {root}")
    return tuple(objects)


def _receipt_path(ctx: ReleaseContext) -> Path:
    return ctx.specials_dir / "_WORKFLOW" / "synchtank_delivery_receipt.json"


def workflow_gate_passed(ctx: ReleaseContext) -> tuple[bool, str]:
    """Require the newest real outcomes for packaging and Step 15 to pass."""
    required = {"10 Final packaging", "15 Final metadata check"}
    statuses: dict[str, str] = {}
    report_dir = Path(LOGS_DIR) / "reports" / ctx.release_id
    if report_dir.is_dir():
        for path in sorted(report_dir.glob("run-*.json"), reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if bool((payload.get("run") or {}).get("dry_run")):
                continue
            steps = payload.get("steps") or {}
            for key in required - statuses.keys():
                status = str((steps.get(key) or {}).get("status") or "")
                if status and status != "skipped":
                    statuses[key] = status
            if required <= statuses.keys():
                break
    missing = sorted(required - statuses.keys())
    if missing:
        return False, "no real-run gate result for " + ", ".join(missing)
    failed = sorted(key for key, value in statuses.items() if value != "completed")
    if failed:
        return False, "newest gate result is not completed for " + ", ".join(failed)
    return True, "Step 10 and Step 15 completed"


def _write_receipt(
    ctx: ReleaseContext,
    objects: tuple[PackageObject, ...],
    prefix: str,
) -> Path:
    path = _receipt_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "schema_version": 1,
        "partner": "synchtank",
        "release_id": ctx.release_id,
        "bucket": SYNCHTANK_BUCKET,
        "remote_prefix": prefix,
        "completion_marker": prefix + COMPLETION_MARKER,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "objects": [{"key": item.key, "size": item.size} for item in objects],
    }
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def deliver_synchtank(
    ctx: ReleaseContext,
    dry_run: bool,
    logger: logging.Logger,
    *,
    gateway: SynchTankGateway | None = None,
) -> bool:
    """Deliver one batch prefix and write its trigger last."""
    try:
        objects = collect_package(package_root(ctx))
        prefix = package_prefix(ctx)
        marker_key = prefix + COMPLETION_MARKER
        expected = {prefix + item.key: item.size for item in objects}
        logger.info(
            "  SynchTank package: %d object(s), %d byte(s)",
            len(objects), sum(item.size for item in objects),
        )
        if dry_run:
            logger.info(
                "  [DRY RUN] Would upload package to s3://%s/%s",
                SYNCHTANK_BUCKET, prefix,
            )
            logger.info("  [DRY RUN] Would upload %s last", marker_key)
            return True

        gate_ok, gate_detail = workflow_gate_passed(ctx)
        if not gate_ok:
            raise SynchTankDeliveryError(
                f"SynchTank upload is blocked: {gate_detail}"
            )
        logger.info("  ✓ Workflow delivery gate: %s", gate_detail)

        credentials = None
        if gateway is None:
            credentials = load_synchtank_s3_credentials()
            if credentials is None:
                raise SynchTankDeliveryError(
                    "SynchTank S3 credentials are missing from this user's Keychain"
                )
            gateway = Boto3SynchTankGateway(*credentials)

        all_remote = gateway.list_objects()
        remote = {key: size for key, size in all_remote.items() if key.startswith(prefix)}
        if marker_key in remote:
            if remote[marker_key] != 0:
                raise SynchTankDeliveryError("Existing delivery.complete is not empty")
            actual = {key: size for key, size in remote.items() if key != marker_key}
            if actual != expected:
                raise SynchTankDeliveryError(
                    "SynchTank completion marker exists but the remote manifest does not "
                    "exactly match this package"
                )
            logger.info("  ✓ SynchTank package was already delivered exactly; no upload needed")
        else:
            unexpected = sorted(set(remote) - set(expected))
            if unexpected:
                raise SynchTankDeliveryError(
                    "SynchTank bucket contains unexpected object(s): "
                    + ", ".join(unexpected[:10])
                )
            for item in objects:
                remote_key = prefix + item.key
                if remote.get(remote_key) == item.size:
                    logger.info("  ↩ Already verified: %s", remote_key)
                    continue
                logger.info("  Uploading: %s", remote_key)
                gateway.upload(item.path, remote_key)
                if gateway.object_size(remote_key) != item.size:
                    raise SynchTankDeliveryError(
                        f"Remote size verification failed for {remote_key}"
                    )
            final = {
                key: size
                for key, size in gateway.list_objects().items()
                if key.startswith(prefix)
            }
            if final != expected:
                raise SynchTankDeliveryError(
                    "SynchTank remote manifest did not exactly match before completion"
                )
            gateway.put_empty(marker_key)
            if gateway.object_size(marker_key) != 0:
                raise SynchTankDeliveryError("Could not verify delivery.complete")
            logger.info("  ✓ SynchTank delivery.complete uploaded last")

        receipt = _write_receipt(ctx, objects, prefix)
        set_partner_status(ctx.specials_dir, "synchtank", "delivered")
        logger.info("  ✓ SynchTank receipt: %s", receipt)
        credentials = None
        return True
    except Exception as exc:  # fail closed at the public boundary
        logger.error("  ✗ SynchTank delivery failed: %s", exc)
        return False


def _main() -> int:
    parser = argparse.ArgumentParser(description="Deliver the SynchTank package to S3")
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--part", type=int, choices=(1, 2))
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--full-month-content", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ok = deliver_synchtank(
        context_from_cli_args(args), args.dry_run, logging.getLogger("synchtank")
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
