#!/usr/bin/env python3
"""Per-user Domo, UniSync, and Monday authentication-state management.

Authentication artifacts stay in the current macOS user's home directory.
Secrets are read into memory only when a workflow client needs them and are
never printed, logged, placed in argv, or stored in repository files.
"""

from __future__ import annotations

import argparse
import ctypes
import getpass
import hmac
import json
import logging
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from config import (
    DAMS_PROFILE_DIR,
    DOMO_PROFILE_DIR,
    LOGS_DIR,
    PRIVATE_STATE_DIR,
    UNISYNC_PREFS_DIR,
    UNISYNC_XML_PATH,
)

LOCAL_DIAGNOSTICS_DIR = Path(__file__).resolve().parent.parent / "_logs"
DOMO_KEYCHAIN_ACCOUNT = "current-macos-user"
DOMO_KEYCHAIN_USERNAME_SERVICE = "com.upm-release-workflow.domo.username"
DOMO_KEYCHAIN_PASSWORD_SERVICE = "com.upm-release-workflow.domo.password"
DOMO_API_CLIENT_ID_SERVICE = "com.upm-release-workflow.domo.api-client-id"
DOMO_API_CLIENT_SECRET_SERVICE = "com.upm-release-workflow.domo.api-client-secret"
MONDAY_KEYCHAIN_TOKEN_SERVICE = "com.upm-release-workflow.monday.api-token"
BMAT_SFTP_USERNAME_SERVICE = "com.upm-release-workflow.bmat-sftp.username"
BMAT_SFTP_PASSWORD_SERVICE = "com.upm-release-workflow.bmat-sftp.password"
SYNCHTANK_S3_ACCESS_KEY_SERVICE = "com.upm-release-workflow.synchtank-s3.access-key-id"
SYNCHTANK_S3_SECRET_KEY_SERVICE = "com.upm-release-workflow.synchtank-s3.secret-access-key"
TUNESAT_SFTP_USERNAME_SERVICE = "com.upm-release-workflow.tunesat-sftp.username"
TUNESAT_SFTP_PASSWORD_SERVICE = "com.upm-release-workflow.tunesat-sftp.password"
ESPN_USERNAME_SERVICE = "com.upm-release-workflow.espn.username"
ESPN_PASSWORD_SERVICE = "com.upm-release-workflow.espn.password"
NETMIX_USERNAME_SERVICE = "com.upm-release-workflow.netmix.username"
NETMIX_PASSWORD_SERVICE = "com.upm-release-workflow.netmix.password"
SOUNDEXCHANGE_USERNAME_SERVICE = "com.upm-release-workflow.soundexchange.username"
SOUNDEXCHANGE_PASSWORD_SERVICE = "com.upm-release-workflow.soundexchange.password"
SOUNDMOUSE_USERNAME_SERVICE = "com.upm-release-workflow.soundmouse.username"
SOUNDMOUSE_PASSWORD_SERVICE = "com.upm-release-workflow.soundmouse.password"
DOMO_API_TOKEN_URL = "https://api.domo.com/oauth/token"
_ERR_SEC_ITEM_NOT_FOUND = -25300
_SECURITY_FRAMEWORK_PATH = (
    "/System/Library/Frameworks/Security.framework/Security"
)
_COREFOUNDATION_FRAMEWORK_PATH = (
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)


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
    secure_private_directory(DAMS_PROFILE_DIR, recursive=True)
    if UNISYNC_PREFS_DIR.exists() or UNISYNC_XML_PATH.exists():
        secure_private_directory(UNISYNC_PREFS_DIR)
    secure_private_file(UNISYNC_XML_PATH)
    secure_private_file(UNISYNC_XML_PATH.with_suffix(".xml.bak"))
    for logs in (LOGS_DIR, LOCAL_DIAGNOSTICS_DIR):
        if logs.exists():
            secure_private_directory(logs, recursive=True)


def unisync_auth_configured() -> bool:
    """Return only whether UniSync has per-user login configuration."""
    try:
        text = UNISYNC_XML_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    import re
    match = re.search(r'\bloginname="([^"]+)"', text)
    return bool(match and match.group(1).strip())


def domo_keychain_configured() -> bool:
    """Confirm both workflow Keychain items contain readable nonempty values."""
    return load_domo_keychain_credentials() is not None


