"""Small fail-closed helpers for Keychain-backed web portal sign-in.

Credential values exist only in the caller's memory and are never returned in
errors, logs, URLs, process arguments, environment variables, or files.
"""

from __future__ import annotations

import time
from collections.abc import Callable


USERNAME_SELECTOR = (
    "input[type=email]:visible, input[name*=email i]:visible, "
    "input[name*=user i]:visible, input[id*=email i]:visible, "
    "input[id*=user i]:visible, input[type=text]:visible"
)
PASSWORD_SELECTOR = "input[type=password]:visible"
SUBMIT_SELECTOR = "button[type=submit]:visible, input[type=submit]:visible"


class PortalAuthenticationError(RuntimeError):
    pass


def _unique_visible(page, selector: str, description: str):
    locator = page.locator(selector)
    count = locator.count()
    if count > 1:
        raise PortalAuthenticationError(
            f"Portal presented multiple visible {description} controls"
        )
    return locator.first if count == 1 else None


def _submit(page) -> None:
    submit = page.locator(SUBMIT_SELECTOR)
    if submit.count() == 1:
        submit.first.click()
        return
    for label in ("Sign in", "Sign In", "Log in", "Login", "Continue", "Next"):
        candidate = page.get_by_role("button", name=label, exact=True)
        if candidate.count() == 1:
            candidate.click()
            return
    raise PortalAuthenticationError("Portal sign-in action was not unique")


def attempt_keychain_login(
    page,
    credentials: tuple[str, str] | None,
    *,
    ready: Callable[[], bool],
    timeout_seconds: float = 60,
) -> bool:
    """Fill a one- or two-stage login form and verify protected portal UI."""
    if ready():
        return True
    if not credentials:
        return False
    username, password = credentials
    try:
        # Some identity pages ask for the username before rendering a password
        # field. Permit exactly one such transition.
        password_field = _unique_visible(page, PASSWORD_SELECTOR, "password")
        username_field = _unique_visible(page, USERNAME_SELECTOR, "username")
        # A freshly loaded application shell may expose neither field yet.
        # Never interpret that transient empty shell as a two-stage username
        # form and click an unrelated button. Wait briefly for either a real
        # credential control or the protected ready state.
        if password_field is None and username_field is None:
            render_deadline = time.monotonic() + min(timeout_seconds, 15)
            while time.monotonic() < render_deadline:
                if ready():
                    return True
                password_field = _unique_visible(
                    page, PASSWORD_SELECTOR, "password"
                )
                username_field = _unique_visible(
                    page, USERNAME_SELECTOR, "username"
                )
                if password_field is not None or username_field is not None:
                    break
                time.sleep(0.25)
        if username_field is not None:
            username_field.fill(username)
        if password_field is None:
            if username_field is None:
                return False
            _submit(page)
            deadline = time.monotonic() + min(timeout_seconds, 30)
            while time.monotonic() < deadline:
                password_field = _unique_visible(
                    page, PASSWORD_SELECTOR, "password"
                )
                if password_field is not None or ready():
                    break
                time.sleep(0.25)
        if ready():
            return True
        if password_field is None:
            return False
        password_field.fill(password)
        _submit(page)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if ready():
                return True
            time.sleep(0.25)
        return False
    finally:
        username = password = ""
        credentials = None
