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
        with patch.object(domo_exports.time, "sleep"):
            domo_exports._authenticate(page, Mock())
        self.assertEqual(
            page.wait_for_function.call_args.kwargs["timeout"],
            domo_exports.SILENT_LOGIN_TIMEOUT,
        )

    def test_domo_setup_explicitly_enables_interactive_enrollment_window(self):
        page = self._domo_page_that_completes_sso()
        with patch.object(domo_exports.time, "sleep"):
            domo_exports._authenticate(page, Mock(), allow_interactive=True)
        self.assertEqual(
            page.wait_for_function.call_args.kwargs["timeout"],
            domo_exports.LOGIN_TIMEOUT,
        )

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
