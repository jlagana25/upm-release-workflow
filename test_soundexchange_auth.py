import unittest
from unittest.mock import Mock, patch

import soundexchange_delivery


class SoundExchangeAuthenticationTests(unittest.TestCase):
    def test_cookie_dialog_is_closed_before_keychain_login(self):
        gateway = soundexchange_delivery.PlaywrightSoundExchangeGateway.__new__(
            soundexchange_delivery.PlaywrightSoundExchangeGateway
        )
        page = Mock()
        cookie_close = Mock()
        cookie_close.count.return_value = 1
        cookie_close.is_visible.return_value = True
        page.get_by_text.side_effect = lambda text, **_kwargs: (
            cookie_close if text == "Close this dialog" else Mock(count=lambda: 0)
        )
        password = Mock()
        password.count.return_value = 1
        password.first = password
        page.locator.return_value = password
        gateway._page = page
        gateway._interactive_login = False

        with (
            patch(
                "auth_manager.load_soundexchange_credentials",
                return_value=("private-user", "private-value"),
            ),
            patch("portal_auth.attempt_keychain_login", return_value=True) as login,
        ):
            gateway.require_authenticated()

        cookie_close.click.assert_called_once_with()
        password.wait_for.assert_called_once_with(state="visible", timeout=15_000)
        login.assert_called_once()

    def test_registrant_waits_for_async_table_before_exact_checks(self):
        gateway = soundexchange_delivery.PlaywrightSoundExchangeGateway.__new__(
            soundexchange_delivery.PlaywrightSoundExchangeGateway
        )
        page = Mock()
        row = Mock()
        row.first = row
        row.count.return_value = 1
        registrant = soundexchange_delivery.REGISTRANTS[0]
        row.inner_text.return_value = (
            f"{registrant.name} {registrant.registrant_id} {registrant.rights_owner}"
        )
        link = Mock()
        link.count.return_value = 1
        row.get_by_text.return_value = link
        page.locator.side_effect = (row, Mock())
        gateway._page = page

        gateway.select_registrant(registrant)

        row.wait_for.assert_called_once_with(state="visible", timeout=15_000)
        link.click.assert_called_once_with()

    def test_bulk_import_sets_exact_input_without_opening_native_chooser(self):
        gateway = soundexchange_delivery.PlaywrightSoundExchangeGateway.__new__(
            soundexchange_delivery.PlaywrightSoundExchangeGateway
        )
        page = Mock()
        picker = Mock()
        picker.count.return_value = 1
        page.locator.return_value = picker
        gateway._page = page

        workbook = soundexchange_delivery.Path("/private/tmp/ingest.xlsx")
        gateway.bulk_import(workbook)

        picker.set_input_files.assert_called_once_with(str(workbook))
        page.get_by_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
