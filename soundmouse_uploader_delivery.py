#!/usr/bin/env python3
"""Deliver the complete Step 16 package through Soundmouse Uploader.

The native app owns authentication and network transfer. This module validates
the exact package and Step 16 gate, explicitly selects workspace ``UPPM`` and
module ``Music``, submits the package directory, then verifies the app's private
queue contains exactly the expected local file URLs and that every new row
reaches its completed status.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from config import LOGS_DIR, ReleaseContext, context_from_cli_args
from delivery_state import set_partner_status


APP_PATH = Path("/Applications/Soundmouse Uploader.app")
APP_PROCESS = "Uploader"
REQUIRED_WORKSPACE = "UPPM"
REQUIRED_MODULE = "Music"
QUEUE_DB = Path.home() / (
    "Library/Containers/com.soundmouse.uploader/Data/Library/Application Support/"
    "com.soundmouse.uploader/data.sqlite"
)
COMPLETED_STATUS = 3
ACTIVE_STATUSES = frozenset({0, 1, 2})
IGNORED_NAMES = frozenset({".DS_Store", "delivery.complete"})


class SoundMouseUploaderError(RuntimeError):
    """A package, UI, queue, or upload failure that must stop delivery."""


@dataclass(frozen=True)
class PackageFile:
    path: Path
    relative: str
    size: int


@dataclass(frozen=True)
class QueueRow:
    row_id: int
    url: str
    status: int
    workspace_id: str
    module_name: str
    failure_reason: int
    original_filename: str


def collect_package(
    root: Path,
    *,
    required_top_levels: frozenset[str] = frozenset({"MEDIA", "Covers", "Metadata"}),
) -> tuple[PackageFile, ...]:
    root = Path(root)
    if not root.is_dir():
        raise SoundMouseUploaderError(f"SoundMouse package is missing: {root}")
    files: list[PackageFile] = []
    for top_level in ("Covers", "MEDIA", "Metadata"):
        delivery_root = root / top_level
        if not delivery_root.is_dir():
            continue
        for path in sorted(delivery_root.rglob("*")):
            if path.is_symlink():
                raise SoundMouseUploaderError(
                    f"SoundMouse package contains a symlink: {path}"
                )
            if not path.is_file() or path.name in IGNORED_NAMES:
                continue
            size = path.stat().st_size
            if size <= 0:
                raise SoundMouseUploaderError(
                    f"SoundMouse package contains an empty file: {path}"
                )
            files.append(
                PackageFile(path.resolve(), path.relative_to(root).as_posix(), size)
            )
    if not files:
        raise SoundMouseUploaderError(f"SoundMouse package contains no files: {root}")
    top_levels = {Path(item.relative).parts[0] for item in files}
    missing = sorted(required_top_levels - top_levels)
    if missing:
        raise SoundMouseUploaderError(
            "SoundMouse package is incomplete; missing nonempty: " + ", ".join(missing)
        )
    return tuple(files)


def soundmouse_gate_passed(ctx: ReleaseContext) -> tuple[bool, str]:
    """Require the newest real, non-skipped Step 16 result to be completed."""
    report_dir = Path(LOGS_DIR) / "reports" / ctx.release_id
    if report_dir.is_dir():
        for path in sorted(report_dir.glob("run-*.json"), reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if bool((payload.get("run") or {}).get("dry_run")):
                continue
            entry = ((payload.get("steps") or {}).get("16 SoundMouse") or {})
            status = str(entry.get("status") or "")
            if status and status != "skipped":
                if status == "completed":
                    return True, "Step 16 completed"
                return False, f"newest Step 16 result is {status}"
    return False, "no real-run Step 16 result exists for this batch"


def _connect_queue(read_only: bool = True) -> sqlite3.Connection:
    if not QUEUE_DB.is_file():
        raise SoundMouseUploaderError(f"Soundmouse Uploader queue is missing: {QUEUE_DB}")
    uri = f"file:{QUEUE_DB}?mode={'ro' if read_only else 'rw'}"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def queue_snapshot() -> tuple[int, tuple[QueueRow, ...]]:
    with _connect_queue() as connection:
        rows = connection.execute(
            "select id, url, status, workspace_id, module_name, failure_reason, "
            "original_filename from upload_item order by id"
        ).fetchall()
    parsed = tuple(
        QueueRow(
            row_id=int(row["id"]),
            url=str(row["url"]),
            status=int(row["status"]),
            workspace_id=str(row["workspace_id"]),
            module_name=str(row["module_name"]),
            failure_reason=int(row["failure_reason"]),
            original_filename=str(row["original_filename"]),
        )
        for row in rows
    )
    return max((row.row_id for row in parsed), default=0), parsed


def _url_path(value: str) -> Path:
    parsed = urlparse(value)
    if parsed.scheme != "file":
        raise SoundMouseUploaderError("Uploader queue contains a non-file source URL")
    return Path(unquote(parsed.path)).resolve()


def validate_queue_rows(
    expected: tuple[PackageFile, ...],
    rows: tuple[QueueRow, ...],
) -> None:
    expected_paths = Counter(item.path for item in expected)
    actual_paths = Counter(_url_path(row.url) for row in rows)
    if actual_paths != expected_paths:
        missing = sorted(str(path) for path in (expected_paths - actual_paths).elements())
        extra = sorted(str(path) for path in (actual_paths - expected_paths).elements())
        raise SoundMouseUploaderError(
            f"Uploader queue manifest mismatch; missing={missing[:10]}, extra={extra[:10]}"
        )
    wrong_module = [row.original_filename for row in rows if row.module_name != "music_manager"]
    if wrong_module:
        raise SoundMouseUploaderError(
            "Uploader queued files outside the Music module: " + ", ".join(wrong_module[:10])
        )
    workspace_ids = {row.workspace_id for row in rows}
    if len(workspace_ids) != 1 or not next(iter(workspace_ids), ""):
        raise SoundMouseUploaderError("Uploader queue did not resolve one workspace ID")


def _ui_script(package: Path) -> str:
    package_path = str(package).replace("\\", "\\\\").replace('"', '\\"')
    return f'''
tell application "{APP_PROCESS}" to activate
tell application "System Events"
    tell process "{APP_PROCESS}"
        set frontmost to true
        set mainWindow to missing value
        repeat 120 times
            repeat with candidateWindow in windows
                try
                    if exists toolbar 1 of candidateWindow then
                        set mainWindow to candidateWindow
                        exit repeat
                    end if
                end try
            end repeat
            if mainWindow is not missing value then exit repeat
            delay 0.25
        end repeat
        if mainWindow is missing value then error "Uploader main window did not open"

        if exists sheet 1 of mainWindow then
            if exists button "Sign In" of sheet 1 of mainWindow then
                click button "Sign In" of sheet 1 of mainWindow
                repeat 240 times
                    if not (exists sheet 1 of mainWindow) then exit repeat
                    delay 0.25
                end repeat
            end if
        end if
        if exists sheet 1 of mainWindow then error "Uploader sign-in did not complete"

        set workspaceButton to first pop up button of toolbar 1 of mainWindow whose description is "Workspace"
        click workspaceButton
        click menu item "{REQUIRED_WORKSPACE}" of menu 1 of workspaceButton
        delay 0.5
        if value of workspaceButton is not "{REQUIRED_WORKSPACE}" then error "Workspace is not UPPM"

        set moduleButton to first pop up button of toolbar 1 of mainWindow whose description is "Module"
        click moduleButton
        click menu item "{REQUIRED_MODULE}" of menu 1 of moduleButton
        delay 0.5
        if value of moduleButton is not "{REQUIRED_MODULE}" then error "Module is not Music"

        click menu item "Add…" of menu "File" of menu bar 1
        repeat 120 times
            if exists sheet 1 of mainWindow then exit repeat
            delay 0.25
        end repeat
        if not (exists sheet 1 of mainWindow) then error "Uploader Add panel did not open"
        set uploadSheet to sheet 1 of mainWindow

        keystroke "g" using {{command down, shift down}}
        repeat 120 times
            if exists sheet 1 of uploadSheet then exit repeat
            delay 0.25
        end repeat
        if not (exists sheet 1 of uploadSheet) then error "Uploader Go to Folder panel did not open"
        set goToSheet to sheet 1 of uploadSheet
        set value of text field 1 of goToSheet to "{package_path}"
        key code 36
        repeat 120 times
            if not (exists sheet 1 of uploadSheet) then exit repeat
            delay 0.25
        end repeat
        if exists sheet 1 of uploadSheet then error "Uploader did not navigate to package folder"
        key code 36
    end tell
end tell
'''


def _ui_submit_package(package: Path) -> None:
    """Select UPPM/Music and submit one directory through the native open panel."""
    result = subprocess.run(
        ["/usr/bin/osascript"],
        input=_ui_script(package),
        text=True,
        capture_output=True,
        timeout=180,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise SoundMouseUploaderError(f"Could not submit package to Uploader: {detail}")


def _receipt_path(ctx: ReleaseContext, *, correction: bool = False) -> Path:
    filename = (
        "soundmouse_uploader_correction_receipt.json"
        if correction else "soundmouse_uploader_receipt.json"
    )
    return ctx.specials_dir / "_WORKFLOW" / filename


def _write_receipt(
    ctx: ReleaseContext,
    files: tuple[PackageFile, ...],
    rows: tuple[QueueRow, ...],
    *,
    correction: bool = False,
) -> Path:
    path = _receipt_path(ctx, correction=correction)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "schema_version": 1,
        "partner": "soundmouse",
        "release_id": ctx.release_id,
        "workspace": REQUIRED_WORKSPACE,
        "module": REQUIRED_MODULE,
        "correction": correction,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "queue_row_ids": [row.row_id for row in rows],
        "files": [{"path": item.relative, "size": item.size} for item in files],
    }
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def deliver_soundmouse_uploader(
    ctx: ReleaseContext,
    dry_run: bool,
    logger: logging.Logger,
    *,
    timeout_hours: float = 24.0,
    correction: bool = False,
) -> bool:
    try:
        package_root = (
            ctx.soundmouse_release_dir / "Missing"
            if correction else ctx.soundmouse_release_dir
        )
        files = collect_package(
            package_root,
            required_top_levels=(
                frozenset({"Metadata"})
                if correction
                else frozenset({"MEDIA", "Covers", "Metadata"})
            ),
        )
        logger.info(
            "  SoundMouse package: %d file(s), %d byte(s)",
            len(files), sum(item.size for item in files),
        )
        if dry_run:
            logger.info(
                "  [DRY RUN] Would submit the full package to %s → %s",
                REQUIRED_WORKSPACE, REQUIRED_MODULE,
            )
            return True

        gate_ok, gate_detail = soundmouse_gate_passed(ctx)
        if not gate_ok:
            raise SoundMouseUploaderError(f"SoundMouse upload is blocked: {gate_detail}")
        if not APP_PATH.is_dir():
            raise SoundMouseUploaderError(f"Soundmouse Uploader is not installed: {APP_PATH}")
        subprocess.run(["/usr/bin/open", "-a", str(APP_PATH)], check=True, timeout=30)
        deadline = time.monotonic() + 60
        while not QUEUE_DB.is_file() and time.monotonic() < deadline:
            time.sleep(0.5)
        before_id, existing = queue_snapshot()
        active = [row for row in existing if row.status in ACTIVE_STATUSES]
        if active:
            raise SoundMouseUploaderError(
                f"Uploader already has {len(active)} active queue item(s)"
            )

        _ui_submit_package(package_root)
        deadline = time.monotonic() + max(timeout_hours, 0.1) * 3600
        new_rows: tuple[QueueRow, ...] = ()
        last_report = 0.0
        while time.monotonic() < deadline:
            _, snapshot = queue_snapshot()
            new_rows = tuple(row for row in snapshot if row.row_id > before_id)
            if len(new_rows) > len(files):
                raise SoundMouseUploaderError("Uploader queued more files than the package contains")
            if len(new_rows) == len(files):
                validate_queue_rows(files, new_rows)
                failed = [row for row in new_rows if row.failure_reason or row.status not in ACTIVE_STATUSES | {COMPLETED_STATUS}]
                if failed:
                    raise SoundMouseUploaderError(
                        f"Uploader reported {len(failed)} failed file(s)"
                    )
                if all(row.status == COMPLETED_STATUS for row in new_rows):
                    break
            now = time.monotonic()
            if now - last_report >= 60:
                complete = sum(row.status == COMPLETED_STATUS for row in new_rows)
                logger.info("  Uploader progress: %d/%d completed", complete, len(files))
                last_report = now
            time.sleep(5)
        else:
            raise SoundMouseUploaderError("SoundMouse upload timed out before exact completion")

        receipt = _write_receipt(
            ctx, files, new_rows, correction=correction
        )
        if not correction:
            set_partner_status(ctx.specials_dir, "soundmouse", "delivered")
        logger.info(
            "  ✓ SoundMouse %s uploaded to UPPM/Music: %s",
            "correction" if correction else "package",
            receipt,
        )
        return True
    except Exception as exc:
        logger.error("  ✗ SoundMouse Uploader delivery failed: %s", exc)
        return False


def _main() -> int:
    parser = argparse.ArgumentParser(description="Deliver the full SoundMouse package")
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--part", type=int, choices=(1, 2))
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--full-month-content", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout-hours", type=float, default=24.0)
    parser.add_argument(
        "--correction",
        action="store_true",
        help="Upload only the validated SoundMouse Missing correction package",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ok = deliver_soundmouse_uploader(
        context_from_cli_args(args),
        args.dry_run,
        logging.getLogger("soundmouse_uploader"),
        timeout_hours=args.timeout_hours,
        correction=args.correction,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
