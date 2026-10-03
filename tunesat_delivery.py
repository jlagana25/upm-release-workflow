#!/usr/bin/env python3
"""Upload the complete verified TuneSat package to its SFTP inbox."""

from __future__ import annotations

import argparse
import json
import logging
import os
import posixpath
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Protocol

from auth_manager import load_tunesat_sftp_credentials
from config import PRIVATE_STATE_DIR, ReleaseContext, context_from_cli_args
from delivery_state import set_partner_status
from synchtank_delivery import workflow_gate_passed


TUNESAT_HOST = "upload.tunesat.com"
TUNESAT_PORT = 22
REMOTE_ROOT = "/AudioFiles"
IGNORED_NAMES = frozenset({".DS_Store", "delivery.complete"})


class TuneSatDeliveryError(RuntimeError):
    """A validation or remote-delivery failure that must stop the upload."""


@dataclass(frozen=True)
class PackageFile:
    relative: str
    path: Path
    size: int


class TuneSatGateway(Protocol):
    def list_files(self, prefix: str = "") -> dict[str, int]: ...
    def upload(self, local_path: Path, relative: str) -> None: ...
    def file_size(self, relative: str) -> int | None: ...
    def close(self) -> None: ...


def package_root(ctx: ReleaseContext) -> Path:
    return (
        ctx.specials_dir
        / "3-FINAL PACKAGING"
        / ctx.partner_folder_name("Tunesat")
    )


def package_prefix(ctx: ReleaseContext) -> str:
    """Return the exact package-folder path beneath /AudioFiles."""
    return package_root(ctx).name.rstrip("/") + "/"


def collect_package(root: Path) -> tuple[PackageFile, ...]:
    root = Path(root)
    if not root.is_dir():
        raise TuneSatDeliveryError(f"TuneSat package is missing: {root}")
    files: list[PackageFile] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise TuneSatDeliveryError(f"TuneSat package contains a symlink: {path}")
        if not path.is_file() or path.name in IGNORED_NAMES:
            continue
        size = path.stat().st_size
        if size <= 0:
            raise TuneSatDeliveryError(f"TuneSat package contains an empty file: {path}")
        relative = path.relative_to(root).as_posix()
        if relative.startswith("/") or ".." in PurePosixPath(relative).parts:
            raise TuneSatDeliveryError(f"Unsafe TuneSat remote path: {relative}")
        files.append(PackageFile(relative=relative, path=path, size=size))
    if not files:
        raise TuneSatDeliveryError(f"TuneSat package contains no deliverable files: {root}")
    top_levels = {PurePosixPath(item.relative).parts[0] for item in files}
    if not {"Music", "Metadata"} <= top_levels:
        raise TuneSatDeliveryError(
            "TuneSat package must contain nonempty Music and Metadata directories"
        )
    return tuple(files)


def _mkdir_remote(sftp, remote_path: str) -> None:
    current = "/" if remote_path.startswith("/") else ""
    for part in PurePosixPath(remote_path).parts:
        if part == "/":
            continue
        current = posixpath.join(current, part)
        try:
            attrs = sftp.stat(current)
            if not stat.S_ISDIR(attrs.st_mode):
                raise TuneSatDeliveryError(f"Remote path is not a directory: {current}")
        except FileNotFoundError:
            sftp.mkdir(current)


class ParamikoTuneSatGateway:
    """SFTP adapter with persistent first-seen host-key pinning."""

    def __init__(
        self,
        username: str,
        password: str,
        *,
        known_hosts: Path | None = None,
    ) -> None:
        try:
            import paramiko
        except ImportError as exc:
            raise TuneSatDeliveryError(
                "TuneSat upload requires Paramiko; install requirements.txt"
            ) from exc
        host_keys_path = known_hosts or (PRIVATE_STATE_DIR / "tunesat_known_hosts")
        host_keys_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._client = paramiko.SSHClient()
        self._sftp = None
        if host_keys_path.exists():
            self._client.load_host_keys(str(host_keys_path))
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self._client.connect(
                TUNESAT_HOST,
                port=TUNESAT_PORT,
                username=username,
                password=password,
                look_for_keys=False,
                allow_agent=False,
                timeout=30,
            )
            self._client.save_host_keys(str(host_keys_path))
            os.chmod(host_keys_path, 0o600)
            self._sftp = self._client.open_sftp()
            attrs = self._sftp.stat(REMOTE_ROOT)
            if not stat.S_ISDIR(attrs.st_mode):
                raise TuneSatDeliveryError(
                    f"TuneSat remote root is not a directory: {REMOTE_ROOT}"
                )
        except Exception:
            self.close()
            raise

    def _remote(self, relative: str) -> str:
        return posixpath.join(REMOTE_ROOT, relative)

    def list_files(self, prefix: str = "") -> dict[str, int]:
        found: dict[str, int] = {}

        def visit(remote_dir: str, prefix: str = "") -> None:
            for attrs in self._sftp.listdir_attr(remote_dir):
                relative = posixpath.join(prefix, attrs.filename) if prefix else attrs.filename
                remote = posixpath.join(remote_dir, attrs.filename)
                if stat.S_ISDIR(attrs.st_mode):
                    visit(remote, relative)
                elif stat.S_ISREG(attrs.st_mode):
                    found[relative] = int(attrs.st_size)
                else:
                    raise TuneSatDeliveryError(f"Unsupported remote item: {relative}")

        clean_prefix = prefix.strip("/")
        start = self._remote(clean_prefix) if clean_prefix else REMOTE_ROOT
        try:
            visit(start, clean_prefix)
        except FileNotFoundError:
            return {}
        return found

    def upload(self, local_path: Path, relative: str) -> None:
        remote = self._remote(relative)
        _mkdir_remote(self._sftp, posixpath.dirname(remote))
        temporary = remote + ".part"
        self._sftp.put(str(local_path), temporary)
        if self._sftp.stat(temporary).st_size != local_path.stat().st_size:
            raise TuneSatDeliveryError(f"Temporary remote size mismatch: {relative}")
        try:
            self._sftp.remove(remote)
        except FileNotFoundError:
            pass
        self._sftp.rename(temporary, remote)

    def file_size(self, relative: str) -> int | None:
        try:
            return int(self._sftp.stat(self._remote(relative)).st_size)
        except FileNotFoundError:
            return None

    def close(self) -> None:
        sftp = getattr(self, "_sftp", None)
        if sftp is not None:
            sftp.close()
            self._sftp = None
        self._client.close()


