"""API-priority Netmix delivery with a guarded master-folder portal fallback."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from config import PRIVATE_STATE_DIR, ReleaseContext, context_from_cli_args
from delivery_common import (
    DeliverySafetyError,
    collect_manifest,
    latest_workflow_gates,
    receipt,
    require_live_authorization,
)
from delivery_state import set_partner_status


PORTAL_URL = "https://www.cndmusictracker.com/main.html"
PROFILE_DIR = PRIVATE_STATE_DIR / "netmix_portal_profile"
STORAGE_STATE_PATH = PROFILE_DIR / "storage_state.json"
AUDIO_SUFFIXES = frozenset({".wav", ".aif", ".aiff"})
COVER_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})


class NetmixDeliveryError(DeliverySafetyError):
    pass


@dataclass(frozen=True)
class NetmixPlan:
    package: Path
    metadata: Path
    files: tuple
    audio_names: frozenset[str]


@dataclass(frozen=True)
class NetmixApiResult:
    completed: bool
    safe_to_fallback: bool
    detail: str = ""


class NetmixApiGateway(Protocol):
    def configured(self) -> bool: ...
    def deliver(self, plan: NetmixPlan) -> NetmixApiResult: ...


class NetmixPortalGateway(Protocol):
    def require_authenticated(self) -> None: ...
    def history_statuses(
        self, folder_name: str, expected_audio: frozenset[str]
    ) -> dict[str, str]: ...
    def upload_package(self, package: Path) -> None: ...
    def wait_for_completion(
        self, folder_name: str, expected_audio: frozenset[str], timeout_seconds: int
    ) -> dict[str, str]: ...
    def close(self) -> None: ...


_COMPLETE_STATUS_WORDS = ("complete", "accepted", "ingested", "success")
_ACTIVE_STATUS_WORDS = (
    "active", "pending", "processing", "queued", "received", "uploading"
)
_FAILED_STATUS_WORDS = ("error", "failed", "rejected")


def history_status_complete(status: str) -> bool:
    lowered = str(status).casefold()
    return any(word in lowered for word in _COMPLETE_STATUS_WORDS)


def validate_history_statuses(
    records: dict[str, str], expected_audio: frozenset[str]
) -> None:
    unexpected = set(records) - set(expected_audio)
    if unexpected:
        raise NetmixDeliveryError(
            "Netmix upload history contains unexpected audio"
        )
    if any(
        word in status.casefold()
        for status in records.values()
        for word in _FAILED_STATUS_WORDS
    ):
        raise NetmixDeliveryError("Netmix upload history contains a rejected track")


def history_resume_action(
    records: dict[str, str], expected_audio: frozenset[str]
) -> str:
    """Return upload, wait, or complete without permitting duplicate media."""
    validate_history_statuses(records, expected_audio)
    if not records:
        return "upload"
    if set(records) == set(expected_audio) and all(
        history_status_complete(status) for status in records.values()
    ):
        return "complete"
    active = any(
        word in status.casefold()
        for status in records.values()
        for word in _ACTIVE_STATUS_WORDS
    )
    if active:
        return "wait"
    if all(history_status_complete(status) for status in records.values()):
        raise NetmixDeliveryError(
            "Netmix history contains a completed partial package; refusing to "
            "re-upload files that already exist"
        )
    raise NetmixDeliveryError(
        "Netmix history contains an unknown non-terminal status; refusing a retry"
    )


def package_root(ctx: ReleaseContext) -> Path:
    return (
        ctx.specials_dir / "3-FINAL PACKAGING"
        / ctx.partner_folder_name("Netmix")
    )


def build_plan(package: Path) -> NetmixPlan:
    package = Path(package)
    if not package.is_dir() or package.is_symlink():
        raise NetmixDeliveryError(f"Netmix package is missing: {package}")
    metadata_files = sorted((package / "Metadata").glob("*.csv"))
    if len(metadata_files) != 1 or not metadata_files[0].is_file():
        raise NetmixDeliveryError(
            f"Netmix package must contain exactly one metadata CSV; found {len(metadata_files)}"
        )
    music = package / "Music"
    if not music.is_dir():
        raise NetmixDeliveryError("Netmix package has no Music directory")
    audio_by_name: dict[str, Path] = {}
    albums: set[Path] = set()
    for path in sorted(package.rglob("*")):
        if path.is_symlink():
            raise NetmixDeliveryError(f"Netmix package contains a symlink: {path}")
        if not path.is_file() or path.name == ".DS_Store":
            continue
        if path.stat().st_size <= 0:
            raise NetmixDeliveryError(f"Netmix package contains an empty file: {path}")
        suffix = path.suffix.casefold()
        if path != metadata_files[0] and suffix not in AUDIO_SUFFIXES | COVER_SUFFIXES:
            raise NetmixDeliveryError(f"Unexpected Netmix package file: {path}")
        if suffix in AUDIO_SUFFIXES:
            key = path.name.casefold()
            if key in audio_by_name:
                raise NetmixDeliveryError(f"Netmix audio filename is duplicated: {path.name}")
            audio_by_name[key] = path
            albums.add(path.parent)
    if not audio_by_name:
        raise NetmixDeliveryError("Netmix package contains no audio")
    for album in albums:
        covers = [
            path for path in album.iterdir()
            if path.is_file() and path.suffix.casefold() in COVER_SUFFIXES
        ]
        if len(covers) != 1:
            raise NetmixDeliveryError(
                f"Netmix album must contain exactly one cover: {album}"
            )
    with metadata_files[0].open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        field = next(
            (
                name for name in (reader.fieldnames or [])
                if name.strip().casefold() == "filename"
            ),
            None,
        )
        if field is None:
            raise NetmixDeliveryError("Netmix metadata has no Filename column")
        names = [
            Path(str(row.get(field) or "").strip()).name.casefold()
            for row in reader
        ]
    if any(not name for name in names):
        raise NetmixDeliveryError("Netmix metadata contains a blank Filename")
    if len(names) != len(set(names)):
        raise NetmixDeliveryError("Netmix metadata contains duplicate Filename values")
    if set(names) != set(audio_by_name):
        raise NetmixDeliveryError(
            "Netmix metadata Filename values do not exactly match the audio manifest"
        )
    return NetmixPlan(
        package,
        metadata_files[0],
        collect_manifest(package, include_root=True),
        frozenset(audio_by_name),
    )


def restore_portal_master(staging_root: Path) -> None:
    journal = staging_root / "restore.json"
    if not staging_root.exists():
        return
    if not journal.is_file():
        if any(path.is_file() for path in staging_root.rglob("*")):
            raise NetmixDeliveryError(
                f"Netmix staging exists without a restore journal: {staging_root}"
            )
        shutil.rmtree(staging_root)
        return
    try:
        moves = json.loads(journal.read_text(encoding="utf-8"))["moves"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise NetmixDeliveryError("Netmix restore journal is invalid") from exc
    for item in reversed(moves):
        source = Path(item["source"])
        destination = Path(item["destination"])
        if source.exists() and destination.exists():
            raise NetmixDeliveryError(
                f"Netmix restore conflict exists at both paths: {source}"
            )
        if destination.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, source)
        elif not source.exists():
            raise NetmixDeliveryError(f"Netmix staged item is missing: {destination}")
    shutil.rmtree(staging_root)


def prepare_portal_master(package: Path) -> tuple[Path, Path]:
    """Temporarily stage Netmix's documented master-folder layout."""
    package = Path(package)
    staging_root = package.parent / ".netmix_portal_staging"
    if staging_root.is_symlink():
        raise NetmixDeliveryError(f"Netmix staging root is a symlink: {staging_root}")
    if staging_root.exists():
        restore_portal_master(staging_root)
    master = staging_root / package.name
    master.mkdir(parents=True)

    metadata_files = sorted((package / "Metadata").glob("*.csv"))
    if len(metadata_files) != 1:
        raise NetmixDeliveryError("Netmix portal staging requires one metadata CSV")
    music = package / "Music"
    album_dirs: list[Path] = []
    for label_dir in sorted(music.iterdir()):
        if label_dir.name == ".DS_Store":
            continue
        if not label_dir.is_dir():
            raise NetmixDeliveryError(
                f"Netmix Music root contains a non-label item: {label_dir.name}"
            )
        for album_dir in sorted(label_dir.iterdir()):
            if album_dir.name == ".DS_Store":
                continue
            if not album_dir.is_dir():
                raise NetmixDeliveryError(
                    f"Netmix label folder contains a non-album item: {album_dir}"
                )
            album_dirs.append(album_dir)
    album_names = [album_dir.name.casefold() for album_dir in album_dirs]
    if len(album_names) != len(set(album_names)):
        raise NetmixDeliveryError(
            "Netmix album folder names collide when removing the label layer"
        )
    sources = [metadata_files[0], *album_dirs]
    moves = [
        {"source": str(source), "destination": str(master / source.name)}
        for source in sources
    ]
    journal = staging_root / "restore.json"
    journal.write_text(json.dumps({"moves": moves}, indent=2) + "\n", encoding="utf-8")
    journal.chmod(0o600)
    try:
        for item in moves:
            os.replace(item["source"], item["destination"])
    except Exception:
        restore_portal_master(staging_root)
        raise
    return master, staging_root


