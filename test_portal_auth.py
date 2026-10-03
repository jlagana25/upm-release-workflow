from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import portal_auth


class PortalAuthTests(unittest.TestCase):
    @staticmethod
    def _page(*, username_count=1, password_count=1, submit_count=1):
        username = Mock()
        username.count.return_value = username_count
        username.first = username
        password = Mock()
        password.count.return_value = password_count
        password.first = password
        submit = Mock()
        submit.count.return_value = submit_count
        submit.first = submit
        page = Mock()

        def locator(selector):
            if selector == portal_auth.USERNAME_SELECTOR:
                return username
            if selector == portal_auth.PASSWORD_SELECTOR:
                return password
            if selector == portal_auth.SUBMIT_SELECTOR:
                return submit
            raise AssertionError(selector)

        page.locator.side_effect = locator
        return page, username, password, submit

    def test_credentials_fill_once_and_require_protected_ready_state(self):
        page, username, password, submit = self._page()
        ready = Mock(side_effect=(False, False, True))
        with patch.object(portal_auth.time, "sleep"):
            self.assertTrue(
                portal_auth.attempt_keychain_login(
                    page, ("private-user", "private-value"), ready=ready
                )
            )
        username.fill.assert_called_once_with("private-user")
        password.fill.assert_called_once_with("private-value")
        submit.click.assert_called_once_with()

    def test_missing_credentials_does_not_touch_form(self):
        page, username, password, submit = self._page()
        self.assertFalse(
            portal_auth.attempt_keychain_login(page, None, ready=lambda: False)
        )
        username.fill.assert_not_called()
        password.fill.assert_not_called()
        submit.click.assert_not_called()

    def test_ambiguous_password_form_fails_closed(self):
        page, _username, _password, _submit = self._page(password_count=2)
        with self.assertRaisesRegex(
            portal_auth.PortalAuthenticationError, "multiple visible password"
        ):
            portal_auth.attempt_keychain_login(
                page, ("private-user", "private-value"), ready=lambda: False
            )

    def test_empty_application_shell_waits_for_form_without_clicking(self):
        page, username, password, submit = self._page(
            username_count=0, password_count=0
        )
        username.count.side_effect = (0, 1)
        password.count.side_effect = (0, 1)
        ready = Mock(side_effect=(False, False, False, True))
        with patch.object(portal_auth.time, "sleep"):
            self.assertTrue(
                portal_auth.attempt_keychain_login(
                    page, ("private-user", "private-value"), ready=ready
                )
            )
        username.fill.assert_called_once_with("private-user")
        password.fill.assert_called_once_with("private-value")
        submit.click.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
