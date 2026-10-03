#!/usr/bin/env python3
"""Recover known workflow volumes after a normal restart or update.

The pipeline Mac (HDF2) reaches HDF1's Pegasus disks through authenticated
Finder SMB mounts.  HDF1 sees those disks locally through Disk Arbitration.
This module handles both cases without storing credentials or accepting an
arbitrary share/device supplied at runtime.
"""

from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote

from config import (
    PIPELINE_HOSTNAME,
    PRIVATE_STATE_DIR,
    REMOTE_SOUNDMINER,
    SOUNDMINER_HOSTNAME,
    current_hostname,
)


DISABLE_SENTINEL = PRIVATE_STATE_DIR / "disable_auto_mount"
DISABLE_ENV = "UPM_DISABLE_AUTO_MOUNT"


@dataclass(frozen=True)
class VolumeSpec:
    key: str
    name: str
    path: Path
    required: bool
    hosts: frozenset[str]
    smb_share: str | None = None


WORKFLOW_VOLUMES: tuple[VolumeSpec, ...] = (
    VolumeSpec(
        "R8_1", "Pegasus32 R8 - 1", Path("/Volumes/Pegasus32 R8 - 1"), True,
        frozenset({PIPELINE_HOSTNAME, SOUNDMINER_HOSTNAME}), "Pegasus32 R8 - 1",
    ),
    VolumeSpec(
        "R8_2", "Pegasus32 R8 - 2", Path("/Volumes/Pegasus32 R8 - 2"), True,
        frozenset({PIPELINE_HOSTNAME, SOUNDMINER_HOSTNAME}), "Pegasus32 R8 - 2",
    ),
    # Documents is a compatibility/convenience share.  Workflow code uses the
    # stable local ~/Documents path and never depends on /Volumes/Documents.
    VolumeSpec(
        "DOCUMENTS", "Documents", Path("/Volumes/Documents"), False,
        frozenset({PIPELINE_HOSTNAME}), "Documents",
    ),
    # UPM Builds is physically attached to HDF1.  Its legacy missing-cover CSV
    # is not a current correctness gate, so failure to recover it only warns.
    VolumeSpec(
        "UPM_BUILDS", "UPM Builds", Path("/Volumes/UPM Builds"), False,
        frozenset({SOUNDMINER_HOSTNAME}), None,
    ),
)


def auto_mount_disabled(*, explicit: bool = False) -> bool:
    value = os.environ.get(DISABLE_ENV, "").strip().casefold()
    return explicit or value in {"1", "true", "yes", "on"} or DISABLE_SENTINEL.exists()


def _is_ready(path: Path) -> bool:
    return os.path.ismount(path) and os.access(path, os.R_OK | os.W_OK)


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def diskutil_devices(payload: bytes) -> dict[str, str]:
    """Map exact volume names to device identifiers from ``diskutil -plist``."""
    data = plistlib.loads(payload)
    devices: dict[str, str] = {}
    duplicates: set[str] = set()
    for item in _walk_dicts(data):
        name = str(item.get("VolumeName") or "")
        device = str(item.get("DeviceIdentifier") or "")
        if not name or not device:
            continue
        if name in devices and devices[name] != device:
            duplicates.add(name)
        else:
            devices[name] = device
    for name in duplicates:
        devices.pop(name, None)
    return devices


def _mount_local(spec: VolumeSpec) -> tuple[bool, str]:
    try:
        listed = subprocess.run(
            ["/usr/sbin/diskutil", "list", "-plist"],
            check=True, capture_output=True, timeout=20,
        )
        device = diskutil_devices(listed.stdout).get(spec.name)
        if not device:
            return False, "attached volume was not uniquely discoverable"
        mounted = subprocess.run(
            ["/usr/sbin/diskutil", "mount", device],
            capture_output=True, text=True, timeout=45,
        )
        detail = (mounted.stdout or mounted.stderr).strip()
        return mounted.returncode == 0, detail or f"diskutil exited {mounted.returncode}"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, str(exc)


def _mount_network(spec: VolumeSpec) -> tuple[bool, str]:
    if not spec.smb_share:
        return False, "no SMB share is configured"
    host = REMOTE_SOUNDMINER["host"]
    user = REMOTE_SOUNDMINER["user"]
    url = f"smb://{quote(user, safe='')}@{host}/{quote(spec.smb_share, safe='')}"
    try:
        result = subprocess.run(
            ["/usr/bin/open", "-g", url],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    detail = (result.stdout or result.stderr).strip()
    return result.returncode == 0, detail or f"open exited {result.returncode}"


def ensure_workflow_volumes(
    logger: logging.Logger,
    *,
    skip_auto_mount: bool = False,
    timeout_seconds: float = 45,
    specs: Iterable[VolumeSpec] | None = None,
    hostname: str | None = None,
    ready: Callable[[Path], bool] = _is_ready,
    network_mounter: Callable[[VolumeSpec], tuple[bool, str]] = _mount_network,
    local_mounter: Callable[[VolumeSpec], tuple[bool, str]] = _mount_local,
) -> bool:
    """Mount known missing volumes and return whether all required ones are ready.

    Intentional unmounts are respected by ``--no-auto-mount``, the
    ``UPM_DISABLE_AUTO_MOUNT`` environment variable, or the private
    ``~/.upm_release_workflow/disable_auto_mount`` sentinel.
    """
    host = (hostname or current_hostname()).upper()
    selected = tuple(spec for spec in (specs or WORKFLOW_VOLUMES) if host in spec.hosts)
    disabled = auto_mount_disabled(explicit=skip_auto_mount)
    all_required = True

    for spec in selected:
        if ready(spec.path):
            logger.info("  ✓  Volume %s ready: %s", spec.key, spec.path)
            continue
        if disabled:
            detail = "automatic mounting disabled"
        else:
            logger.warning("  ↻  Volume %s is missing; attempting recovery", spec.key)
            if host == PIPELINE_HOSTNAME and spec.smb_share:
                started, detail = network_mounter(spec)
            else:
                started, detail = local_mounter(spec)
            if started:
                deadline = time.monotonic() + max(timeout_seconds, 0)
                while time.monotonic() <= deadline:
                    if ready(spec.path):
                        logger.info("  ✓  Volume %s remounted: %s", spec.key, spec.path)
                        break
                    time.sleep(0.5)
                else:
                    detail = "mount command completed but the exact path did not become ready"
            if ready(spec.path):
                continue

        log = logger.error if spec.required else logger.warning
        log("  %s  Volume %s unavailable: %s (%s)", "✗" if spec.required else "⚠", spec.key, spec.path, detail)
        if spec.required:
            all_required = False
    return all_required
