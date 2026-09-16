from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tunesat_delivery as td


class FakeGateway:
    def __init__(self, initial: dict[str, int] | None = None) -> None:
        self.files = dict(initial or {})
        self.uploads: list[str] = []

    def list_files(self) -> dict[str, int]:
        return dict(self.files)

    def upload(self, local_path: Path, relative: str) -> None:
        self.uploads.append(relative)
        self.files.pop(relative + ".part", None)
        self.files[relative] = local_path.stat().st_size

    def file_size(self, relative: str) -> int | None:
        return self.files.get(relative)

    def close(self) -> None:
        pass


def _context(root: Path):
    return SimpleNamespace(
        specials_dir=root,
        release_id="UPM20260912",
        partner_folder_name=lambda partner: f"Universal Production Music Test - {partner}",
    )


class TuneSatDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ctx = _context(self.root)
        self.package = td.package_root(self.ctx)
        (self.package / "Music" / "Label").mkdir(parents=True)
        (self.package / "Metadata").mkdir()
        (self.package / "Music" / "Label" / "track.mp3").write_bytes(b"audio")
        (self.package / "Metadata" / "metadata.csv").write_bytes(b"metadata")
        self.logger = logging.getLogger("test_tunesat")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @patch("tunesat_delivery.workflow_gate_passed", return_value=(True, "ready"))
    @patch("tunesat_delivery.set_partner_status")
    def test_uploads_complete_package_directly(self, set_status, _gate) -> None:
        gateway = FakeGateway()
        self.assertTrue(td.deliver_tunesat(self.ctx, False, self.logger, gateway=gateway))
        self.assertEqual(
            gateway.uploads,
            ["Metadata/metadata.csv", "Music/Label/track.mp3"],
        )
        self.assertNotIn("UPM20260912", "".join(gateway.files))
        self.assertNotIn("delivery.complete", gateway.files)
        set_status.assert_called_once_with(self.root, "tunesat", "uploaded")
        self.assertTrue((self.root / "_WORKFLOW" / "tunesat_delivery_receipt.json").is_file())

    @patch("tunesat_delivery.workflow_gate_passed", return_value=(True, "ready"))
    @patch("tunesat_delivery.set_partner_status")
    def test_exact_files_are_resumed_without_upload(self, set_status, _gate) -> None:
        manifest = td.collect_package(self.package)
        gateway = FakeGateway({item.relative: item.size for item in manifest})
        self.assertTrue(td.deliver_tunesat(self.ctx, False, self.logger, gateway=gateway))
        self.assertEqual(gateway.uploads, [])
        set_status.assert_called_once()

    @patch("tunesat_delivery.workflow_gate_passed", return_value=(True, "ready"))
    @patch("tunesat_delivery.set_partner_status")
    def test_matching_part_file_is_recoverable(self, set_status, _gate) -> None:
        gateway = FakeGateway({"Music/Label/track.mp3.part": 2})
        self.assertTrue(td.deliver_tunesat(self.ctx, False, self.logger, gateway=gateway))
        self.assertIn("Music/Label/track.mp3", gateway.uploads)
        self.assertNotIn("Music/Label/track.mp3.part", gateway.files)
        set_status.assert_called_once()

    @patch("tunesat_delivery.workflow_gate_passed", return_value=(True, "ready"))
    @patch("tunesat_delivery.set_partner_status")
    def test_unexpected_remote_file_fails_closed(self, set_status, _gate) -> None:
        gateway = FakeGateway({"old.mp3": 10})
        self.assertFalse(td.deliver_tunesat(self.ctx, False, self.logger, gateway=gateway))
        self.assertEqual(gateway.uploads, [])
        set_status.assert_not_called()

    @patch("tunesat_delivery.load_tunesat_sftp_credentials")
    def test_dry_run_does_not_load_credentials(self, load_credentials) -> None:
        self.assertTrue(td.deliver_tunesat(self.ctx, True, self.logger))
        load_credentials.assert_not_called()
        self.assertFalse((self.root / "_WORKFLOW").exists())

    def test_requires_music_and_metadata(self) -> None:
        (self.package / "Metadata" / "metadata.csv").unlink()
        with self.assertRaises(td.TuneSatDeliveryError):
            td.collect_package(self.package)


if __name__ == "__main__":
    unittest.main()