def _read_keychain_secret(service: str) -> str | None:
    result = subprocess.run(
        [
            "/usr/bin/security", "find-generic-password",
            "-a", DOMO_KEYCHAIN_ACCOUNT, "-s", service, "-w",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return None
    value = result.stdout.rstrip("\r\n")
    return value or None


def load_domo_keychain_credentials() -> tuple[str, str] | None:
    """Load Domo credentials into memory only; callers must never log them."""
    username = _read_keychain_secret(DOMO_KEYCHAIN_USERNAME_SERVICE)
    password = _read_keychain_secret(DOMO_KEYCHAIN_PASSWORD_SERVICE)
    if not username or not password:
        return None
    return username, password


def load_domo_api_keychain_credentials() -> tuple[str, str] | None:
    """Load the Domo API client pair into memory without logging either value."""
    client_id = _read_native_keychain_secret(DOMO_API_CLIENT_ID_SERVICE)
    client_secret = _read_native_keychain_secret(DOMO_API_CLIENT_SECRET_SERVICE)
    if not client_id or not client_secret:
        return None
    return client_id, client_secret


def domo_api_keychain_configured() -> bool:
    """Confirm that both workflow-owned Domo API Keychain items are readable."""
    return load_domo_api_keychain_credentials() is not None


def load_monday_keychain_token() -> str | None:
    """Load the current user's Monday API token into memory only."""
    value = _read_native_keychain_secret(MONDAY_KEYCHAIN_TOKEN_SERVICE)
    return value.strip() if value and value.strip() else None


def load_bmat_sftp_credentials() -> tuple[str, str] | None:
    """Load the BMAT SFTP pair into memory without logging either value."""
    username = _read_native_keychain_secret(BMAT_SFTP_USERNAME_SERVICE)
    password = _read_native_keychain_secret(BMAT_SFTP_PASSWORD_SERVICE)
    if not username or not password:
        return None
    return username, password


def bmat_sftp_keychain_configured() -> bool:
    """Confirm that both BMAT SFTP Keychain items are readable."""
    return load_bmat_sftp_credentials() is not None


def load_synchtank_s3_credentials() -> tuple[str, str] | None:
    """Load the SynchTank AWS pair into memory without logging either value."""
    access_key = _read_native_keychain_secret(SYNCHTANK_S3_ACCESS_KEY_SERVICE)
    secret_key = _read_native_keychain_secret(SYNCHTANK_S3_SECRET_KEY_SERVICE)
    if not access_key or not secret_key:
        return None
    return access_key, secret_key


def synchtank_s3_keychain_configured() -> bool:
    return load_synchtank_s3_credentials() is not None


def load_tunesat_sftp_credentials() -> tuple[str, str] | None:
    """Load the TuneSat SFTP pair into memory without logging either value."""
    username = _read_native_keychain_secret(TUNESAT_SFTP_USERNAME_SERVICE)
    password = _read_native_keychain_secret(TUNESAT_SFTP_PASSWORD_SERVICE)
    if not username or not password:
        return None
    return username, password


def tunesat_sftp_keychain_configured() -> bool:
    return load_tunesat_sftp_credentials() is not None


def _load_web_credentials(
    username_service: str, password_service: str,
) -> tuple[str, str] | None:
    username = _read_native_keychain_secret(username_service)
    password = _read_native_keychain_secret(password_service)
    if not username or not password:
        return None
    return username, password


def load_espn_credentials() -> tuple[str, str] | None:
    """Load the ESPN portal pair into memory without logging either value."""
    return _load_web_credentials(ESPN_USERNAME_SERVICE, ESPN_PASSWORD_SERVICE)


def espn_keychain_configured() -> bool:
    return load_espn_credentials() is not None


def load_netmix_credentials() -> tuple[str, str] | None:
    """Load the Netmix history-portal pair without logging either value."""
    return _load_web_credentials(NETMIX_USERNAME_SERVICE, NETMIX_PASSWORD_SERVICE)


def netmix_keychain_configured() -> bool:
    return load_netmix_credentials() is not None


def load_soundexchange_credentials() -> tuple[str, str] | None:
    """Load the SoundExchange portal pair without logging either value."""
    return _load_web_credentials(
        SOUNDEXCHANGE_USERNAME_SERVICE, SOUNDEXCHANGE_PASSWORD_SERVICE
    )


def soundexchange_keychain_configured() -> bool:
    return load_soundexchange_credentials() is not None


def load_soundmouse_credentials() -> tuple[str, str] | None:
    """Load the SoundMouse website pair without logging either value."""
    return _load_web_credentials(
        SOUNDMOUSE_USERNAME_SERVICE, SOUNDMOUSE_PASSWORD_SERVICE
    )


def soundmouse_keychain_configured() -> bool:
    return load_soundmouse_credentials() is not None


def _keychain_frameworks():
    """Load the macOS Keychain C API lazily so imports stay cross-platform."""
    security = ctypes.CDLL(_SECURITY_FRAMEWORK_PATH)
    core_foundation = ctypes.CDLL(_COREFOUNDATION_FRAMEWORK_PATH)
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    security.SecKeychainAddGenericPassword.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    security.SecKeychainItemModifyAttributesAndData.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
    ]
    security.SecKeychainItemFreeContent.restype = ctypes.c_int32
    security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    security.SecKeychainItemDelete.restype = ctypes.c_int32
    security.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    return security, core_foundation


def _keychain_identity(service: str) -> tuple[bytes, bytes]:
    return service.encode("utf-8"), DOMO_KEYCHAIN_ACCOUNT.encode("utf-8")


def _read_native_keychain_secret(service: str) -> str | None:
    """Read a Python-owned generic password through macOS Security.framework."""
    try:
        security, core_foundation = _keychain_frameworks()
    except OSError:
        return None
    service_bytes, account_bytes = _keychain_identity(service)
    password_length = ctypes.c_uint32()
    password_data = ctypes.c_void_p()
    item_ref = ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None,
        len(service_bytes), service_bytes,
        len(account_bytes), account_bytes,
        ctypes.byref(password_length), ctypes.byref(password_data),
        ctypes.byref(item_ref),
    )
    if status != 0:
        return None
    try:
        raw = ctypes.string_at(password_data.value, password_length.value)
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    finally:
        security.SecKeychainItemFreeContent(None, password_data)
        if item_ref.value:
            core_foundation.CFRelease(item_ref)


def _store_native_keychain_secret(service: str, secret: str) -> bool:
    """Add/update a Python-owned generic password via Security.framework."""
    try:
        security, core_foundation = _keychain_frameworks()
    except OSError:
        return False
    service_bytes, account_bytes = _keychain_identity(service)
    secret_bytes = secret.encode("utf-8")
    secret_buffer = ctypes.create_string_buffer(secret_bytes)
    item_ref = ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None,
        len(service_bytes), service_bytes,
        len(account_bytes), account_bytes,
        None, None, ctypes.byref(item_ref),
    )
    try:
        if status == 0:
            status = security.SecKeychainItemModifyAttributesAndData(
                item_ref, None, len(secret_bytes), secret_buffer,
            )
        elif status == _ERR_SEC_ITEM_NOT_FOUND:
            status = security.SecKeychainAddGenericPassword(
                None,
                len(service_bytes), service_bytes,
                len(account_bytes), account_bytes,
                len(secret_bytes), secret_buffer,
                ctypes.byref(item_ref),
            )
        else:
            return False
    finally:
        if item_ref.value:
            core_foundation.CFRelease(item_ref)
    stored = _read_native_keychain_secret(service)
    return status == 0 and bool(stored and hmac.compare_digest(stored, secret))


