import unittest
from unittest.mock import Mock, patch

import soundmouse_web_auth


class SoundMouseWebAuthenticationTests(unittest.TestCase):
    def test_ready_requires_music_heading_and_up_pm_workspace(self):
        page = Mock()
        page.locator.return_value.count.return_value = 0
        page.get_by_role.return_value.count.return_value = 1
        page.get_by_text.return_value.count.return_value = 1
        self.assertTrue(soundmouse_web_auth._ready(page))

    def test_fresh_session_uses_keychain_and_requires_protected_ui(self):
        page = Mock()
        password_tab = Mock()
        password_tab.count.return_value = 1
        password_tab.is_visible.return_value = True
        page.get_by_text.return_value = password_tab
        password = Mock()
        password.first = password
        page.locator.return_value = password
        with (
            patch.object(soundmouse_web_auth, "_ready", side_effect=(False, True)),
            patch(
                "auth_manager.load_soundmouse_credentials",
                return_value=("private-user", "private-value"),
            ),
            patch("portal_auth.attempt_keychain_login", return_value=True) as login,
        ):
            soundmouse_web_auth.require_authenticated(page)
        self.assertEqual(login.call_count, 1)
        password_tab.click.assert_called_once_with()
        password.wait_for.assert_called_once_with(state="visible", timeout=15_000)

    def test_missing_keychain_fails_with_redacted_setup_command(self):
        page = Mock()
        page.get_by_text.return_value.count.return_value = 0
        page.locator.return_value.first.wait_for.side_effect = TimeoutError
        with (
            patch.object(soundmouse_web_auth, "_ready", return_value=False),
            patch("auth_manager.load_soundmouse_credentials", return_value=None),
            patch("portal_auth.attempt_keychain_login", return_value=False),
        ):
            with self.assertRaisesRegex(
                soundmouse_web_auth.SoundMouseWebAuthenticationError,
                "enroll-soundmouse-keychain",
            ):
                soundmouse_web_auth.require_authenticated(page)


if __name__ == "__main__":
    unittest.main()
