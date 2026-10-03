#!/usr/bin/env python3
"""Fail-closed SoundMouse website authentication for metadata processing.

The retained browser profile is private per macOS user.  A fresh session may
recover from the separate workflow-owned Keychain pair, but credentials never
enter argv, URLs, logs, reports, screenshots, or repository files.
"""

from __future__ import annotations

import argparse
import logging

from config import PRIVATE_STATE_DIR


# Always enter through the service root. SoundMouse establishes/recovers the
# authenticated workspace there, then redirects the retained UPPM session to
# the protected Music page. Deep-linking before that redirect can strand a
# successful MFA flow on the root while the checker waits on the old page.
PORTAL_URL = "https://app.soundmouse.com/"
PROFILE_DIR = PRIVATE_STATE_DIR / "soundmouse_web_profile"


class SoundMouseWebAuthenticationError(RuntimeError):
    pass


def _ready(page) -> bool:
    """Require protected UPPM Music UI, not merely a SoundMouse hostname."""
    return bool(
        not page.locator("input[type=password]:visible").count()
        and page.get_by_role("heading", name="Music", exact=True).count()
        and page.get_by_text("UPPM", exact=True).count()
    )


def require_authenticated(page, *, allow_interactive: bool = False) -> None:
    """Recover a fresh SoundMouse web session from Keychain and verify it."""
    if _ready(page):
        return
    # The application shell can redirect to `/` before the Password/SSO tabs
    # and credential form finish rendering. Wait for the exact password tab
    # and field so the generic login helper never examines an empty shell.
    password_tab = page.get_by_text("Use Password", exact=True)
    if password_tab.count() == 1 and password_tab.is_visible():
        password_tab.click()
    try:
        page.locator("input[type=password]:visible").first.wait_for(
            state="visible", timeout=15_000
        )
    except Exception:
        pass
    from auth_manager import load_soundmouse_credentials
    from portal_auth import PortalAuthenticationError, attempt_keychain_login

    try:
        if attempt_keychain_login(
            page,
            load_soundmouse_credentials(),
            ready=lambda: _ready(page),
        ):
            return
    except PortalAuthenticationError as exc:
        raise SoundMouseWebAuthenticationError(
            f"SoundMouse website authentication failed closed: {exc}"
        ) from exc

    if allow_interactive:
        try:
            page.get_by_role("heading", name="Music", exact=True).wait_for(
                state="visible", timeout=300_000
            )
        except Exception as exc:
            raise SoundMouseWebAuthenticationError(
                "SoundMouse interactive sign-in did not complete within five minutes"
            ) from exc
        if _ready(page):
            return
    raise SoundMouseWebAuthenticationError(
        "SoundMouse website session is not authenticated; run "
        "auth_manager.py --enroll-soundmouse-keychain"
    )


def check_authentication(*, allow_interactive: bool = False) -> bool:
    """Launch the private retained profile and prove the protected UI opens."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SoundMouseWebAuthenticationError(
            "SoundMouse website authentication requires Playwright"
        ) from exc

    from auth_manager import private_creation_umask, secure_private_directory

    secure_private_directory(PROFILE_DIR, recursive=True)
    with sync_playwright() as playwright:
        with private_creation_umask():
            context = playwright.chromium.launch_persistent_context(
                str(PROFILE_DIR), channel="chrome", headless=False
            )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(PORTAL_URL, wait_until="domcontentloaded")
            require_authenticated(page, allow_interactive=allow_interactive)
            return True
        finally:
            context.close()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate unattended SoundMouse website authentication"
    )
    parser.add_argument(
        "--interactive-login",
        action="store_true",
        help="allow one bounded manual sign-in when policy requires MFA",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        check_authentication(allow_interactive=args.interactive_login)
    except SoundMouseWebAuthenticationError as exc:
        logging.error("SoundMouse website authentication failed: %s", exc)
        return 1
    logging.info("SoundMouse website authentication verified (identity redacted).")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