def _delete_native_keychain_secret(service: str) -> bool:
    """Delete a Python-owned generic password through Security.framework."""
    try:
        security, core_foundation = _keychain_frameworks()
    except OSError:
        return False
    service_bytes, account_bytes = _keychain_identity(service)
    item_ref = ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None,
        len(service_bytes), service_bytes,
        len(account_bytes), account_bytes,
        None, None, ctypes.byref(item_ref),
    )
    if status == _ERR_SEC_ITEM_NOT_FOUND:
        return True
    if status != 0:
        return False
    try:
        return security.SecKeychainItemDelete(item_ref) == 0
    finally:
        if item_ref.value:
            core_foundation.CFRelease(item_ref)


def _prompt_hidden_secret(description: str) -> str | None:
    """Collect one hidden value without involving Keychain's paste handling."""
    value = getpass.getpass(f"{description} (hidden; paste once): ").strip()
    return value or None


def _store_keychain_secret(service: str, secret: str) -> bool:
    """Store an in-memory secret without placing it in argv or command output."""
    # Apple's `security -w` deliberately reopens /dev/tty, so a normal stdin
    # pipe is ignored and its two prompts leak back to the user's Terminal. Run
    # it on a private pseudo-terminal instead and answer both prompts with the
    # one value Python already collected. Nothing enters argv, the environment,
    # logs, or a filesystem artifact.
    argv = [
        "/usr/bin/security", "add-generic-password", "-U",
        "-a", DOMO_KEYCHAIN_ACCOUNT, "-s", service,
        "-T", "/usr/bin/security", "-w",
    ]
    pid, master_fd = pty.fork()
    if pid == 0:  # pragma: no cover - replaces this process in the child
        os.execv(argv[0], argv)

    secret_line = secret.encode("utf-8") + b"\n"
    prompt_buffer = b""
    prompt_count = 0
    deadline = time.monotonic() + 30
    status: int | None = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 0.25)
            if not ready:
                waited_pid, status = os.waitpid(pid, os.WNOHANG)
                if waited_pid:
                    break
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            # Keep only enough redacted child output to recognize the fixed
            # prompts. It is never printed or returned.
            prompt_buffer = (prompt_buffer + chunk)[-4096:]
            expected = (
                b"password data for new item:"
                if prompt_count == 0
                else b"retype password for new item:"
            )
            if prompt_count < 2 and expected in prompt_buffer:
                os.write(master_fd, secret_line)
                prompt_count += 1
                prompt_buffer = b""
        if status is None:
            waited_pid, status = os.waitpid(pid, os.WNOHANG)
            if not waited_pid:
                os.kill(pid, signal.SIGTERM)
                _, status = os.waitpid(pid, 0)
    finally:
        os.close(master_fd)

    if prompt_count != 2 or os.waitstatus_to_exitcode(status) != 0:
        return False
    stored = _read_keychain_secret(service)
    return bool(stored and hmac.compare_digest(stored, secret))


def _prompt_and_store_keychain_secret(service: str, description: str) -> bool:
    """Collect a value once, then store and verify its exact Keychain value."""
    print(f"\nKeychain enrollment for {description}.", flush=True)
    secret = _prompt_hidden_secret(description)
    return bool(secret and _store_keychain_secret(service, secret))


def enroll_domo_keychain(logger: logging.Logger) -> bool:
    """Have macOS Keychain collect credentials directly through hidden prompts."""
    if not sys.stdin.isatty():
        logger.error("Domo Keychain enrollment must be run in an interactive Terminal.")
        return False
    username_ok = bool(_read_keychain_secret(DOMO_KEYCHAIN_USERNAME_SERVICE))
    if username_ok:
        logger.info("Existing Domo Keychain email is readable; keeping it (value redacted).")
    else:
        username_ok = _prompt_and_store_keychain_secret(
            DOMO_KEYCHAIN_USERNAME_SERVICE, "UMG email"
        )
    # Always refresh the password when enrollment is explicitly requested.
    password_ok = username_ok and _prompt_and_store_keychain_secret(
        DOMO_KEYCHAIN_PASSWORD_SERVICE, "UMG password"
    )
    if not (username_ok and password_ok and domo_keychain_configured()):
        logger.error(
            "Could not store readable nonempty Domo credentials in macOS Keychain."
        )
        return False
    logger.info("Domo unattended credentials stored in macOS Keychain (values redacted).")
    return True


def enroll_bmat_sftp_keychain(logger: logging.Logger) -> bool:
    """Atomically store the BMAT SFTP pair without exposing either value."""
    if not sys.stdin.isatty():
        logger.error("BMAT SFTP enrollment must be run in an interactive Terminal.")
        return False
    print("\nBMAT SFTP Keychain enrollment.", flush=True)
    username = _prompt_hidden_secret("BMAT SFTP username")
    password = _prompt_hidden_secret("BMAT SFTP password")
    if not username or not password:
        logger.error("Both BMAT SFTP values are required; Keychain was not changed.")
        return False
    previous_username = _read_native_keychain_secret(BMAT_SFTP_USERNAME_SERVICE)
    previous_password = _read_native_keychain_secret(BMAT_SFTP_PASSWORD_SERVICE)
    username_ok = _store_native_keychain_secret(BMAT_SFTP_USERNAME_SERVICE, username)
    password_ok = username_ok and _store_native_keychain_secret(
        BMAT_SFTP_PASSWORD_SERVICE, password
    )
    stored = load_bmat_sftp_credentials()
    exact = bool(
        stored
        and hmac.compare_digest(stored[0], username)
        and hmac.compare_digest(stored[1], password)
    )
    username = password = ""
    stored = None
    if not (username_ok and password_ok and exact):
        _restore_native_keychain_secret(
            BMAT_SFTP_USERNAME_SERVICE, previous_username
        )
        _restore_native_keychain_secret(
            BMAT_SFTP_PASSWORD_SERVICE, previous_password
        )
        logger.error("Could not atomically store the BMAT SFTP Keychain pair.")
        return False
    logger.info("BMAT SFTP credentials stored in macOS Keychain (values redacted).")
    return True


