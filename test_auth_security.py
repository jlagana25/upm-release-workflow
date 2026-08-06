import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import auth_manager
import domo_exports
import security_scan


class AuthSecurityTests(unittest.TestCase):
    @staticmethod
    def _domo_page_that_completes_sso():
        locator = Mock()
        locator.first = locator
        locator.click.side_effect = domo_exports.PlaywrightTimeoutError("not shown")
        page = Mock()
        page.locator.return_value = locator
        page.get_by_text.return_value = locator
        page.url = "https://example.domo.com/home"
        return page

    def test_scanner_detects_corporate_identity_without_echoing_value(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bad.py"
            source.write_text("identity = 'person@" + "umusic.com'\n", encoding="utf-8")
            findings = security_scan.scan_paths([source], root)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].rule, "corporate email identity")
            self.assertNotIn("person", findings[0].location)

    def test_scanner_rejects_auth_artifact_filename(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "domo_browser_profile" / "Default" / "Cookies"
            artifact.parent.mkdir(parents=True)
            artifact.touch()
            findings = security_scan.scan_paths([artifact], root)
            self.assertEqual(findings[0].rule, "per-user authentication artifact")

    def test_private_permission_helpers_remove_group_and_other_access(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw) / "private"
            auth_manager.secure_private_directory(directory)
            secret = directory / "state.xml"
            secret.write_text("local", encoding="utf-8")
            secret.chmod(0o644)
            auth_manager.secure_private_file(secret)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(secret.stat().st_mode & 0o777, 0o600)

    def test_keychain_enrollment_uses_direct_hidden_security_prompt(self):
        completed = Mock(returncode=0)
        with (
            patch.object(auth_manager.subprocess, "run", return_value=completed) as run,
            patch("builtins.print"),
        ):
            self.assertTrue(
                auth_manager._prompt_and_store_keychain_secret(
                    "test-service", "test value"
                )
            )
        argv = run.call_args.args[0]
        self.assertEqual(argv[-1], "-w")
        self.assertNotIn("input", run.call_args.kwargs)

    def test_domo_log_url_drops_auth_query_and_fragment(self):
        safe = domo_exports._safe_url_for_log(
            "https://login.example.invalid/path?code=sensitive#session"
        )
        self.assertEqual(safe, "https://login.example.invalid/path")

    def test_domo_authenticated_url_check_ignores_query_values(self):
        with patch.object(domo_exports, "DOMO_INSTANCE", "tenant.domo.com"):
            self.assertTrue(
                domo_exports._domo_session_is_authenticated(
                    "https://tenant.domo.com/page/home?ticket=private"
                )
            )
            self.assertFalse(
                domo_exports._domo_session_is_authenticated(
                    "https://tenant.domo.com/auth/index"
                )
            )

    def test_normal_domo_auth_uses_short_unattended_sso_window(self):
        page = self._domo_page_that_completes_sso()
        with (
            patch.object(domo_exports.time, "sleep"),
            patch.object(
                domo_exports,
                "_attempt_keychain_microsoft_login",
                return_value=False,
            ),
        ):
            domo_exports._authenticate(page, Mock())
        timeout = page.wait_for_function.call_args.kwargs["timeout"]
        self.assertLessEqual(timeout, domo_exports.SILENT_LOGIN_TIMEOUT)
        self.assertGreater(
            timeout,
            domo_exports.SILENT_LOGIN_TIMEOUT - 1_000,
        )

    def test_domo_setup_explicitly_enables_interactive_enrollment_window(self):
        page = self._domo_page_that_completes_sso()
        with (
            patch.object(domo_exports.time, "sleep"),
            patch.object(
                domo_exports,
                "_attempt_keychain_microsoft_login",
                return_value=False,
            ),
        ):
            domo_exports._authenticate(page, Mock(), allow_interactive=True)
        timeout = page.wait_for_function.call_args.kwargs["timeout"]
        self.assertLessEqual(timeout, domo_exports.LOGIN_TIMEOUT)
        self.assertGreater(
            timeout,
            domo_exports.LOGIN_TIMEOUT - 1_000,
        )

    def test_keychain_domo_login_selects_structural_account_and_password(self):
        account = Mock()
        account.first = account
        account.is_visible.return_value = True
        password_field = Mock()
        password_field.first = password_field
        password_field.is_visible.return_value = True
        submit = Mock()
        submit.first = submit
        submit.is_visible.return_value = True

        page = Mock()
        page.url = "https://login.microsoftonline.com/tenant/saml2"

        def locator(selector):
            if selector.startswith("#tilesHolder"):
                return account
            if "passwd" in selector:
                return password_field
            return submit

        page.locator.side_effect = locator
        submit.click.side_effect = lambda: setattr(
            page, "url", f"https://{domo_exports.DOMO_INSTANCE}/home"
        )
        logger = Mock()
        with patch.object(
            domo_exports,
            "load_domo_keychain_credentials",
            return_value=("test-user", "test-value"),
        ):
            self.assertTrue(
                domo_exports._attempt_keychain_microsoft_login(page, logger)
            )
        account.click.assert_called_once_with()
        password_field.fill.assert_called_once_with("test-value")
        rendered_logs = " ".join(
            str(arg)
            for call in logger.method_calls
            for arg in call.args
        )
        self.assertNotIn("test-user", rendered_logs)
        self.assertNotIn("test-value", rendered_logs)

    def test_auth_status_redacts_unisync_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            profile = root / "profile"
            profile.mkdir(mode=0o700)
            (profile / "state").touch()
            xml = root / "UniSync.xml"
            xml.write_text(
                '<userPrefs loginname="private-identity" cachePath="x"/>',
                encoding="utf-8",
            )
            xml.chmod(0o600)
            with (
                patch.object(auth_manager, "DOMO_PROFILE_DIR", profile),
                patch.object(auth_manager, "UNISYNC_XML_PATH", xml),
            ):
                rendered = str(auth_manager.auth_status())
            self.assertNotIn("private-identity", rendered)
            self.assertIn("configured", rendered)


if __name__ == "__main__":
    unittest.main()