class PlaywrightNetmixPortalGateway:
    """Retained-session CND adapter using the portal's Uppy folder control."""

    def __init__(
        self, *, setup_auth: bool = False, native_folder_picker: bool = False
    ) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise NetmixDeliveryError("Netmix portal delivery requires Playwright") from exc
        PROFILE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        PROFILE_DIR.chmod(0o700)
        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False
        )
        if STORAGE_STATE_PATH.is_file():
            try:
                state = json.loads(STORAGE_STATE_PATH.read_text(encoding="utf-8"))
                cookies = state.get("cookies")
                if not isinstance(cookies, list):
                    raise ValueError("cookies are missing")
                if cookies:
                    self._context.add_cookies(cookies)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise NetmixDeliveryError("Netmix private browser state is invalid") from exc
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.goto(PORTAL_URL, wait_until="domcontentloaded")
        self._setup_auth = setup_auth
        self._native_folder_picker = native_folder_picker
        self._staging_root: Path | None = None

    def _ready(self) -> bool:
        return bool(
            "/auth/" not in self._page.url
            and self._page.locator("#buttondiva").count() == 1
            and self._page.locator("#buttondivb").count() == 1
        )

    def _probe_authenticated(self, timeout: int) -> bool:
        if "/auth/" in self._page.url:
            return False
        button = self._page.locator("#buttondivb")
        try:
            button.wait_for(state="visible", timeout=15_000)
        except Exception:
            return False
        if button.count() != 1:
            return False
        button.click()
        try:
            self._page.wait_for_url("**/auth/**", timeout=timeout)
        except Exception:
            return self._ready()
        return False

    def _open_upload_surface(self) -> None:
        if self._page.url != PORTAL_URL:
            self._page.goto(PORTAL_URL, wait_until="domcontentloaded")
        button = self._page.locator("#buttondiva")
        if button.count() != 1:
            raise NetmixDeliveryError("Netmix upload control was not unique")
        button.click()
        self._page.get_by_text(
            "Drop Master Folder here", exact=True
        ).wait_for(state="visible", timeout=15_000)

    def require_authenticated(self) -> None:
        if self._probe_authenticated(8_000):
            self._open_upload_surface()
            return
        from auth_manager import load_netmix_credentials
        from portal_auth import PortalAuthenticationError, attempt_keychain_login
        credentials = load_netmix_credentials()
        if credentials is not None:
            try:
                authenticated = attempt_keychain_login(
                    self._page,
                    credentials,
                    ready=lambda: "/auth/" not in self._page.url,
                )
            except PortalAuthenticationError as exc:
                raise NetmixDeliveryError(
                    f"Netmix authentication failed closed: {exc}"
                ) from exc
            if authenticated:
                self._page.goto(PORTAL_URL, wait_until="domcontentloaded")
                if self._probe_authenticated(8_000):
                    self._open_upload_surface()
                    return
        if self._setup_auth:
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                if "/auth/" not in self._page.url and self._probe_authenticated(5_000):
                    self._open_upload_surface()
                    return
                self._page.wait_for_timeout(2_000)
            raise NetmixDeliveryError(
                "Netmix portal sign-in did not complete within fifteen minutes"
            )
        raise NetmixDeliveryError(
            "Netmix portal session is not authenticated; run "
            "auth_manager.py --enroll-netmix-keychain or "
            "netmix_portal_delivery.py --setup-auth"
        )

    def upload_package(self, package: Path) -> None:
        self.require_authenticated()
        portal_master, self._staging_root = prepare_portal_master(package)
        drop_target = self._page.get_by_text("Drop Master Folder here", exact=True)
        drop_target.wait_for(state="visible")
        start = self._page.locator(".uppy-StatusBar-actionBtn--upload:visible")
        inputs = self._page.locator(
            'input[type="file"][webkitdirectory], input[type="file"][directory]'
        )
        if inputs.count() != 1:
            raise NetmixDeliveryError("Netmix master-folder input was not unique")
        if self._native_folder_picker:
            self._page.evaluate(
                """() => {
                    const input = document.querySelector(
                        'input[type="file"][webkitdirectory], input[type="file"][directory]'
                    );
                    if (!input) throw new Error('Netmix directory input is missing');
                    input.id = 'upm-netmix-folder-input';
                    document.getElementById('upm-netmix-folder-label')?.remove();
                    const label = document.createElement('label');
                    label.id = 'upm-netmix-folder-label';
                    label.htmlFor = input.id;
                    label.textContent = 'Select Netmix Folder';
                    Object.assign(label.style, {
                        position: 'fixed', left: '400px', top: '250px',
                        zIndex: '2147483647', padding: '20px',
                        background: '#2563eb', color: 'white',
                        fontSize: '24px', cursor: 'pointer'
                    });
                    document.body.appendChild(label);
                }"""
            )
            start.wait_for(state="visible", timeout=30 * 60 * 1000)
            self._page.locator("#upm-netmix-folder-label").evaluate(
                "element => element.remove()"
            )
        else:
            # Chromium must enumerate every file in the selected directory before
            # Uppy receives it. Large release folders on Pegasus routinely exceed
            # Playwright's 30-second action default even though enumeration is
            # progressing normally.
            inputs.first.set_input_files(str(portal_master), timeout=15 * 60 * 1000)
        start.wait_for(state="visible", timeout=30 * 60 * 1000)
        if start.count() != 1:
            raise NetmixDeliveryError("Netmix portal exposed multiple upload actions")
        if not start.first.is_enabled():
            raise NetmixDeliveryError(
                "Netmix upload action is disabled after folder selection"
            )
        start.first.click()
        primary = self._page.locator(".uppy-StatusBar-statusPrimary:visible")
        primary.wait_for(state="visible", timeout=60_000)
        initial_status = " ".join(primary.first.inner_text().split()).casefold()
        if any(word in initial_status for word in _FAILED_STATUS_WORDS):
            raise NetmixDeliveryError(
                f"Netmix upload was rejected at initiation: {initial_status}"
            )
        self._page.locator(
            ".uppy-StatusBar-statusPrimary", has_text="Complete"
        ).wait_for(state="visible", timeout=12 * 60 * 60 * 1000)

    def _history_rows(self) -> list[dict[str, str]]:
        payload = self._page.evaluate(
            """() => {
                const grid = window.w2ui && window.w2ui.gridviewuploads;
                if (!grid || !Array.isArray(grid.records)) return [];
                return grid.records.map(record => {
                    const result = {};
                    for (const [key, value] of Object.entries(record || {})) {
                        if (value === null || ['string','number','boolean'].includes(typeof value)) {
                            result[String(key).toLowerCase()] = String(value ?? '');
                        }
                    }
                    return result;
                });
            }"""
        )
        return payload if isinstance(payload, list) else []

    @staticmethod
    def _value(row: dict[str, str], names: tuple[str, ...]) -> str:
        for name in names:
            if row.get(name):
                return row[name].strip()
        for key, value in row.items():
            if any(name in key for name in names) and value.strip():
                return value.strip()
        return ""

    def _open_history_surface(self) -> None:
        grid = self._page.locator("#grid_gridviewuploads_body")
        if not grid.count() or not grid.is_visible():
            self._page.locator("#buttondivb").click()
            grid.wait_for(state="visible")

    def history_statuses(
        self, folder_name: str, expected_audio: frozenset[str]
    ) -> dict[str, str]:
        self._open_history_surface()
        records: dict[str, str] = {}
        for row in self._history_rows():
            folder = self._value(row, ("folder", "directory", "path"))
            filename = Path(self._value(row, ("filename", "file", "name"))).name
            status = self._value(row, ("status", "state"))
            if folder_name.casefold() not in folder.casefold() or not filename:
                continue
            key = filename.casefold()
            if key in records:
                raise NetmixDeliveryError(
                    f"Netmix upload history duplicated {filename}"
                )
            records[key] = status
        validate_history_statuses(records, expected_audio)
        return records

    def wait_for_completion(
        self, folder_name: str, expected_audio: frozenset[str], timeout_seconds: int
    ) -> dict[str, str]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            records = self.history_statuses(folder_name, expected_audio)
            if set(records) == set(expected_audio) and all(
                history_status_complete(status) for status in records.values()
            ):
                return records
            if time.monotonic() >= deadline:
                raise NetmixDeliveryError("Timed out verifying Netmix upload history")
            self._page.locator("#tb_gridviewuploads_toolbar_item_w2ui-reload").click()
            time.sleep(10)

    def close(self) -> None:
        try:
            self._context.storage_state(path=str(STORAGE_STATE_PATH))
            STORAGE_STATE_PATH.chmod(0o600)
            self._context.close()
            self._pw.stop()
        finally:
            if self._staging_root is not None and self._staging_root.is_dir():
                restore_portal_master(self._staging_root)


