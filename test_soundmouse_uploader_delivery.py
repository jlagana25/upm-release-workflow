from __future__ import annotations

import json
import logging
import tempfile
import unittest
from dataclasses import replace
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

    def test_metadata_only_correction_is_valid(self) -> None:
        correction = self.root / "Missing"
        (correction / "Metadata").mkdir(parents=True)
        (correction / "Metadata" / "corrected.xlsx").write_bytes(b"metadata")
        files = sud.collect_package(
            correction,
            required_top_levels=frozenset({"Metadata"}),
        )
        self.assertEqual(
            [item.relative for item in files],
            ["Metadata/corrected.xlsx"],
        )

    def test_missing_reports_and_correction_audits_are_not_uploaded(self) -> None:
        (self.package / "SoundMouse Missing Report.csv").write_text(
            "Type,Filename\n", encoding="utf-8"
        )
        missing = self.package / "Missing"
        missing.mkdir()
        (missing / "SoundMouse Missing Audit.csv").write_text(
            "Action,Filename\n", encoding="utf-8"
        )

        files = sud.collect_package(self.package)

        self.assertEqual(
            [item.relative for item in files],
            ["Covers/cover.jpg", "MEDIA/track.wav", "Metadata/metadata.xlsx"],
        )

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

    def test_uploaded_and_complete_rows_are_successful(self) -> None:
        files = sud.collect_package(self.package)
        rows = tuple(
            sud.QueueRow(
                row_id=index,
                url=item.path.as_uri(),
                status=(
                    sud.UPLOADED_STATUS if index == 1 else sud.COMPLETED_STATUS
                ),
                workspace_id="workspace-id",
                module_name="music_manager",
                failure_reason=0,
                original_filename=item.path.name,
            )
            for index, item in enumerate(files, start=1)
        )
        self.assertTrue(sud.queue_rows_successful(rows))
        self.assertNotIn(sud.UPLOADED_STATUS, sud.ACTIVE_STATUSES)

    def test_success_status_supersedes_stale_retry_failure_reason(self) -> None:
        files = sud.collect_package(self.package)
        rows = tuple(
            sud.QueueRow(
                row_id=index,
                url=item.path.as_uri(),
                status=sud.COMPLETED_STATUS,
                workspace_id="workspace-id",
                module_name="music_manager",
                failure_reason=1,
                original_filename=item.path.name,
            )
            for index, item in enumerate(files, start=1)
        )
        self.assertTrue(sud.queue_rows_successful(rows))
        self.assertFalse(
            sud.queue_rows_successful(
                (replace(rows[0], status=4),) + rows[1:]
            )
        )

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

    def test_ui_script_verifies_toolbar_without_brittle_sheet_query(self) -> None:
        script = sud._ui_script(self.package)
        self.assertIn("repeat with candidateWindow in windows", script)
        self.assertIn("exists toolbar 1 of candidateWindow", script)
        self.assertIn("toolbar 1 of mainWindow", script)
        self.assertNotIn("toolbar 1 of window 1", script)
        self.assertIn("set value of text field 1 of goToSheet", script)
        self.assertIn(str(self.package), script)
        self.assertNotIn('keystroke "' + str(self.package.parent), script)
        self.assertIn('description is "Workspace"', script)
        self.assertIn('value of workspaceButton is not "UPPM"', script)
        self.assertIn('description is "Module"', script)
        self.assertIn('value of moduleButton is not "Music"', script)
        self.assertNotIn("every pop up button of entire contents", script)
        self.assertNotIn("every radio button of entire contents", script)

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

    @patch("soundmouse_uploader_delivery.set_partner_status")
    @patch("soundmouse_uploader_delivery._write_receipt")
    @patch("soundmouse_uploader_delivery.queue_snapshot")
    @patch("soundmouse_uploader_delivery.subprocess.run")
    @patch("soundmouse_uploader_delivery.soundmouse_gate_passed")
    def test_completed_native_upload_stays_uploaded_until_web_processing(
        self, gate, run, snapshot, receipt, set_status
    ) -> None:
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
        gate.return_value = (True, "Step 16 completed")
        snapshot.return_value = (len(rows), rows)
        receipt.return_value = self.root / "receipt.json"
        logger = logging.getLogger("test_soundmouse_uploaded_boundary")
        queue_db = self.root / "queue.sqlite"
        queue_db.write_bytes(b"queue")

        with patch.object(sud, "APP_PATH", self.root), patch.object(
            sud, "QUEUE_DB", queue_db
        ):
            self.assertTrue(sud.deliver_soundmouse_uploader(self.ctx, False, logger))

        run.assert_called_once()
        set_status.assert_called_once_with(
            self.ctx.specials_dir, "soundmouse", "uploaded"
        )

    @patch("soundmouse_uploader_delivery._ui_submit_package")
    @patch("soundmouse_uploader_delivery.subprocess.run")
    def test_dry_run_never_opens_or_submits(self, run, submit) -> None:
        logger = logging.getLogger("test_soundmouse_uploader")
        self.assertTrue(sud.deliver_soundmouse_uploader(self.ctx, True, logger))
        run.assert_not_called()
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
