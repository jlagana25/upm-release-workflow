import csv
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import unisync_automation as unisync


class UniSyncRetryTests(unittest.TestCase):
    def test_unattended_zero_progress_is_failure_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = root / "request.csv"
            with request.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["Filename", "workAudioId"])
                writer.writeheader()
                writer.writerow({"Filename": "track-one", "workAudioId": "101"})
            client = root / "client"
            client.mkdir()
            job = {
                "name": "US WAV",
                "csv": str(request),
                "cache_path": str(root / "cache"),
                "client_path": str(client),
                "territory": "United States",
            }
            logger = logging.getLogger(self.id())
            with (
                patch.object(unisync, "_drive_unisync_for_csv", return_value=unisync.STATUS_OK),
                patch.object(unisync, "_present_filenames", return_value=set()),
                patch.object(unisync, "_report_not_found"),
                patch.object(unisync, "FAILURE_SCREENSHOTS_DIR", root / "failures"),
                patch.object(unisync, "UNATTENDED_ZERO_PROGRESS_RETRIES", 1),
                patch.object(unisync, "SUPERVISED", False),
            ):
                status = unisync._run_single_job(job, False, logger)
            self.assertEqual(status, unisync.STATUS_FAILED)
            self.assertEqual(len(list((root / "failures").glob("*_missing_*.csv"))), 1)


if __name__ == "__main__":
    unittest.main()