def _receipt_path(ctx: ReleaseContext) -> Path:
    return ctx.specials_dir / "_WORKFLOW" / "tunesat_delivery_receipt.json"


def _write_receipt(
    ctx: ReleaseContext,
    files: tuple[PackageFile, ...],
    prefix: str,
) -> Path:
    path = _receipt_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "schema_version": 1,
        "partner": "tunesat",
        "release_id": ctx.release_id,
        "host": TUNESAT_HOST,
        "remote_root": REMOTE_ROOT,
        "remote_prefix": prefix,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "files": [{"path": item.relative, "size": item.size} for item in files],
    }
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def deliver_tunesat(
    ctx: ReleaseContext,
    dry_run: bool,
    logger: logging.Logger,
    *,
    gateway: TuneSatGateway | None = None,
) -> bool:
    """Upload one complete package folder beneath /AudioFiles."""
    owned_gateway = False
    credentials = None
    try:
        files = collect_package(package_root(ctx))
        prefix = package_prefix(ctx)
        expected = {prefix + item.relative: item.size for item in files}
        logger.info(
            "  TuneSat package: %d file(s), %d byte(s)",
            len(files), sum(item.size for item in files),
        )
        if dry_run:
            logger.info(
                "  [DRY RUN] Would upload package beneath %s:%s/%s",
                TUNESAT_HOST, REMOTE_ROOT, prefix,
            )
            return True

        gate_ok, gate_detail = workflow_gate_passed(ctx)
        if not gate_ok:
            raise TuneSatDeliveryError(f"TuneSat upload is blocked: {gate_detail}")
        logger.info("  ✓ Workflow delivery gate: %s", gate_detail)

        if gateway is None:
            credentials = load_tunesat_sftp_credentials()
            if credentials is None:
                raise TuneSatDeliveryError(
                    "TuneSat SFTP credentials are missing from this user's Keychain"
                )
            gateway = ParamikoTuneSatGateway(*credentials)
            owned_gateway = True

        remote = gateway.list_files(prefix)
        recoverable_parts = {key + ".part" for key in expected}
        unexpected = sorted(set(remote) - set(expected) - recoverable_parts)
        if unexpected:
            raise TuneSatDeliveryError(
                "TuneSat /AudioFiles contains unexpected file(s): "
                + ", ".join(unexpected[:10])
            )
        for item in files:
            remote_path = prefix + item.relative
            if remote.get(remote_path) == item.size:
                logger.info("  ↩ Already verified: %s", remote_path)
                continue
            logger.info("  Uploading: %s", remote_path)
            gateway.upload(item.path, remote_path)
            if gateway.file_size(remote_path) != item.size:
                raise TuneSatDeliveryError(
                    f"Remote size verification failed for {remote_path}"
                )
        final = {
            key: size
            for key, size in gateway.list_files(prefix).items()
            if key.startswith(prefix)
        }
        if final != expected:
            raise TuneSatDeliveryError("TuneSat remote manifest did not exactly match")

        receipt = _write_receipt(ctx, files, prefix)
        set_partner_status(ctx.specials_dir, "tunesat", "delivered")
        logger.info("  ✓ TuneSat package uploaded and verified: %s", receipt)
        return True
    except Exception as exc:
        logger.error("  ✗ TuneSat delivery failed: %s", exc)
        return False
    finally:
        credentials = None
        if owned_gateway and gateway is not None:
            gateway.close()


def _main() -> int:
    parser = argparse.ArgumentParser(description="Deliver the complete TuneSat package by SFTP")
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
    ok = deliver_tunesat(
        context_from_cli_args(args), args.dry_run, logging.getLogger("tunesat")
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
