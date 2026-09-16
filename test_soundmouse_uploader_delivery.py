from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import soundmouse_uploader_delivery as sud


class SoundMouseUploaderDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package = self.root / "2026-09-12_to_2026-09-25"
        for directory in ("MEDIA", "Covers", "Metadata"):
            (self.package / directory).mkdir(parents=True)
        (self.package / "MEDIA" / "track.wav").write_bytes(b"audio")
        (self.package / "Covers" / "cover.jpg").write_bytes(b"cover")
        (self.package / "Metadata" / "metadata.xlsx").write_bytes(b"metadata")
        self.ctx = SimpleNamespace(
            soundmouse_release_dir=self.package,
            specials_dir=self.root / "specials",
            release_id="UPM20260912",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_collects_exact_full_package(self) -> None:
        files = sud.collect_package(self.package)
        self.assertEqual(
            [item.relative for item in files],
            ["Covers/cover.jpg", "MEDIA/track.wav", "Metadata/metadata.xlsx"],
        )

    def test_missing_required_directory_fails(self) -> None:
        (self.package / "Covers" / "cover.jpg").unlink()
        with self.assertRaises(sud.SoundMouseUploaderError):
            sud.collect_package(self.package)

    def test_queue_manifest_uses_full_file_urls(self) -> None:
        files = sud.collect_package(self.package)
        rows = tuple(
            sud.QueueRow(
                row_id=index,
                url=item.path.as_uri(),
                status=sud.COMPLETED_STATUS,
                workspace_id="workspace-id",
                module_name="music_manager",
                failure_reason=0,
                original_filename=item.path.name,
            )
            for index, item in enumerate(files, start=1)
        )
        sud.validate_queue_rows(files, rows)

    def test_wrong_module_fails(self) -> None:
        files = sud.collect_package(self.package)
        rows = tuple(
            sud.QueueRow(
                row_id=index,
                url=item.path.as_uri(),
                status=sud.COMPLETED_STATUS,
                workspace_id="workspace-id",
                module_name="productions",
                failure_reason=0,
                original_filename=item.path.name,
            )
            for index, item in enumerate(files, start=1)
        )
        with self.assertRaises(sud.SoundMouseUploaderError):
            sud.validate_queue_rows(files, rows)

    def test_latest_real_step16_failure_wins(self) -> None:
        reports = self.root / "reports" / self.ctx.release_id
        reports.mkdir(parents=True)
        older = {
            "run": {"dry_run": False},
            "steps": {"16 SoundMouse": {"status": "completed"}},
        }
        newer = {
            "run": {"dry_run": False},
            "steps": {"16 SoundMouse": {"status": "failed"}},
        }
        (reports / "run-1.json").write_text(json.dumps(older), encoding="utf-8")
        (reports / "run-2.json").write_text(json.dumps(newer), encoding="utf-8")
        with patch.object(sud, "LOGS_DIR", self.root):
            passed, detail = sud.soundmouse_gate_passed(self.ctx)
        self.assertFalse(passed)
        self.assertIn("failed", detail)

    @patch("soundmouse_uploader_delivery._ui_submit_package")
    @patch("soundmouse_uploader_delivery.subprocess.run")
    def test_dry_run_never_opens_or_submits(self, run, submit) -> None:
        logger = logging.getLogger("test_soundmouse_uploader")
        self.assertTrue(sud.deliver_soundmouse_uploader(self.ctx, True, logger))
        run.assert_not_called()
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
