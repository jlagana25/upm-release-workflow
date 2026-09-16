import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

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

    def test_keychain_enrollment_keeps_secret_out_of_argv(self):
        with (
            patch.object(auth_manager.pty, "fork", return_value=(123, 9)),
            patch.object(auth_manager.select, "select", side_effect=[([9], [], []), ([9], [], []), ([9], [], [])]),
            patch.object(
                auth_manager.os,
                "read",
                side_effect=[
                    b"password data for new item:",
                    b"retype password for new item:",
                    OSError(),
                ],
            ),
            patch.object(auth_manager.os, "write") as write,
            patch.object(auth_manager.os, "waitpid", return_value=(123, 0)),
            patch.object(auth_manager.os, "close"),
            patch.object(auth_manager, "_read_keychain_secret", return_value="private"),
        ):
            self.assertTrue(
                auth_manager._store_keychain_secret("test-service", "private")
            )
        self.assertEqual(
            write.call_args_list,
            [call(9, b"private\n"), call(9, b"private\n")],
        )

    def test_keychain_enrollment_rejects_changed_round_trip_value(self):
        with (
            patch.object(auth_manager.pty, "fork", return_value=(123, 9)),
            patch.object(auth_manager.select, "select", side_effect=[([9], [], []), ([9], [], []), ([9], [], [])]),
            patch.object(
                auth_manager.os,
                "read",
                side_effect=[
                    b"password data for new item:",
                    b"retype password for new item:",
                    OSError(),
                ],
            ),
            patch.object(auth_manager.os, "write"),
            patch.object(auth_manager.os, "waitpid", return_value=(123, 0)),
            patch.object(auth_manager.os, "close"),
            patch.object(auth_manager, "_read_keychain_secret", return_value="changed"),
        ):
            self.assertFalse(
                auth_manager._store_keychain_secret("test-service", "private")
            )

    def test_monday_enrollment_rejects_and_removes_invalid_token(self):
        logger = Mock()
        with (
            patch.object(auth_manager.sys.stdin, "isatty", return_value=True),
            patch.object(auth_manager, "_prompt_hidden_secret", return_value="bad-value"),
            patch("monday_sync.MondayClient.validate_auth") as validate,
            patch.object(auth_manager, "_store_native_keychain_secret") as store,
        ):
            from monday_sync import MondayAuthorizationError
            validate.side_effect = MondayAuthorizationError("rejected", status_code=401)
            self.assertFalse(auth_manager.enroll_monday_keychain(logger))
        store.assert_not_called()

    def test_domo_api_validation_uses_basic_auth_without_query_secrets(self):
        response = Mock(status_code=200)
        response.json.return_value = {"access_token": "short-lived"}
        with patch("requests.get", return_value=response) as get:
            self.assertEqual(
                auth_manager._validate_domo_api_credentials(
                    "private-client", "private-secret"
                ),
                (True, "accepted"),
            )
        _, kwargs = get.call_args
        self.assertEqual(kwargs["auth"], ("private-client", "private-secret"))
        self.assertNotIn("client_id", kwargs["params"])
        self.assertNotIn("client_secret", kwargs["params"])

    def test_domo_api_enrollment_validates_before_keychain_write(self):
        logger = Mock()
        prompts = iter(("private-client", "private-secret"))
        with (
            patch.object(auth_manager.sys.stdin, "isatty", return_value=True),
            patch.object(
                auth_manager,
                "_prompt_hidden_secret",
                side_effect=lambda _description: next(prompts),
            ),
            patch.object(
                auth_manager,
                "_validate_domo_api_credentials",
                return_value=(False, "http-401"),
            ),
            patch.object(auth_manager, "_store_native_keychain_secret") as store,
        ):
            self.assertFalse(auth_manager.enroll_domo_api_keychain(logger))
        store.assert_not_called()

    def test_domo_api_enrollment_stores_both_values_without_logging_them(self):
        logger = Mock()
        client_id = "private-client"
        credential_value = "private-" + "secret"
        prompts = iter((client_id, credential_value))
        with (
            patch.object(auth_manager.sys.stdin, "isatty", return_value=True),
            patch.object(
                auth_manager,
                "_prompt_hidden_secret",
                side_effect=lambda _description: next(prompts),
            ),
            patch.object(
                auth_manager,
                "_validate_domo_api_credentials",
                return_value=(True, "accepted"),
            ),
            patch.object(
                auth_manager, "_read_native_keychain_secret", return_value=None
            ),
            patch.object(
                auth_manager, "_store_native_keychain_secret", return_value=True
            ) as store,
            patch.object(
                auth_manager, "domo_api_keychain_configured", return_value=True
            ),
        ):
            self.assertTrue(auth_manager.enroll_domo_api_keychain(logger))
        self.assertEqual(
            store.call_args_list,
            [
                call(auth_manager.DOMO_API_CLIENT_ID_SERVICE, client_id),
            call(auth_manager.DOMO_API_CLIENT_SECRET_SERVICE, credential_value),
            ],
        )
        rendered = " ".join(str(item) for item in logger.method_calls)
        self.assertNotIn(client_id, rendered)
        self.assertNotIn(credential_value, rendered)

    def test_monday_enrollment_accepts_valid_token_without_logging_it(self):
        logger = Mock()
        token = "private-test-value"
        with (
            patch.object(auth_manager.sys.stdin, "isatty", return_value=True),
            patch.object(auth_manager, "_prompt_hidden_secret", return_value=token),
            patch.object(auth_manager, "_store_native_keychain_secret", return_value=True) as store,
            patch("monday_sync.MondayClient.validate_auth") as validate,
        ):
            self.assertTrue(auth_manager.enroll_monday_keychain(logger))
        validate.assert_called_once_with()
        store.assert_called_once_with(auth_manager.MONDAY_KEYCHAIN_TOKEN_SERVICE, token)
        rendered = " ".join(str(call) for call in logger.method_calls)
        self.assertNotIn(token, rendered)

    def test_monday_enrollment_keeps_token_on_permission_failure(self):
        logger = Mock()
        with (
            patch.object(auth_manager.sys.stdin, "isatty", return_value=True),
            patch.object(auth_manager, "_prompt_hidden_secret", return_value="stored-value"),
            patch("monday_sync.MondayClient.validate_auth") as validate,
            patch.object(auth_manager, "_store_native_keychain_secret") as store,
        ):
            from monday_sync import MondayAuthorizationError
            validate.side_effect = MondayAuthorizationError("forbidden", status_code=403)
            self.assertFalse(auth_manager.enroll_monday_keychain(logger))
        store.assert_not_called()

    def test_bmat_enrollment_atomically_stores_both_values(self):
        logger = Mock()
        username = "private-bmat-user"
        credential_value = "private-bmat-" + "value"
        prompts = iter((username, credential_value))
        with (
            patch.object(auth_manager.sys.stdin, "isatty", return_value=True),
            patch.object(
                auth_manager,
                "_prompt_hidden_secret",
                side_effect=lambda _description: next(prompts),
            ),
            patch.object(
                auth_manager, "_read_native_keychain_secret", return_value=None
            ),
            patch.object(
                auth_manager, "_store_native_keychain_secret", return_value=True
            ) as store,
            patch.object(
                auth_manager,
                "load_bmat_sftp_credentials",
                return_value=(username, credential_value),
            ),
        ):
            self.assertTrue(auth_manager.enroll_bmat_sftp_keychain(logger))
        self.assertEqual(
            store.call_args_list,
            [
                call(auth_manager.BMAT_SFTP_USERNAME_SERVICE, username),
                call(auth_manager.BMAT_SFTP_PASSWORD_SERVICE, credential_value),
            ],
        )
        rendered = " ".join(str(item) for item in logger.method_calls)
        self.assertNotIn(username, rendered)
        self.assertNotIn(credential_value, rendered)

    def test_tunesat_enrollment_atomically_stores_both_values(self):
        logger = Mock()
        username = "private-tunesat-user"
        credential_value = "private-tunesat-" + "value"
        prompts = iter((username, credential_value))
        with (
            patch.object(auth_manager.sys.stdin, "isatty", return_value=True),
            patch.object(
                auth_manager,
                "_prompt_hidden_secret",
                side_effect=lambda _description: next(prompts),
            ),
            patch.object(
                auth_manager, "_read_native_keychain_secret", return_value=None
            ),
            patch.object(
                auth_manager, "_store_native_keychain_secret", return_value=True
            ) as store,
            patch.object(
                auth_manager,
                "load_tunesat_sftp_credentials",
                return_value=(username, credential_value),
            ),
        ):
            self.assertTrue(auth_manager.enroll_tunesat_sftp_keychain(logger))
        self.assertEqual(
            store.call_args_list,
            [
                call(auth_manager.TUNESAT_SFTP_USERNAME_SERVICE, username),
                call(auth_manager.TUNESAT_SFTP_PASSWORD_SERVICE, credential_value),
            ],
        )
        rendered = " ".join(str(item) for item in logger.method_calls)
        self.assertNotIn(username, rendered)
        self.assertNotIn(credential_value, rendered)

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
            patch.object(domo_exports, "_verify_domo_workspace"),
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
            patch.object(domo_exports, "_verify_domo_workspace"),
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
