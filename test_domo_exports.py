"""Offline configuration tests for Step 1 Domo delivery metadata exports."""

from __future__ import annotations

import logging
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import DOMO_CARDS, ReleaseContext
from domo_exports import (
    CARD_CONFIGS,
    _export_api_projection,
    _latest_dataflow_history_entry,
    _refresh_card_dataflow_chain,
    _xlsx_to_csv,
    verify_exports_exist,
    run_domo_exports,
)
from domo_api import DomoQueryResult
from domo_projection_contracts import DOMO_PROJECTION_CONTRACTS
from bmat_delivery import BMAT_CARD_CONFIGS
from final_metadata_verification import _build_checks
from final_packaging import _build_ops
from prune import _tree_specs


class DomoDeliveryMetadataTests(unittest.TestCase):
    def test_every_step1_and_bmat_card_has_explicit_api_contract(self) -> None:
        expected = {card["key"] for card in CARD_CONFIGS + BMAT_CARD_CONFIGS}
        self.assertEqual(expected, set(DOMO_PROJECTION_CONTRACTS))

    def test_api_projection_writes_ordered_tracklist_without_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "tracklist.csv"
            card = next(card for card in CARD_CONFIGS if card["key"] == "us_tracklist")
            columns = [
                "LabelName", "AlbumNo", "AlbumTitle", "AudioFile",
                "AlbumReleaseDate", "WorkTitle", "TrackNo", "domoAudioId",
                "PipsCode", "ComposerNames", "AlbumCoverArt",
            ]
            values = [
                "Label", "ABC1", "Album", "track.mp3", "2026-09-03T23:00:00",
                "Track", 1, 123, "P1", "Writer", "cover.jpeg",
            ]
            with patch(
                "domo_exports.query_dataset",
                return_value=DomoQueryResult(columns, [values]),
            ) as query:
                _export_api_projection(
                    card,
                    output,
                    ReleaseContext.for_date_range("2026-09-01", "2026-09-11"),
                    logging.getLogger("test-api-projection"),
                )
            with output.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Filename"], "track")
            self.assertEqual(rows[0]["AlbumNoMasters"], "ABC1 - Album")
            self.assertEqual(rows[0]["AlbumCoverArt"], "cover.jpg")
            self.assertTrue(rows[0]["CDNAlbumArt"].endswith("/cover.webp"))
            sql = query.call_args.args[1]
            self.assertIn("AlbumReleaseDate", sql)
            self.assertIn("2026-09-12", sql)

    def test_bmat_contracts_encode_custom_inventory_boundaries(self) -> None:
        releases = DOMO_PROJECTION_CONTRACTS["bmat_releases"]
        submission = DOMO_PROJECTION_CONTRACTS["bmat_submission"]
        self.assertTrue(releases.distinct)
        self.assertEqual(releases.where_sql, "`LabelName` = 'ELIAS CUSTOM'")
        self.assertIn("'As Heard On TV'", submission.where_sql or "")
        self.assertIn("'ELIAS CUSTOM'", submission.where_sql or "")
        headers = [field.output for field in submission.fields]
        self.assertIn("COM:1:Society", headers)
        self.assertEqual(len(headers), len(set(headers)))

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

    def test_monthly_metadata_cards_skip_non_triggering_rolling_run(self) -> None:
        ctx = ReleaseContext.for_date_range("2026-09-12", "2026-09-25")
        results = run_domo_exports(
            ctx,
            True,
            logging.getLogger("test-monthly-metadata-skip"),
            only_keys=[
                "japan_metadata",
                "japan_jmdtss_metadata",
                "qwire_metadata",
                "scripps_metadata",
            ],
        )
        self.assertEqual(
            results,
            {
                "japan_metadata": "skipped_not_due",
                "japan_jmdtss_metadata": "skipped_not_due",
                "qwire_metadata": "skipped_not_due",
                "scripps_metadata": "skipped_not_due",
            },
        )

    def test_monthly_metadata_cards_use_calendar_month_paths_when_due(self) -> None:
        ctx = ReleaseContext.for_monthly_delivery("2026-10-01")
        cards = {card["key"]: card for card in CARD_CONFIGS}
        for key in (
            "japan_metadata",
            "japan_jmdtss_metadata",
            "qwire_metadata",
            "scripps_metadata",
        ):
            self.assertTrue(cards[key]["monthly_metadata"])
            self.assertIn("October 2026", str(cards[key]["output_fn"](ctx)))

    def test_off_cycle_context_omits_japan_unisync_and_final_check(self) -> None:
        ctx = ReleaseContext.for_date_range("2026-09-12", "2026-09-25")
        self.assertNotIn("Japan WAV", {job["name"] for job in ctx.unisync_jobs})
        self.assertNotIn("NTT Data", {check.label for check in _build_checks(ctx)})
        self.assertNotIn("japan_ntt", {op.partner_key for op in _build_ops(ctx)})
        self.assertNotIn("Japan", {spec[0] for spec in _tree_specs(ctx)})

    def test_monthly_context_includes_japan_unisync_and_final_check(self) -> None:
        ctx = ReleaseContext.for_monthly_delivery("2026-10-01")
        self.assertIn("Japan WAV", {job["name"] for job in ctx.unisync_jobs})
        self.assertIn("NTT Data", {check.label for check in _build_checks(ctx)})
        self.assertIn("japan_ntt", {op.partner_key for op in _build_ops(ctx)})
        self.assertIn("Japan", {spec[0] for spec in _tree_specs(ctx)})

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
