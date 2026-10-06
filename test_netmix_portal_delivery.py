from __future__ import annotations

import csv
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import netmix_portal_delivery as netmix


class FakePortal:
    def __init__(self, statuses=None):
        self.uploaded = 0
        self.statuses = dict(statuses or {})
        self.waited = 0

    def require_authenticated(self):
        pass

    def history_statuses(self, _folder, _expected):
        return dict(self.statuses)

    def upload_package(self, _package):
        self.uploaded += 1

    def wait_for_completion(self, _folder, expected, _timeout):
        self.waited += 1
        return {name: "Complete" for name in expected}

    def close(self):
        pass


class FakeApi:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def configured(self):
        return True

    def deliver(self, _plan):
        self.calls += 1
        return self.result


class NetmixPortalDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package = self.root / "Netmix"
        album = self.package / "Music" / "Label" / "Album"
        album.mkdir(parents=True)
        (album / "one.wav").write_bytes(b"one")
        (album / "two.wav").write_bytes(b"two")
        (album / "cover.jpg").write_bytes(b"cover")
        metadata = self.package / "Metadata" / "metadata.csv"
        metadata.parent.mkdir(parents=True)
        with metadata.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["Filename", "Title"])
            writer.writeheader()
            writer.writerow({"Filename": "one.wav", "Title": "One"})
            writer.writerow({"Filename": "two.wav", "Title": "Two"})
        self.ctx = SimpleNamespace(
            release_id="UPM20260912",
            specials_dir=self.root,
            partner_folder_name=lambda _name: self.package.name,
        )
        self.log = logging.getLogger("test_netmix")

    def tearDown(self):
        self.temp.cleanup()

    def test_plan_requires_exact_metadata_audio_and_one_cover(self):
        plan = netmix.build_plan(self.package)
        self.assertEqual(len(plan.audio_names), 2)
        (self.package / "Music" / "Label" / "Album" / "extra.png").write_bytes(b"x")
        with self.assertRaisesRegex(netmix.NetmixDeliveryError, "exactly one cover"):
            netmix.build_plan(self.package)

    def test_portal_master_removes_packaging_wrappers_and_finder_files(self):
        (self.package / ".DS_Store").write_bytes(b"finder")
        (self.package / "Metadata" / ".DS_Store").write_bytes(b"finder")
        master, staging_root = netmix.prepare_portal_master(self.package)
        try:
            self.assertTrue((master / "metadata.csv").is_file())
            self.assertTrue((master / "Album" / "one.wav").is_file())
            self.assertFalse((master / "Label").exists())
            self.assertFalse((master / "Music").exists())
            self.assertFalse((master / "Metadata").exists())
            self.assertEqual(len([p for p in master.rglob("*") if p.is_file()]), 4)
        finally:
            netmix.restore_portal_master(staging_root)
        self.assertTrue((self.package / "Metadata" / "metadata.csv").is_file())
        self.assertTrue((self.package / "Music" / "Label" / "Album" / "one.wav").is_file())

    def test_api_is_priority_and_skips_portal_after_success(self):
        api = FakeApi(netmix.NetmixApiResult(True, False, "done"))
        portal = Mock()
        with (
            patch.object(netmix, "package_root", return_value=self.package),
            patch.object(netmix, "latest_workflow_gates", return_value=(True, "ok")),
            patch.object(netmix, "set_partner_status") as state,
        ):
            result = netmix.deliver_netmix(
                self.ctx, False, self.log,
                live_confirmation=self.ctx.release_id,
                api_gateway=api,
                portal_gateway=portal,
            )
        self.assertTrue(result)
        self.assertEqual(api.calls, 1)
        portal.upload_package.assert_not_called()
        state.assert_called_once_with(self.ctx.specials_dir, "netmix", "delivered")

    def test_api_uncertain_failure_refuses_duplicate_portal_upload(self):
        api = FakeApi(netmix.NetmixApiResult(False, False, "uncertain"))
        portal = Mock()
        with (
            patch.object(netmix, "package_root", return_value=self.package),
            patch.object(netmix, "latest_workflow_gates", return_value=(True, "ok")),
        ):
            result = netmix.deliver_netmix(
                self.ctx, False, self.log,
                live_confirmation=self.ctx.release_id,
                api_gateway=api,
                portal_gateway=portal,
            )
        self.assertFalse(result)
        portal.upload_package.assert_not_called()

    def test_safe_api_failure_uses_portal_and_writes_receipt(self):
        api = FakeApi(netmix.NetmixApiResult(False, True, "unavailable"))
        portal = FakePortal()
        with (
            patch.object(netmix, "package_root", return_value=self.package),
            patch.object(netmix, "latest_workflow_gates", return_value=(True, "ok")),
        ):
            result = netmix.deliver_netmix(
                self.ctx, False, self.log,
                live_confirmation=self.ctx.release_id,
                api_gateway=api,
                portal_gateway=portal,
                timeout_seconds=1,
            )
        self.assertTrue(result)
        self.assertEqual(portal.uploaded, 1)
        self.assertTrue((self.root / "_WORKFLOW" / "netmix_delivery_receipt.json").is_file())

    def test_completed_portal_history_writes_receipt_without_reupload(self):
        portal = FakePortal({"one.wav": "Complete", "two.wav": "Accepted"})
        with (
            patch.object(netmix, "package_root", return_value=self.package),
            patch.object(netmix, "latest_workflow_gates", return_value=(True, "ok")),
        ):
            result = netmix.deliver_netmix(
                self.ctx, False, self.log,
                live_confirmation=self.ctx.release_id,
                portal_gateway=portal,
                timeout_seconds=1,
            )
        self.assertTrue(result)
        self.assertEqual(portal.uploaded, 0)
        self.assertEqual(portal.waited, 0)
        self.assertTrue((self.root / "_WORKFLOW" / "netmix_delivery_receipt.json").is_file())

    def test_active_portal_history_resumes_verification_without_reupload(self):
        portal = FakePortal({"one.wav": "Complete", "two.wav": "Processing"})
        with (
            patch.object(netmix, "package_root", return_value=self.package),
            patch.object(netmix, "latest_workflow_gates", return_value=(True, "ok")),
        ):
            result = netmix.deliver_netmix(
                self.ctx, False, self.log,
                live_confirmation=self.ctx.release_id,
                portal_gateway=portal,
                timeout_seconds=1,
            )
        self.assertTrue(result)
        self.assertEqual(portal.uploaded, 0)
        self.assertEqual(portal.waited, 1)

    def test_completed_partial_history_refuses_duplicate_upload(self):
        portal = FakePortal({"one.wav": "Complete"})
        with (
            patch.object(netmix, "package_root", return_value=self.package),
            patch.object(netmix, "latest_workflow_gates", return_value=(True, "ok")),
        ):
            result = netmix.deliver_netmix(
                self.ctx, False, self.log,
                live_confirmation=self.ctx.release_id,
                portal_gateway=portal,
                timeout_seconds=1,
            )
        self.assertFalse(result)
        self.assertEqual(portal.uploaded, 0)
        self.assertEqual(portal.waited, 0)

    def test_history_action_rejects_unknown_status(self):
        with self.assertRaisesRegex(netmix.NetmixDeliveryError, "unknown"):
            netmix.history_resume_action(
                {"one.wav": "Mystery"}, frozenset({"one.wav", "two.wav"})
            )

    def test_expired_history_session_recovers_from_keychain(self):
        gateway = object.__new__(netmix.PlaywrightNetmixPortalGateway)
        gateway._page = Mock()
        gateway._setup_auth = False
        gateway._probe_authenticated = Mock(side_effect=(False, True))
        gateway._open_upload_surface = Mock()
        with (
            patch("auth_manager.load_netmix_credentials", return_value=("user", "secret")),
            patch("portal_auth.attempt_keychain_login", return_value=True) as login,
        ):
            gateway.require_authenticated()
        login.assert_called_once()
        gateway._page.goto.assert_called_once_with(
            netmix.PORTAL_URL, wait_until="domcontentloaded"
        )
        gateway._open_upload_surface.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
