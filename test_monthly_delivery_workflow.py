import logging
import unittest
from unittest.mock import patch

from config import ReleaseContext
from monthly_delivery_workflow import MONTHLY_DOMO_KEYS, run_monthly_delivery


LOG = logging.getLogger("test-monthly-delivery")


class MonthlyDeliveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.ctx = ReleaseContext.for_monthly_delivery("2026-11-01")

    def test_monthly_context_uses_containing_rolling_batch(self):
        self.assertEqual(self.ctx.release_start, "2026-10-01")
        self.assertEqual(self.ctx.release_end, "2026-10-31")
        self.assertEqual(self.ctx.release_id, "UPM20261024")
        self.assertEqual(self.ctx.monthly_monday_batch, "UPM20261024")
        self.assertEqual(self.ctx.monthly_rolling_owner_start, "2026-10-24")
        self.assertEqual(self.ctx.monthly_rolling_owner_end, "2026-11-06")

    def test_october_delivery_uses_sep_26_rolling_batch(self):
        october = ReleaseContext.for_monthly_delivery("2026-10-01")
        self.assertTrue(october.monthly_rolls_into_batch)
        self.assertEqual(october.release_id, "UPM20260926")
        self.assertEqual(october.monthly_rolling_owner_end, "2026-10-09")

    def test_context_refuses_a_non_first_delivery_date(self):
        with self.assertRaisesRegex(ValueError, "scheduled for the 1st"):
            ReleaseContext.for_monthly_delivery("2026-10-02")

    def test_dry_run_executes_only_monthly_build_phases(self):
        with (
            patch("monthly_delivery_workflow._preflight", return_value=True),
            patch("folder_setup.create_monthly_delivery_folder", return_value=True),
            patch(
                "monday_sync.run_monday_source_preflight", return_value=True
            ) as monday_source,
            patch("monday_sync.run_monday_sync") as monday,
            patch(
                "domo_exports.run_domo_exports",
                return_value={key: "skipped" for key in MONTHLY_DOMO_KEYS},
            ) as domo,
            patch("unisync_automation.run_all_unisync_jobs", return_value={"Japan WAV": "skipped"}),
            patch("monthly_delivery_workflow._verify_monthly_sources", return_value=True),
            patch("final_packaging.copy_originals_to_finals", return_value=True) as packaging,
            patch("final_metadata_verification.verify_final_packaging_metadata", return_value=True),
        ):
            result = run_monthly_delivery(
                self.ctx,
                dry_run=True,
                overwrite=False,
                skip_monday=False,
                logger=LOG,
            )

        self.assertNotIn("failed", result.values())
        self.assertEqual(domo.call_args.kwargs["only_keys"], list(MONTHLY_DOMO_KEYS))
        packaging.assert_called_once()
        monday.assert_not_called()
        owner = monday_source.call_args.args[0]
        self.assertEqual(owner.release_start, "2026-10-24")
        self.assertEqual(owner.release_end, "2026-11-06")

    def test_failed_domo_marks_monthly_monday_item_stuck(self):
        with (
            patch("monthly_delivery_workflow._preflight", return_value=True),
            patch("folder_setup.create_monthly_delivery_folder", return_value=True),
            patch("monday_sync.run_monday_source_preflight", return_value=True),
            patch("monday_sync.run_monday_sync", return_value=True) as monday,
            patch(
                "domo_exports.run_domo_exports",
                return_value={key: "failed" for key in MONTHLY_DOMO_KEYS},
            ),
            patch("unisync_automation.run_all_unisync_jobs") as unisync,
        ):
            result = run_monthly_delivery(
                self.ctx,
                dry_run=False,
                overwrite=False,
                skip_monday=False,
                logger=LOG,
            )

        self.assertEqual(result["domo_exports"], "failed")
        self.assertEqual(monday.call_count, 2)
        final_results = monday.call_args.args[1]
        self.assertEqual(final_results["10 Final packaging"], "failed")
        self.assertEqual(final_results["15 Final metadata check"], "failed")
        unisync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
