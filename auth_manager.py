#!/usr/bin/env python3
"""Per-user Domo and UniSync authentication-state management.

Authentication artifacts stay in the current macOS user's home directory.
This module never reads, prints, accepts, or stores a password/token itself.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from config import (
    DOMO_PROFILE_DIR,
    LOGS_DIR,
    PRIVATE_STATE_DIR,
    UNISYNC_PREFS_DIR,
    UNISYNC_XML_PATH,
)

LOCAL_DIAGNOSTICS_DIR = Path(__file__).resolve().parent.parent / "_logs"


@contextmanager
def private_creation_umask():
    """Ensure child processes create user-only auth files from their first byte."""
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def secure_private_directory(path: Path, *, recursive: bool = False) -> None:
    """Create a user-private directory and optionally repair descendants."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    if not recursive:
        return
    for root, directories, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        if not root_path.is_symlink():
            root_path.chmod(0o700)
        for name in directories:
            item = root_path / name
            if not item.is_symlink():
                item.chmod(0o700)
        for name in files:
            item = root_path / name
            if not item.is_symlink():
                item.chmod(0o600)


def secure_private_file(path: Path) -> None:
    """Restrict an existing auth/preferences file to its owner."""
    if path.exists() and not path.is_symlink():
        path.chmod(0o600)


def secure_auth_permissions() -> None:
    secure_private_directory(PRIVATE_STATE_DIR)
    secure_private_directory(DOMO_PROFILE_DIR, recursive=True)
    if UNISYNC_PREFS_DIR.exists() or UNISYNC_XML_PATH.exists():
        secure_private_directory(UNISYNC_PREFS_DIR)
    secure_private_file(UNISYNC_XML_PATH)
    secure_private_file(UNISYNC_XML_PATH.with_suffix(".xml.bak"))
    for logs in (LOGS_DIR, LOCAL_DIAGNOSTICS_DIR):
        if logs.exists():
            secure_private_directory(logs, recursive=True)


def _has_unisync_identity() -> bool:
    try:
        text = UNISYNC_XML_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    import re
    match = re.search(r'\bloginname="([^"]+)"', text)
    return bool(match and match.group(1).strip())


def auth_status() -> dict[str, dict[str, object]]:
    """Return state only; never return usernames, cookie values, or tokens."""
    cookie_candidates = (
        DOMO_PROFILE_DIR / "Default" / "Cookies",
        DOMO_PROFILE_DIR / "Default" / "Network" / "Cookies",
    )
    domo_files = DOMO_PROFILE_DIR.exists() and any(DOMO_PROFILE_DIR.iterdir())
    domo_private = (
        DOMO_PROFILE_DIR.exists()
        and (DOMO_PROFILE_DIR.stat().st_mode & 0o077) == 0
    )
    unisync_private = (
        UNISYNC_XML_PATH.exists()
        and (UNISYNC_XML_PATH.stat().st_mode & 0o077) == 0
    )
    return {
        "domo": {
            "state": "configured" if domo_files else "missing",
            "cookie_store_present": any(path.exists() for path in cookie_candidates),
            "private_permissions": domo_private,
            "location": str(DOMO_PROFILE_DIR),
        },
        "unisync": {
            "state": "configured" if _has_unisync_identity() else "missing",
            "private_permissions": unisync_private,
            "location": str(UNISYNC_XML_PATH),
        },
    }


def setup_domo(logger: logging.Logger) -> bool:
    """Open an isolated persistent browser for the current user's UMG SSO."""
    secure_private_directory(PRIVATE_STATE_DIR)
    secure_private_directory(DOMO_PROFILE_DIR, recursive=True)
    try:
        import domo_exports as domo
        domo._require_playwright()
        with domo.sync_playwright() as playwright:
            with private_creation_umask():
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(DOMO_PROFILE_DIR),
                    headless=False,
                    accept_downloads=False,
                )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                domo._authenticate(page, logger)
            finally:
                context.close()
        secure_private_directory(DOMO_PROFILE_DIR, recursive=True)
        logger.info("Domo authentication configured for this macOS user.")
        return True
    except Exception as exc:
        logger.error("Domo authentication setup failed: %s", exc)
        return False


