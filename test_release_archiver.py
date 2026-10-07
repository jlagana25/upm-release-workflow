import json
import logging
import tempfile
import unittest
from datetime import date
from pathlib import Path

from release_archiver import (
    ArchiveError,
    archive_release,
    retention_cutoff,
)


class ReleaseArchiverTests(unittest.TestCase):
    def test_retention_keeps_current_and_previous_two_calendar_months(self):
        self.assertEqual(date(2026, 8, 1), retention_cutoff(date(2026, 10, 6), 3))

    def test_archives_verified_old_release_and_removes_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "UPM-2026-07-04"
            workflow = root / "_WORKFLOW"
            workflow.mkdir(parents=True)
            (root / "audio.wav").write_bytes(b"audio-data" * 100)
            (workflow / "delivery_status.json").write_text(json.dumps({
                "partners": {"test": {"status": "delivered"}}
            }), encoding="utf-8")
            logs = base / "logs"
            reports = logs / "reports" / "UPM20260704"
            reports.mkdir(parents=True)
            (reports / "run-1.json").write_text(json.dumps({
                "run": {"dry_run": False, "overall": "completed"}
            }), encoding="utf-8")
            archive = archive_release(
                root,
                as_of=date(2026, 10, 6),
                execute=True,
                confirmation="UPM20260704",
                logs_dir=logs,
                logger=logging.getLogger("archive-test"),
            )
            self.assertFalse(root.exists())
            self.assertTrue(archive.is_file())
            record = json.loads(archive.with_suffix("").with_suffix(".json").read_text())
            self.assertEqual("UPM20260704", record["release_id"])

    def test_refuses_release_inside_retention_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "UPM-2026-08-01"
            root.mkdir()
            with self.assertRaises(ArchiveError):
                archive_release(
                    root,
                    as_of=date(2026, 10, 6),
                    execute=False,
                    confirmation=None,
                    logs_dir=Path(tmp) / "logs",
                    logger=logging.getLogger("archive-test"),
                )


if __name__ == "__main__":
    unittest.main()
