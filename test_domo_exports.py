"""Offline configuration tests for Step 1 Domo delivery metadata exports."""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import DOMO_CARDS, ReleaseContext
from domo_exports import (
    CARD_CONFIGS,
    _latest_dataflow_history_entry,
    _refresh_card_dataflow_chain,
    _xlsx_to_csv,
    verify_exports_exist,
)
from final_metadata_verification import _build_checks


class DomoDeliveryMetadataTests(unittest.TestCase):
    def test_latest_dataflow_history_entry_is_newest_row(self) -> None:
        body = (
            "History\n"
            "Version\tStart Time\tEnd Time\tDuration\tData Input\tData Output\tStarted by\tStatus"
            "\nv24\t9/2/2026 1:56 PM\t–\t3s\nManual run\nRUNNING\n"
            "v24\t9/1/2026 12:03 AM\t9/1/2026 12:05 AM\nSUCCESSFUL\n"
        )
        status, signature = _latest_dataflow_history_entry(body)
        self.assertEqual("RUNNING", status)
        self.assertIn("9/2/2026 1:56 PM", signature or "")

    def test_latest_dataflow_history_entry_fails_closed_without_table(self) -> None:
        self.assertEqual((None, None), _latest_dataflow_history_entry("Loading…"))

    def test_refresh_chain_runs_upstream_before_card_owner(self) -> None:
        card = {
            "key": "japan_metadata",
            "upstream_dataflow_ids": ("4312",),
        }
        calls: list[str] = []

        with (
            patch("domo_exports._resolve_card_dataflow_id", return_value="4278"),
            patch(
                "domo_exports._run_and_wait_for_dataflow",
                side_effect=lambda _page, dataflow_id, _logger, **_kwargs: (
                    calls.append(dataflow_id) or True
                ),
            ),
        ):
            result = _refresh_card_dataflow_chain(
                object(), card, logging.getLogger("test-domo-chain")
            )

        self.assertTrue(result)
        self.assertEqual(calls, ["4312", "4278"])

    def test_refresh_chain_stops_after_upstream_failure(self) -> None:
        card = {
            "key": "soundmouse_tracklist",
            "upstream_dataflow_ids": ("3691",),
        }
        calls: list[str] = []

        with (
            patch("domo_exports._resolve_card_dataflow_id", return_value="4330"),
            patch(
                "domo_exports._run_and_wait_for_dataflow",
                side_effect=lambda _page, dataflow_id, _logger, **_kwargs: (
                    calls.append(dataflow_id) or False
                ),
            ),
        ):
            result = _refresh_card_dataflow_chain(
                object(), card, logging.getLogger("test-domo-chain")
            )

        self.assertFalse(result)
        self.assertEqual(calls, ["3691"])

    def test_unisync_jobs_map_to_their_source_cards(self) -> None:
        jobs = ReleaseContext(2026, 8, 2, full_month_content=True).unisync_jobs
        self.assertEqual(
            [
                "us_tracklist",
                "us_tracklist",
                "exus_tracklist",
                "exus_tracklist",
                "japan_metadata",
            ],
            [job["domo_card_key"] for job in jobs],
        )

    def test_csv_conversion_drops_domo_grand_total_footer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            import pandas as pd

            root = Path(tmp)
            source = root / "summary.xlsx"
            output = root / "summary.csv"
            pd.DataFrame(
                [
                    {"Filename": "track_1.wav", "TrackTitle": "One"},
                    {"Filename": "GRAND TOTAL", "TrackTitle": ""},
                ]
            ).to_excel(source, index=False)

            _xlsx_to_csv(
                source,
                output,
                logging.getLogger("test-domo-exports"),
                drop_summary_rows=True,
            )

            exported = pd.read_csv(output, dtype=str).fillna("")
            self.assertEqual(exported["Filename"].tolist(), ["track_1.wav"])
            self.assertFalse(source.exists())

    def test_nbc_card_is_retired_from_active_exports(self) -> None:
        self.assertNotIn("nbc_metadata", DOMO_CARDS)
        self.assertNotIn("nbc_metadata", {card["key"] for card in CARD_CONFIGS})

    def test_scripps_export_enables_summary_footer_cleanup(self) -> None:
        cards = {card["key"]: card for card in CARD_CONFIGS}
        self.assertTrue(cards["scripps_metadata"]["drop_summary_rows"])

    def test_sourceaudio_cards_target_delivery_metadata_folders(self) -> None:
        ctx = ReleaseContext(2026, 8, 1)
        cards = {card["key"]: card for card in CARD_CONFIGS}

        self.assertEqual(DOMO_CARDS["sourceaudio_metadata"], "816828701")
        self.assertEqual(DOMO_CARDS["sourceaudio_exus_metadata"], "1909039415")
        self.assertEqual(
            cards["sourceaudio_metadata"]["output_fn"](ctx),
            ctx.partner_metadata["sourceaudio"],
        )
        self.assertEqual(
            cards["sourceaudio_exus_metadata"]["output_fn"](ctx),
            ctx.partner_metadata["sourceaudio_exus"],
        )
        self.assertEqual(cards["sourceaudio_metadata"]["sourceaudio_delta"], "us")
        self.assertEqual(
            cards["sourceaudio_exus_metadata"]["sourceaudio_delta"], "exus"
        )
        self.assertEqual(
            ctx.partner_metadata["sourceaudio"].name,
            "UPM August 2026 Part 1 Metadata.csv",
        )
        self.assertEqual(
            ctx.partner_metadata["sourceaudio_exus"].name,
            "UPM Ex-US August 2026 Part 1 Metadata.csv",
        )
        checks = {check.label: check for check in _build_checks(ctx)}
        self.assertEqual(
            checks["SourceAudio"].audio_source,
            ctx.partner_metadata["sourceaudio"],
        )
        self.assertEqual(
            checks["SourceAudio Ex-US"].audio_source,
            ctx.partner_metadata["sourceaudio_exus"],
        )

    def test_skip_domo_rejects_unchanged_baseline_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline.csv"
            delivery = root / "delivery.csv"
            baseline.write_text("Title,Filename\nOld,old.wav\n", encoding="utf-8")
            delivery.write_bytes(baseline.read_bytes())
            card = {
                "key": "sourceaudio_metadata",
                "output_fn": lambda _ctx: delivery,
                "baseline_template": baseline,
            }

            with patch("domo_exports.CARD_CONFIGS", [card]):
                result = verify_exports_exist(
                    object(), logging.getLogger("test-domo-exports")
                )
            self.assertFalse(result["sourceaudio_metadata"])

            delivery.write_text("Title,Filename\nNew,new.wav\n", encoding="utf-8")
            with patch("domo_exports.CARD_CONFIGS", [card]):
                result = verify_exports_exist(
                    object(), logging.getLogger("test-domo-exports")
                )
            self.assertTrue(result["sourceaudio_metadata"])


if __name__ == "__main__":
    unittest.main()