def enroll_synchtank_s3_keychain(logger: logging.Logger) -> bool:
    """Atomically store the SynchTank AWS pair without exposing either value."""
    if not sys.stdin.isatty():
        logger.error("SynchTank S3 enrollment must be run in an interactive Terminal.")
        return False
    print("\nSynchTank S3 Keychain enrollment.", flush=True)
    access_key = _prompt_hidden_secret("SynchTank AWS access-key ID")
    secret_key = _prompt_hidden_secret("SynchTank AWS secret access key")
    if not access_key or not secret_key:
        logger.error("Both SynchTank AWS values are required; Keychain was not changed.")
        return False
    try:
        import boto3
        client = boto3.client(
            "s3",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        client.list_objects_v2(Bucket="synchtank-delivery-upm", MaxKeys=1)
    except ImportError:
        access_key = secret_key = ""
        logger.error("SynchTank credential validation requires boto3.")
        return False
    except Exception:
        access_key = secret_key = ""
        logger.error(
            "SynchTank rejected the AWS credentials or bucket access; "
            "Keychain was not changed."
        )
        return False
    previous_access = _read_native_keychain_secret(SYNCHTANK_S3_ACCESS_KEY_SERVICE)
    previous_secret = _read_native_keychain_secret(SYNCHTANK_S3_SECRET_KEY_SERVICE)
    access_ok = _store_native_keychain_secret(
        SYNCHTANK_S3_ACCESS_KEY_SERVICE, access_key
    )
    secret_ok = access_ok and _store_native_keychain_secret(
        SYNCHTANK_S3_SECRET_KEY_SERVICE, secret_key
    )
    stored = load_synchtank_s3_credentials()
    exact = bool(
        stored
        and hmac.compare_digest(stored[0], access_key)
        and hmac.compare_digest(stored[1], secret_key)
    )
    access_key = secret_key = ""
    stored = None
    if not (access_ok and secret_ok and exact):
        _restore_native_keychain_secret(
            SYNCHTANK_S3_ACCESS_KEY_SERVICE, previous_access
        )
        _restore_native_keychain_secret(
            SYNCHTANK_S3_SECRET_KEY_SERVICE, previous_secret
        )
        logger.error("Could not atomically store the SynchTank AWS pair.")
        return False
    logger.info("SynchTank S3 credentials stored in macOS Keychain (values redacted).")
    return True


def enroll_tunesat_sftp_keychain(logger: logging.Logger) -> bool:
    """Atomically store the TuneSat SFTP pair without exposing either value."""
    if not sys.stdin.isatty():
        logger.error("TuneSat SFTP enrollment must be run in an interactive Terminal.")
        return False
    print("\nTuneSat SFTP Keychain enrollment.", flush=True)
    username = _prompt_hidden_secret("TuneSat SFTP username")
    password = _prompt_hidden_secret("TuneSat SFTP password")
    if not username or not password:
        logger.error("Both TuneSat SFTP values are required; Keychain was not changed.")
        return False
    previous_username = _read_native_keychain_secret(TUNESAT_SFTP_USERNAME_SERVICE)
    previous_password = _read_native_keychain_secret(TUNESAT_SFTP_PASSWORD_SERVICE)
    username_ok = _store_native_keychain_secret(TUNESAT_SFTP_USERNAME_SERVICE, username)
    password_ok = username_ok and _store_native_keychain_secret(
        TUNESAT_SFTP_PASSWORD_SERVICE, password
    )
    stored = load_tunesat_sftp_credentials()
    exact = bool(
        stored
        and hmac.compare_digest(stored[0], username)
        and hmac.compare_digest(stored[1], password)
    )
    username = password = ""
    stored = None
    if not (username_ok and password_ok and exact):
        _restore_native_keychain_secret(
            TUNESAT_SFTP_USERNAME_SERVICE, previous_username
        )
        _restore_native_keychain_secret(
            TUNESAT_SFTP_PASSWORD_SERVICE, previous_password
        )
        logger.error("Could not atomically store the TuneSat SFTP pair.")
        return False
    logger.info("TuneSat SFTP credentials stored in macOS Keychain (values redacted).")
    return True


def _enroll_web_keychain_pair(
    logger: logging.Logger,
    *,
    label: str,
    username_service: str,
    password_service: str,
    loader,
) -> bool:
    """Atomically store one portal login pair after hidden TTY collection."""
    if not sys.stdin.isatty():
        logger.error("%s enrollment must be run in an interactive Terminal.", label)
        return False
    print(f"\n{label} Keychain enrollment.", flush=True)
    username = _prompt_hidden_secret(f"{label} username or email")
    password = _prompt_hidden_secret(f"{label} password")
    if not username or not password:
        logger.error("Both %s values are required; Keychain was not changed.", label)
        return False
    previous_username = _read_native_keychain_secret(username_service)
    previous_password = _read_native_keychain_secret(password_service)
    username_ok = _store_native_keychain_secret(username_service, username)
    password_ok = username_ok and _store_native_keychain_secret(
        password_service, password
    )
    stored = loader()
    exact = bool(
        stored
        and hmac.compare_digest(stored[0], username)
        and hmac.compare_digest(stored[1], password)
    )
    username = password = ""
    stored = None
    if not (username_ok and password_ok and exact):
        _restore_native_keychain_secret(username_service, previous_username)
        _restore_native_keychain_secret(password_service, previous_password)
        logger.error("Could not atomically store the %s Keychain pair.", label)
        return False
    logger.info("%s credentials stored in macOS Keychain (values redacted).", label)
    return True


def enroll_espn_keychain(logger: logging.Logger) -> bool:
    return _enroll_web_keychain_pair(
        logger,
        label="ESPN Media Shuttle",
        username_service=ESPN_USERNAME_SERVICE,
        password_service=ESPN_PASSWORD_SERVICE,
        loader=load_espn_credentials,
    )


def enroll_netmix_keychain(logger: logging.Logger) -> bool:
    return _enroll_web_keychain_pair(
        logger,
        label="Netmix upload history",
        username_service=NETMIX_USERNAME_SERVICE,
        password_service=NETMIX_PASSWORD_SERVICE,
        loader=load_netmix_credentials,
    )


def enroll_soundexchange_keychain(logger: logging.Logger) -> bool:
    return _enroll_web_keychain_pair(
        logger,
        label="SoundExchange Direct",
        username_service=SOUNDEXCHANGE_USERNAME_SERVICE,
        password_service=SOUNDEXCHANGE_PASSWORD_SERVICE,
        loader=load_soundexchange_credentials,
    )


def enroll_soundmouse_keychain(logger: logging.Logger) -> bool:
    return _enroll_web_keychain_pair(
        logger,
        label="SoundMouse website",
        username_service=SOUNDMOUSE_USERNAME_SERVICE,
        password_service=SOUNDMOUSE_PASSWORD_SERVICE,
        loader=load_soundmouse_credentials,
    )


def _validate_domo_api_credentials(
    client_id: str,
    client_secret: str,
) -> tuple[bool, str]:
    """Validate a Domo API client pair without persisting or exposing a token."""
    import requests

    try:
        response = requests.get(
            DOMO_API_TOKEN_URL,
            params={"grant_type": "client_credentials", "scope": "data"},
            auth=(client_id, client_secret),
            headers={"Accept": "application/json"},
            timeout=30,
        )
    except requests.RequestException:
        return False, "unavailable"
    if response.status_code != 200:
        return False, f"http-{response.status_code}"
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return False, "invalid-response"
    if not isinstance(payload, dict) or not payload.get("access_token"):
        return False, "invalid-response"
    return True, "accepted"


def _restore_native_keychain_secret(service: str, previous: str | None) -> bool:
    """Restore one Keychain item after a multi-item enrollment failure."""
    if previous is None:
        return _delete_native_keychain_secret(service)
    return _store_native_keychain_secret(service, previous)


def enroll_domo_api_keychain(logger: logging.Logger) -> bool:
    """Validate and atomically replace this user's Domo API client pair."""
    if not sys.stdin.isatty():
        logger.error(
            "Domo API Keychain enrollment must be run in an interactive Terminal."
        )
        return False
    print("\nDomo API Keychain enrollment.", flush=True)
    client_id = _prompt_hidden_secret("Domo API client ID")
    client_secret = _prompt_hidden_secret("Domo API client secret")
    if not client_id or not client_secret:
        logger.error(
            "Both Domo API values are required; Keychain was not changed."
        )
        return False

    valid, reason = _validate_domo_api_credentials(client_id, client_secret)
    if not valid:
        if reason in {"http-400", "http-401", "http-403"}:
            logger.error(
                "Domo rejected the API client ID/secret for the data scope; "
                "Keychain was not changed."
            )
        else:
            logger.error(
                "The Domo API credentials could not be validated because the "
                "token service was unavailable or returned an invalid response; "
                "Keychain was not changed."
            )
        return False

    previous_id = _read_native_keychain_secret(DOMO_API_CLIENT_ID_SERVICE)
    previous_secret = _read_native_keychain_secret(DOMO_API_CLIENT_SECRET_SERVICE)
    id_ok = _store_native_keychain_secret(DOMO_API_CLIENT_ID_SERVICE, client_id)
    secret_ok = id_ok and _store_native_keychain_secret(
        DOMO_API_CLIENT_SECRET_SERVICE, client_secret
    )
    if not (id_ok and secret_ok and domo_api_keychain_configured()):
        restored_id = _restore_native_keychain_secret(
            DOMO_API_CLIENT_ID_SERVICE, previous_id
        )
        restored_secret = _restore_native_keychain_secret(
            DOMO_API_CLIENT_SECRET_SERVICE, previous_secret
        )
        logger.error(
            "Domo accepted the API credentials, but Keychain storage did not "
            "round-trip exactly. Previous API credentials were %s.",
            "restored" if restored_id and restored_secret else "not fully restored",
        )
        return False
    logger.info(
        "Domo API client credentials validated and stored in macOS Keychain "
        "(values redacted)."
    )
    return True


def enroll_monday_keychain(logger: logging.Logger) -> bool:
    """Validate and store this user's Monday token without exposing it."""
    if not sys.stdin.isatty():
        logger.error("Monday Keychain enrollment must be run in an interactive Terminal.")
        return False
    print("\nMonday Keychain enrollment.", flush=True)
    token = _prompt_hidden_secret("Monday personal API token")
    if not token:
        logger.error("No Monday API token was entered; Keychain was not changed.")
        return False
    from monday_sync import MondayAuthorizationError, MondayClient, MondayError
    try:
        MondayClient(token).validate_auth()
    except MondayAuthorizationError as exc:
        if exc.status_code == 401:
            logger.error(
                "Monday returned HTTP 401 for both supported authorization forms. "
                "The entered token cannot authenticate this Monday user; Keychain "
                "was not changed. "
                "Confirm the account is an active admin/member with a confirmed "
                "email; Monday blocks API access for viewers, disabled users, and "
                "users whose email is unconfirmed."
            )
        else:
            logger.error(
                f"Monday returned HTTP {exc.status_code or 'authorization failure'}. "
                "The token remains in Keychain; this usually indicates account, "
                "API, network-security, or workspace permission restrictions."
            )
        return False
    except MondayError:
        logger.error(
            "The Monday token could not be validated because the API was unavailable. "
            "Keychain was not changed; retry --enroll-monday-keychain."
        )
        return False
    if not _store_native_keychain_secret(MONDAY_KEYCHAIN_TOKEN_SERVICE, token):
        logger.error(
            "Monday accepted the token, but Keychain did not return the exact same "
            "value after storage. The enrollment was not accepted."
        )
        return False
    logger.info(
        "Monday personal API token validated and stored in macOS Keychain "
        "(value redacted)."
    )
    return True


def delete_domo_keychain_credentials(logger: logging.Logger) -> bool:
    """Delete only the two Keychain items created by this workflow."""
    ok = True
    for service in (DOMO_KEYCHAIN_USERNAME_SERVICE, DOMO_KEYCHAIN_PASSWORD_SERVICE):
        result = subprocess.run(
            [
                "/usr/bin/security", "delete-generic-password",
                "-a", DOMO_KEYCHAIN_ACCOUNT, "-s", service,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode not in (0, 44):
            ok = False
    if ok:
        logger.info("Workflow-owned Domo Keychain credentials deleted.")
    else:
        logger.error("One or more workflow-owned Domo Keychain items could not be deleted.")
    return ok


def delete_monday_keychain_token(logger: logging.Logger) -> bool:
    """Delete only the Monday token created by this workflow."""
    if _delete_native_keychain_secret(MONDAY_KEYCHAIN_TOKEN_SERVICE):
        logger.info("Workflow-owned Monday Keychain token deleted.")
        return True
    logger.error("Workflow-owned Monday Keychain token could not be deleted.")
    return False


def delete_bmat_sftp_keychain_credentials(logger: logging.Logger) -> bool:
    """Delete only the BMAT SFTP pair created by this workflow."""
    ok = True
    for service in (BMAT_SFTP_USERNAME_SERVICE, BMAT_SFTP_PASSWORD_SERVICE):
        ok = _delete_native_keychain_secret(service) and ok
    if ok:
        logger.info("Workflow-owned BMAT SFTP Keychain credentials deleted.")
    else:
        logger.error("One or more BMAT SFTP Keychain items could not be deleted.")
    return ok


def delete_synchtank_s3_keychain_credentials(logger: logging.Logger) -> bool:
    """Delete only the SynchTank AWS pair created by this workflow."""
    ok = True
    for service in (
        SYNCHTANK_S3_ACCESS_KEY_SERVICE,
        SYNCHTANK_S3_SECRET_KEY_SERVICE,
    ):
        ok = _delete_native_keychain_secret(service) and ok
    if ok:
        logger.info("Workflow-owned SynchTank S3 credentials deleted.")
    else:
        logger.error("One or more SynchTank S3 Keychain items could not be deleted.")
    return ok


def delete_tunesat_sftp_keychain_credentials(logger: logging.Logger) -> bool:
    """Delete only the TuneSat SFTP pair created by this workflow."""
    ok = True
    for service in (TUNESAT_SFTP_USERNAME_SERVICE, TUNESAT_SFTP_PASSWORD_SERVICE):
        ok = _delete_native_keychain_secret(service) and ok
    if ok:
        logger.info("Workflow-owned TuneSat SFTP credentials deleted.")
    else:
        logger.error("One or more TuneSat SFTP Keychain items could not be deleted.")
    return ok


def _delete_web_keychain_pair(
    logger: logging.Logger,
    *,
    label: str,
    services: tuple[str, str],
) -> bool:
    ok = True
    for service in services:
        ok = _delete_native_keychain_secret(service) and ok
    if ok:
        logger.info("Workflow-owned %s Keychain credentials deleted.", label)
    else:
        logger.error("One or more workflow-owned %s Keychain items could not be deleted.", label)
    return ok


def delete_espn_keychain_credentials(logger: logging.Logger) -> bool:
    return _delete_web_keychain_pair(
        logger,
        label="ESPN Media Shuttle",
        services=(ESPN_USERNAME_SERVICE, ESPN_PASSWORD_SERVICE),
    )


def delete_netmix_keychain_credentials(logger: logging.Logger) -> bool:
    return _delete_web_keychain_pair(
        logger,
        label="Netmix upload history",
        services=(NETMIX_USERNAME_SERVICE, NETMIX_PASSWORD_SERVICE),
    )


def delete_soundexchange_keychain_credentials(logger: logging.Logger) -> bool:
    return _delete_web_keychain_pair(
        logger,
        label="SoundExchange Direct",
        services=(SOUNDEXCHANGE_USERNAME_SERVICE, SOUNDEXCHANGE_PASSWORD_SERVICE),
    )


def delete_soundmouse_keychain_credentials(logger: logging.Logger) -> bool:
    return _delete_web_keychain_pair(
        logger,
        label="SoundMouse website",
        services=(SOUNDMOUSE_USERNAME_SERVICE, SOUNDMOUSE_PASSWORD_SERVICE),
    )


def delete_domo_api_keychain_credentials(logger: logging.Logger) -> bool:
    """Delete only the two Domo API items created by this workflow."""
    ok = True
    for service in (
        DOMO_API_CLIENT_ID_SERVICE,
        DOMO_API_CLIENT_SECRET_SERVICE,
    ):
        ok = _delete_native_keychain_secret(service) and ok
    if ok:
        logger.info("Workflow-owned Domo API Keychain credentials deleted.")
    else:
        logger.error(
            "One or more workflow-owned Domo API Keychain items could not be deleted."
        )
    return ok


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
    dams_cookie_candidates = (
        DAMS_PROFILE_DIR / "Default" / "Cookies",
        DAMS_PROFILE_DIR / "Default" / "Network" / "Cookies",
    )
    dams_files = DAMS_PROFILE_DIR.exists() and any(DAMS_PROFILE_DIR.iterdir())
    dams_private = (
        DAMS_PROFILE_DIR.exists()
        and (DAMS_PROFILE_DIR.stat().st_mode & 0o077) == 0
    )
    unisync_private = (
        UNISYNC_XML_PATH.exists()
        and (UNISYNC_XML_PATH.stat().st_mode & 0o077) == 0
    )
    return {
        "domo": {
            "state": "configured" if domo_files else "missing",
            "cookie_store_present": any(path.exists() for path in cookie_candidates),
            "keychain_credentials_present": domo_keychain_configured(),
            "private_permissions": domo_private,
            "location": str(DOMO_PROFILE_DIR),
        },
        "domo_api": {
            "state": "configured" if domo_api_keychain_configured() else "missing",
            "private_permissions": True,
            "location": "macOS Keychain",
        },
        "dams": {
            "state": "configured" if dams_files else "missing",
            "cookie_store_present": any(
                path.exists() for path in dams_cookie_candidates
            ),
            "private_permissions": dams_private,
            "location": str(DAMS_PROFILE_DIR),
        },
        "bmat_sftp": {
            "state": "configured" if bmat_sftp_keychain_configured() else "missing",
            "private_permissions": True,
            "location": "macOS Keychain",
        },
        "synchtank_s3": {
            "state": "configured" if synchtank_s3_keychain_configured() else "missing",
            "private_permissions": True,
            "location": "macOS Keychain",
        },
        "tunesat_sftp": {
            "state": "configured" if tunesat_sftp_keychain_configured() else "missing",
            "private_permissions": True,
            "location": "macOS Keychain",
        },
        "espn": {
            "state": "configured" if espn_keychain_configured() else "missing",
            "private_permissions": True,
            "location": "macOS Keychain",
        },
        "netmix": {
            "state": "configured" if netmix_keychain_configured() else "missing",
            "private_permissions": True,
            "location": "macOS Keychain",
        },
        "soundexchange": {
            "state": "configured" if soundexchange_keychain_configured() else "missing",
            "private_permissions": True,
            "location": "macOS Keychain",
        },
        "soundmouse": {
            "state": "configured" if soundmouse_keychain_configured() else "missing",
            "private_permissions": True,
            "location": "macOS Keychain",
        },
        "unisync": {
            "state": "configured" if unisync_auth_configured() else "missing",
            "private_permissions": unisync_private,
            "location": str(UNISYNC_XML_PATH),
        },
        "monday": {
            "state": "configured" if load_monday_keychain_token() else "missing",
            "private_permissions": True,
            "location": "macOS Keychain",
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
                domo._authenticate(page, logger, allow_interactive=True)
                logger.info(
                    "Protected Domo workspace verified; leaving the browser "
                    "visible for 10 seconds for confirmation."
                )
                time.sleep(10)
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
    if unisync_auth_configured():
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
        if unisync_auth_configured():
            secure_private_file(UNISYNC_XML_PATH)
            logger.info("UniSync login detected and local preferences secured (identity redacted).")
            return True
        time.sleep(2)
    logger.error("UniSync login was not detected within five minutes; rerun setup when ready.")
    return False


def setup_dams(logger: logging.Logger) -> bool:
    """Enroll DAMS through its UMG Employee SSO route."""
    from bmat_delivery import setup_dams_auth
    return setup_dams_auth(logger)


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
    if target in {"dams", "all"} and _process_running(str(DAMS_PROFILE_DIR)):
        logger.error("Close the workflow's DAMS/Chromium window before resetting DAMS.")
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
    if target in {"dams", "all"} and DAMS_PROFILE_DIR.exists():
        shutil.move(str(DAMS_PROFILE_DIR), str(archive / "dams_browser_profile"))
        moved += 1
    if target in {"unisync", "all"}:
        for source in (UNISYNC_XML_PATH, UNISYNC_XML_PATH.with_suffix(".xml.bak")):
            if source.exists():
                shutil.move(str(source), str(archive / source.name))
                moved += 1
    secure_private_directory(PRIVATE_STATE_DIR)
    if target in {"domo", "all"}:
        secure_private_directory(DOMO_PROFILE_DIR)
    if target in {"dams", "all"}:
        secure_private_directory(DAMS_PROFILE_DIR)
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
    action.add_argument("--setup", choices=("domo", "dams", "unisync", "all"), help="Configure this user's login")
    action.add_argument(
        "--enroll-domo-keychain", action="store_true",
        help="Store Domo email/password using hidden prompts in macOS Keychain",
    )
    action.add_argument(
        "--delete-domo-keychain", action="store_true",
        help="Delete workflow-owned Domo credentials from macOS Keychain",
    )
    action.add_argument(
        "--enroll-domo-api-keychain", action="store_true",
        help="Validate and store a Domo API client ID/secret in macOS Keychain",
    )
    action.add_argument(
        "--delete-domo-api-keychain", action="store_true",
        help="Delete workflow-owned Domo API credentials from macOS Keychain",
    )
    action.add_argument(
        "--enroll-monday-keychain", action="store_true",
        help="Store this user's Monday personal API token using hidden Keychain prompts",
    )
    action.add_argument(
        "--delete-monday-keychain", action="store_true",
        help="Delete the workflow-owned Monday API token from macOS Keychain",
    )
    action.add_argument(
        "--enroll-bmat-keychain", action="store_true",
        help="Store the BMAT SFTP username/password using hidden Keychain prompts",
    )
    action.add_argument(
        "--delete-bmat-keychain", action="store_true",
        help="Delete the workflow-owned BMAT SFTP pair from macOS Keychain",
    )
    action.add_argument(
        "--enroll-synchtank-keychain", action="store_true",
        help="Store the SynchTank AWS access-key pair using hidden Keychain prompts",
    )
    action.add_argument(
        "--delete-synchtank-keychain", action="store_true",
        help="Delete the workflow-owned SynchTank AWS pair from macOS Keychain",
    )
    action.add_argument(
        "--enroll-tunesat-keychain", action="store_true",
        help="Store the TuneSat SFTP username/password using hidden Keychain prompts",
    )
    action.add_argument(
        "--delete-tunesat-keychain", action="store_true",
        help="Delete the workflow-owned TuneSat SFTP pair from macOS Keychain",
    )
    action.add_argument(
        "--enroll-espn-keychain", action="store_true",
        help="Store ESPN Media Shuttle credentials using hidden Keychain prompts",
    )
    action.add_argument(
        "--delete-espn-keychain", action="store_true",
        help="Delete the workflow-owned ESPN Media Shuttle credential pair",
    )
    action.add_argument(
        "--enroll-netmix-keychain", action="store_true",
        help="Store Netmix upload-history credentials using hidden Keychain prompts",
    )
    action.add_argument(
        "--delete-netmix-keychain", action="store_true",
        help="Delete the workflow-owned Netmix upload-history credential pair",
    )
    action.add_argument(
        "--enroll-soundexchange-keychain", action="store_true",
        help="Store SoundExchange Direct credentials using hidden Keychain prompts",
    )
    action.add_argument(
        "--delete-soundexchange-keychain", action="store_true",
        help="Delete the workflow-owned SoundExchange Direct credential pair",
    )
    action.add_argument(
        "--enroll-soundmouse-keychain", action="store_true",
        help="Store SoundMouse website credentials using hidden Keychain prompts",
    )
    action.add_argument(
        "--delete-soundmouse-keychain", action="store_true",
        help="Delete the workflow-owned SoundMouse website credential pair",
    )
    action.add_argument("--reset", choices=("domo", "dams", "unisync", "all"), help="Move local auth state to Trash")
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
                extra = ""
                if name == "domo":
                    extra = (
                        "; unattended_credentials="
                        f"{detail['keychain_credentials_present']}"
                    )
                elif name == "monday":
                    extra = "; api_token_present=" + str(detail["state"] == "configured")
                elif name == "domo_api":
                    extra = "; api_client_present=" + str(
                        detail["state"] == "configured"
                    )
                print(
                    f"{name}: {detail['state']}; "
                    f"private_permissions={detail['private_permissions']}; "
                    f"location={detail['location']}{extra}"
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

    if args.enroll_domo_keychain:
        return 0 if enroll_domo_keychain(logger) else 1
    if args.delete_domo_keychain:
        if not args.confirm_reset:
            parser.error("--delete-domo-keychain requires --confirm-reset")
        return 0 if delete_domo_keychain_credentials(logger) else 1
    if args.enroll_domo_api_keychain:
        return 0 if enroll_domo_api_keychain(logger) else 1
    if args.delete_domo_api_keychain:
        if not args.confirm_reset:
            parser.error("--delete-domo-api-keychain requires --confirm-reset")
        return 0 if delete_domo_api_keychain_credentials(logger) else 1
    if args.enroll_monday_keychain:
        return 0 if enroll_monday_keychain(logger) else 1
    if args.delete_monday_keychain:
        if not args.confirm_reset:
            parser.error("--delete-monday-keychain requires --confirm-reset")
        return 0 if delete_monday_keychain_token(logger) else 1
    if args.enroll_bmat_keychain:
        return 0 if enroll_bmat_sftp_keychain(logger) else 1
    if args.delete_bmat_keychain:
        if not args.confirm_reset:
            parser.error("--delete-bmat-keychain requires --confirm-reset")
        return 0 if delete_bmat_sftp_keychain_credentials(logger) else 1
    if args.enroll_synchtank_keychain:
        return 0 if enroll_synchtank_s3_keychain(logger) else 1
    if args.delete_synchtank_keychain:
        if not args.confirm_reset:
            parser.error("--delete-synchtank-keychain requires --confirm-reset")
        return 0 if delete_synchtank_s3_keychain_credentials(logger) else 1
    if args.enroll_tunesat_keychain:
        return 0 if enroll_tunesat_sftp_keychain(logger) else 1
    if args.delete_tunesat_keychain:
        if not args.confirm_reset:
            parser.error("--delete-tunesat-keychain requires --confirm-reset")
        return 0 if delete_tunesat_sftp_keychain_credentials(logger) else 1
    if args.enroll_espn_keychain:
        return 0 if enroll_espn_keychain(logger) else 1
    if args.delete_espn_keychain:
        if not args.confirm_reset:
            parser.error("--delete-espn-keychain requires --confirm-reset")
        return 0 if delete_espn_keychain_credentials(logger) else 1
    if args.enroll_netmix_keychain:
        return 0 if enroll_netmix_keychain(logger) else 1
    if args.delete_netmix_keychain:
        if not args.confirm_reset:
            parser.error("--delete-netmix-keychain requires --confirm-reset")
        return 0 if delete_netmix_keychain_credentials(logger) else 1
    if args.enroll_soundexchange_keychain:
        return 0 if enroll_soundexchange_keychain(logger) else 1
    if args.delete_soundexchange_keychain:
        if not args.confirm_reset:
            parser.error("--delete-soundexchange-keychain requires --confirm-reset")
        return 0 if delete_soundexchange_keychain_credentials(logger) else 1
    if args.enroll_soundmouse_keychain:
        return 0 if enroll_soundmouse_keychain(logger) else 1
    if args.delete_soundmouse_keychain:
        if not args.confirm_reset:
            parser.error("--delete-soundmouse-keychain requires --confirm-reset")
        return 0 if delete_soundmouse_keychain_credentials(logger) else 1

    ok = True
    if args.setup in {"domo", "all"}:
        ok = setup_domo(logger) and ok
    if args.setup in {"dams", "all"}:
        ok = setup_dams(logger) and ok
    if args.setup in {"unisync", "all"}:
        ok = setup_unisync(logger) and ok
    secure_auth_permissions()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_main())
