"""Offline configuration tests for Step 1 Domo delivery metadata exports."""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import DOMO_CARDS, ReleaseContext
from domo_exports import CARD_CONFIGS, _xlsx_to_csv, verify_exports_exist
from final_metadata_verification import _build_checks


class DomoDeliveryMetadataTests(unittest.TestCase):
    def test_nbc_export_drops_domo_grand_total_footer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            import pandas as pd

            root = Path(tmp)
            source = root / "nbc.xlsx"
            output = root / "nbc.csv"
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