def setup_unisync(logger: logging.Logger) -> bool:
    """Launch UniSync for the current user to complete its own UMG login."""
    secure_private_directory(UNISYNC_PREFS_DIR)
    app = Path("/Applications/UniSync.app")
    if not app.exists():
        logger.error("UniSync is not installed at %s", app)
        return False
    if _has_unisync_identity():
        secure_private_file(UNISYNC_XML_PATH)
        logger.info("UniSync login is already configured for this macOS user (identity redacted).")
        return True
    result = subprocess.run(["open", "-a", "UniSync"], capture_output=True, text=True)
    if result.returncode:
        logger.error("Could not open UniSync: %s", result.stderr.strip())
        return False
    logger.info(
        "UniSync opened. Sign in with your own UMG account in the app; no "
        "credential is collected by this workflow. Waiting up to five minutes…"
    )
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if _has_unisync_identity():
            secure_private_file(UNISYNC_XML_PATH)
            logger.info("UniSync login detected and local preferences secured (identity redacted).")
            return True
        time.sleep(2)
    logger.error("UniSync login was not detected within five minutes; rerun setup when ready.")
    return False


def _process_running(pattern: str) -> bool:
    result = subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True, text=True
    )
    return result.returncode == 0


def reset_auth(target: str, logger: logging.Logger) -> bool:
    """Move local auth state to Trash so reset is recoverable."""
    if target in {"domo", "all"} and _process_running(str(DOMO_PROFILE_DIR)):
        logger.error("Close the workflow's Domo/Chromium window before resetting Domo.")
        return False
    if target in {"unisync", "all"} and _process_running("/Applications/UniSync"):
        logger.error("Quit UniSync before resetting its local preferences.")
        return False
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = Path.home() / ".Trash" / f"UPM-auth-reset-{stamp}"
    archive.mkdir(parents=True, exist_ok=False, mode=0o700)
    moved = 0
    if target in {"domo", "all"} and DOMO_PROFILE_DIR.exists():
        shutil.move(str(DOMO_PROFILE_DIR), str(archive / "domo_browser_profile"))
        moved += 1
    if target in {"unisync", "all"}:
        for source in (UNISYNC_XML_PATH, UNISYNC_XML_PATH.with_suffix(".xml.bak")):
            if source.exists():
                shutil.move(str(source), str(archive / source.name))
                moved += 1
    secure_private_directory(PRIVATE_STATE_DIR)
    if target in {"domo", "all"}:
        secure_private_directory(DOMO_PROFILE_DIR)
    logger.info("Moved %d local auth artifact(s) to %s", moved, archive)
    logger.info(
        "macOS Keychain entries are intentionally untouched. If UniSync still "
        "signs in automatically, use its own Sign Out command before onboarding another user."
    )
    return True


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage private per-user UPM authentication state")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true", help="Show redacted auth state")
    action.add_argument("--permissions", action="store_true", help="Repair private file permissions")
    action.add_argument("--setup", choices=("domo", "unisync", "all"), help="Configure this user's login")
    action.add_argument("--reset", choices=("domo", "unisync", "all"), help="Move local auth state to Trash")
    parser.add_argument("--confirm-reset", action="store_true", help="Required with --reset")
    parser.add_argument("--json", action="store_true", help="Machine-readable redacted status")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("auth_manager")

    if args.status:
        secure_auth_permissions()
        status = auth_status()
        if args.json:
            print(json.dumps(status, indent=2, sort_keys=True))
        else:
            for name, detail in status.items():
                print(
                    f"{name}: {detail['state']}; "
                    f"private_permissions={detail['private_permissions']}; "
                    f"location={detail['location']}"
                )
        return 0
    if args.permissions:
        secure_auth_permissions()
        logger.info("Private authentication permissions repaired.")
        return 0
    if args.reset:
        if not args.confirm_reset:
            parser.error("--reset requires --confirm-reset; artifacts are moved to Trash")
        return 0 if reset_auth(args.reset, logger) else 1

    ok = True
    if args.setup in {"domo", "all"}:
        ok = setup_domo(logger) and ok
    if args.setup in {"unisync", "all"}:
        ok = setup_unisync(logger) and ok
    secure_auth_permissions()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_main())
