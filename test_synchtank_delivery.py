from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import synchtank_delivery as sd


class FakeGateway:
    def __init__(self, initial: dict[str, int] | None = None) -> None:
        self.objects = dict(initial or {})
        self.events: list[tuple[str, str]] = []

    def list_objects(self) -> dict[str, int]:
        return dict(self.objects)

    def upload(self, local_path: Path, key: str) -> None:
        self.events.append(("upload", key))
        self.objects[key] = local_path.stat().st_size

    def object_size(self, key: str) -> int | None:
        return self.objects.get(key)

    def put_empty(self, key: str) -> None:
        self.events.append(("put_empty", key))
        self.objects[key] = 0


def _context(root: Path):
    return SimpleNamespace(
        specials_dir=root,
        release_id="UPM20260912",
        partner_folder_name=lambda partner: f"Universal Production Music Test - {partner}",
    )


class SynchTankDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ctx = _context(self.root)
        self.package = sd.package_root(self.ctx)
        (self.package / "Music" / "Label").mkdir(parents=True)
        (self.package / "Covers").mkdir()
        (self.package / "Metadata").mkdir()
        (self.package / "Music" / "Label" / "track.wav").write_bytes(b"audio")
        (self.package / "Covers" / "cover.jpg").write_bytes(b"cover")
        (self.package / "Metadata" / "metadata.csv").write_bytes(b"metadata")
        self.logger = logging.getLogger("test_synchtank")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @patch("synchtank_delivery.workflow_gate_passed", return_value=(True, "ready"))
    @patch("synchtank_delivery.set_partner_status")
    def test_uploads_under_package_prefix_and_marker_is_last(self, set_status, _gate) -> None:
        gateway = FakeGateway({"Historical Delivery/old.wav": 10})
        self.assertTrue(sd.deliver_synchtank(self.ctx, False, self.logger, gateway=gateway))
        uploaded_keys = [key for action, key in gateway.events if action == "upload"]
        prefix = "Universal Production Music Test - SynchTank/"
        self.assertEqual(
            uploaded_keys,
            [
                prefix + "Covers/cover.jpg",
                prefix + "Metadata/metadata.csv",
                prefix + "Music/Label/track.wav",
            ],
        )
        self.assertEqual(
            gateway.events[-1], ("put_empty", prefix + "delivery.complete")
        )
        self.assertIn("Historical Delivery/old.wav", gateway.objects)
        set_status.assert_called_once_with(self.root, "synchtank", "delivered")
        self.assertTrue((self.root / "_WORKFLOW" / "synchtank_delivery_receipt.json").is_file())

    @patch("synchtank_delivery.workflow_gate_passed", return_value=(True, "ready"))
    @patch("synchtank_delivery.set_partner_status")
    def test_exact_completed_delivery_is_idempotent(self, set_status, _gate) -> None:
        manifest = sd.collect_package(self.package)
        prefix = sd.package_prefix(self.ctx)
        initial = {prefix + item.key: item.size for item in manifest}
        initial[prefix + sd.COMPLETION_MARKER] = 0
        initial["Historical Delivery/old.wav"] = 10
        gateway = FakeGateway(initial)
        self.assertTrue(sd.deliver_synchtank(self.ctx, False, self.logger, gateway=gateway))
        self.assertEqual(gateway.events, [])
        set_status.assert_called_once()

    @patch("synchtank_delivery.workflow_gate_passed", return_value=(True, "ready"))
    @patch("synchtank_delivery.set_partner_status")
    def test_unexpected_remote_object_fails_before_upload(self, set_status, _gate) -> None:
        gateway = FakeGateway(
            {sd.package_prefix(self.ctx) + "unexpected-old.wav": 10}
        )
        self.assertFalse(sd.deliver_synchtank(self.ctx, False, self.logger, gateway=gateway))
        self.assertEqual(gateway.events, [])
        set_status.assert_not_called()

    @patch("synchtank_delivery.load_synchtank_s3_credentials")
    def test_dry_run_needs_no_credentials(self, load_credentials) -> None:
        self.assertTrue(sd.deliver_synchtank(self.ctx, True, self.logger))
        load_credentials.assert_not_called()
        self.assertFalse((self.root / "_WORKFLOW").exists())

    def test_symlink_is_rejected(self) -> None:
        link = self.package / "Music" / "linked.wav"
        try:
            link.symlink_to(self.package / "Music" / "Label" / "track.wav")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        with self.assertRaises(sd.SynchTankDeliveryError):
            sd.collect_package(self.package)

    @patch("synchtank_delivery.workflow_gate_passed", return_value=(False, "Step 15 failed"))
    @patch("synchtank_delivery.set_partner_status")
    def test_failed_workflow_gate_blocks_remote_access(self, set_status, _gate) -> None:
        gateway = FakeGateway()
        self.assertFalse(sd.deliver_synchtank(self.ctx, False, self.logger, gateway=gateway))
        self.assertEqual(gateway.events, [])
        set_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