def deliver_netmix(
    ctx: ReleaseContext,
    dry_run: bool,
    logger: logging.Logger,
    *,
    live_confirmation: str | None = None,
    api_gateway: NetmixApiGateway | None = None,
    portal_gateway: NetmixPortalGateway | None = None,
    timeout_seconds: int = 24 * 60 * 60,
    native_folder_picker: bool = False,
) -> bool:
    owned = False
    try:
        plan = build_plan(package_root(ctx))
        logger.info("  Netmix package: %d file(s), %d audio", len(plan.files), len(plan.audio_names))
        if dry_run:
            logger.info(
                "  [DRY RUN] Netmix transport: %s",
                "API" if api_gateway and api_gateway.configured() else "portal fallback",
            )
            return True
        require_live_authorization(ctx, live_confirmation)
        gate_ok, detail = latest_workflow_gates(
            ctx, ("10 Final packaging", "15 Final metadata check")
        )
        if not gate_ok:
            raise NetmixDeliveryError(f"Netmix upload is blocked: {detail}")
        if api_gateway is not None and api_gateway.configured():
            result = api_gateway.deliver(plan)
            if result.completed:
                set_partner_status(ctx.specials_dir, "netmix", "delivered")
                logger.info("  ✓ Netmix API delivery verified")
                return True
            if not result.safe_to_fallback:
                raise NetmixDeliveryError(
                    "Netmix API failed after a remote mutation; refusing portal fallback"
                )
        if portal_gateway is None:
            portal_gateway = PlaywrightNetmixPortalGateway(
                native_folder_picker=native_folder_picker
            )
            owned = True
        portal_gateway.require_authenticated()
        statuses = portal_gateway.history_statuses(
            plan.package.name, plan.audio_names
        )
        resume_action = history_resume_action(statuses, plan.audio_names)
        if resume_action == "upload":
            portal_gateway.upload_package(plan.package)
            statuses = portal_gateway.wait_for_completion(
                plan.package.name, plan.audio_names, timeout_seconds
            )
        elif resume_action == "wait":
            logger.info(
                "  Netmix history contains an active exact-package upload; "
                "resuming verification without re-uploading"
            )
            statuses = portal_gateway.wait_for_completion(
                plan.package.name, plan.audio_names, timeout_seconds
            )
        else:
            logger.info(
                "  Netmix exact package was already complete in upload history"
            )
        receipt_path = receipt(ctx, "netmix", plan.files, {
            "transport": "portal-master-folder",
            "package": plan.package.name,
            "audio": len(plan.audio_names),
            "history_status_counts": {
                status: list(statuses.values()).count(status)
                for status in sorted(set(statuses.values()))
            },
        })
        set_partner_status(ctx.specials_dir, "netmix", "delivered")
        logger.info("  ✓ Netmix portal delivery verified: %s", receipt_path)
        return True
    except Exception as exc:
        logger.error("  ✗ Netmix delivery failed: %s", exc)
        return False
    finally:
        if owned and portal_gateway is not None:
            portal_gateway.close()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="API-priority Netmix delivery")
    parser.add_argument("--setup-auth", action="store_true")
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--part", type=int, choices=(1, 2))
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--full-month-content", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-live-release")
    parser.add_argument("--timeout-hours", type=float, default=24)
    parser.add_argument(
        "--native-folder-picker",
        action="store_true",
        help="Open the macOS folder picker for a supervised portal upload",
    )
    args = parser.parse_args(argv)
    gateway = None
    if args.setup_auth:
        try:
            gateway = PlaywrightNetmixPortalGateway(setup_auth=True)
            gateway.require_authenticated()
            print("Netmix portal authentication configured (identity redacted).")
            return 0
        finally:
            if gateway is not None:
                gateway.close()
    return 0 if deliver_netmix(
        context_from_cli_args(args), args.dry_run, logging.getLogger("netmix"),
        live_confirmation=args.confirm_live_release,
        timeout_seconds=max(1, int(args.timeout_hours * 3600)),
        native_folder_picker=args.native_folder_picker,
    ) else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(_main())
