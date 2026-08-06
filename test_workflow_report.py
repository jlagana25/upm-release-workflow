import json
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from config import ReleaseContext
from upm_release_workflow import StepResults, STATUS_COMPLETED
import workflow_report


class WorkflowReportTests(unittest.TestCase):
    def test_report_contains_steps_counts_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "workflow.log"
            log.write_text(
                "2026-08-05 10:00:00  WARNING   test warning\n",
                encoding="utf-8",
            )
            results = StepResults()
            results.set("preflight", STATUS_COMPLETED)
            ctx = ReleaseContext(2026, 7, 1)
            with (
                patch.object(workflow_report, "LOGS_DIR", root / "reports"),
                patch.object(workflow_report, "_count_files", return_value=0),
            ):
                path = workflow_report.write_workflow_report(
                    ctx,
                    Namespace(dry_run=True),
                    results,
                    log,
                    datetime.now().astimezone(),
                )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["release"]["id"], "UPM-2026-07-P1")
            self.assertEqual(payload["steps"]["preflight"]["status"], "completed")
            self.assertEqual(payload["diagnostics"][0]["level"], "warning")
            self.assertIn("nbc_wav", payload["output_counts"])


if __name__ == "__main__":
    unittest.main()
