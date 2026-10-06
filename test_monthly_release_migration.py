import json
import logging
import tempfile
import unittest
from pathlib import Path

from config import ReleaseContext
from monthly_release_migration import (
    MonthlyMigrationError,
    adopt_completed_monthly_release,
)


class MonthlyReleaseMigrationTests(unittest.TestCase):
    def _completed_root(self, parent: Path) -> Path:
        root = parent / "UPM-2026-10-MONTHLY"
        final = root / "3-FINAL PACKAGING"
        for name in (
            "Universal Production Music September 2026 - Japan NTT DATA",
            "Universal Production Music September 2026 - Japan JMD and TSS",
            "Universal Production Music October 2026 - Qwire",
            "Universal Production Music October 2026 - Scripps",
        ):
            package = final / name
            package.mkdir(parents=True)
            (package / "metadata.csv").write_text("header\nvalue\n", encoding="utf-8")
        workflow = root / "_WORKFLOW"
        workflow.mkdir()
        (workflow / "delivery_status.json").write_text(json.dumps({
            "partners": {
                "qwire": {"status": "delivered"},
                "scripps": {"status": "delivered"},
            }
        }), encoding="utf-8")
        for partner in ("qwire", "scripps"):
            (workflow / f"{partner}_delivery_receipt.json").write_text(json.dumps({
                "release_id": "UPM-2026-10-MONTHLY",
                "root": str(root),
            }), encoding="utf-8")
        return root

    def test_adopts_completed_root_without_changing_delivery_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self._completed_root(base)
            logs = base / "logs"
            report = logs / "reports" / source.name
            report.mkdir(parents=True)
            (report / "run.json").write_text(json.dumps({
                "release_id": source.name,
                "root": str(source),
            }), encoding="utf-8")
            ctx = ReleaseContext.for_monthly_delivery("2026-10-01")
            ctx.specials_dir = base / "UPM-2026-09-26"

            target = adopt_completed_monthly_release(
                ctx,
                source_root=source,
                logs_dir=logs,
                logger=logging.getLogger("monthly-migration"),
            )

            self.assertFalse(source.exists())
            self.assertTrue(target.is_dir())
            state = json.loads((target / "_WORKFLOW" / "delivery_status.json").read_text())
            self.assertEqual("delivered", state["partners"]["qwire"]["status"])
            receipt = json.loads((target / "_WORKFLOW" / "qwire_delivery_receipt.json").read_text())
            self.assertEqual("UPM20260926", receipt["release_id"])
            self.assertEqual(str(target), receipt["root"])
            marker = json.loads((target / "_WORKFLOW" / "early_monthly_build.json").read_text())
            self.assertEqual("2026-09-26", marker["rolling_start"])
            migrated_report = logs / "reports" / "UPM20260926" / "run.json"
            self.assertEqual("UPM20260926", json.loads(migrated_report.read_text())["release_id"])

    def test_refuses_to_overwrite_existing_rolling_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self._completed_root(base)
            ctx = ReleaseContext.for_monthly_delivery("2026-10-01")
            ctx.specials_dir = base / "UPM-2026-09-26"
            ctx.specials_dir.mkdir()
            with self.assertRaises(MonthlyMigrationError):
                adopt_completed_monthly_release(
                    ctx,
                    source_root=source,
                    logs_dir=base / "logs",
                    logger=logging.getLogger("monthly-migration"),
                )


if __name__ == "__main__":
    unittest.main()
