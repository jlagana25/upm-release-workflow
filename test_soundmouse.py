"""Offline synthetic-filesystem tests for SoundMouse Step 16."""

from __future__ import annotations

import csv
import logging
import tempfile
import unittest
from pathlib import Path

from config import ReleaseContext
from soundmouse import (
    activation_ranges_from_tracklist,
    create_soundmouse_directories,
    metadata_codes_from_bucket,
    strip_xlsx_formatting,
    validate_soundmouse_delivery,
)


class SoundMouseTests(unittest.TestCase):
    def _csv(self, root: Path, name: str, fields: list[str], rows: list[dict]) -> Path:
        path = root / name
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_context_names_full_month_and_parts(self) -> None:
        full = ReleaseContext(2026, 6, 1, previous_month=True)
        self.assertEqual(
            full.soundmouse_tracklist_csv.name,
            "Soundmouse 06-01-26 to 07-01-26.csv",
        )
        self.assertEqual(full.soundmouse_activation_range, "2026-06-01_to_2026-06-30")
        self.assertEqual(full.tracklist_token, "Jun2026-Full")
        self.assertEqual(full.month_display_folder, "June 2026 Full")
        self.assertEqual(full.specials_root, "UPM-2026-06_FULL")
        self.assertEqual(full.hd_folder, "2026-06 (June Full)")
        self.assertEqual(
            full.pinned_cli_args(),
            ["--previous-month", "--year", "2026", "--month", "7"],
        )
        self.assertEqual(
            full.cleanup_target_folder.parent.name,
            "Universal Production Music June 2026 Full Release - Tunesat",
        )

        part1 = ReleaseContext(2026, 6, 1)
        part2 = ReleaseContext(2026, 6, 2)
        self.assertEqual(
            part2.pinned_cli_args(),
            ["--year", "2026", "--month", "6", "--part", "2"],
        )
        december_full = ReleaseContext(2026, 12, 1, previous_month=True)
        self.assertEqual(
            december_full.pinned_cli_args(),
            ["--previous-month", "--year", "2027", "--month", "1"],
        )
        self.assertEqual(part1.soundmouse_tracklist_csv.name, "Soundmouse 06-01-26 to 06-15-26.csv")
        self.assertEqual(part2.soundmouse_tracklist_csv.name, "Soundmouse 06-15-26 to 07-01-26.csv")

    def test_directories_come_from_activation_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracklist = self._csv(
                root, "tracklist.csv", ["workAudioId", "ActivationRange"],
                [
                    {"workAudioId": "1", "ActivationRange": "2026-06-01_to_2026-06-30"},
                    {"workAudioId": "2", "ActivationRange": "2026-06-01_to_2026-06-30"},
                ],
            )
            roots = create_soundmouse_directories(
                tracklist, root / "SoundMouse", False, logging.getLogger("test")
            )
            self.assertEqual(len(roots), 1)
            for child in ("Covers", "Metadata", "MEDIA"):
                self.assertTrue((roots[0] / child).is_dir())

    def test_invalid_activation_range_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracklist = self._csv(
                root, "bad.csv", ["ActivationRange"],
                [{"ActivationRange": "../../escape"}],
            )
            with self.assertRaises(ValueError):
                activation_ranges_from_tracklist(tracklist)

    def test_bucket_recognizes_codes_and_territory_sets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bucket = self._csv(
                root, "bucket.csv", ["SoundMouse Bucket", "Territory List"],
                [
                    {"SoundMouse Bucket": "02 UK DE SE OZ", "Territory List": ""},
                    {"SoundMouse Bucket": "", "Territory List": "US OZ"},
                    {"SoundMouse Bucket": "08 - OZ", "Territory List": ""},
                ],
            )
            self.assertEqual(metadata_codes_from_bucket(bucket), ["02", "07", "08"])

    def test_xlsx_formatting_is_removed_but_values_and_formulas_remain(self) -> None:
        from openpyxl import Workbook, load_workbook
        from openpyxl.formatting.rule import CellIsRule
        from openpyxl.styles import Font, PatternFill

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Metadata"
            sheet["A1"] = "Title"
            sheet["A1"].font = Font(bold=True, color="FFFFFF")
            sheet["A1"].fill = PatternFill("solid", fgColor="4472C4")
            sheet["A2"] = 5
            sheet["B2"] = "=A2*2"
            sheet.column_dimensions["A"].width = 30
            sheet.row_dimensions[1].height = 24
            sheet.freeze_panes = "A2"
            sheet.conditional_formatting.add(
                "A2", CellIsRule(operator="greaterThan", formula=["0"])
            )
            workbook.save(path)
            workbook.close()

            strip_xlsx_formatting(path)

            cleaned = load_workbook(path, data_only=False)
            cleaned_sheet = cleaned["Metadata"]
            self.assertEqual(cleaned_sheet["A1"].value, "Title")
            self.assertEqual(cleaned_sheet["B2"].value, "=A2*2")
            self.assertEqual(cleaned_sheet["A1"].style_id, 0)
            self.assertEqual(cleaned_sheet["A2"].number_format, "General")
            self.assertNotIn("A", cleaned_sheet.column_dimensions)
            self.assertNotIn(1, cleaned_sheet.row_dimensions)
            self.assertIsNone(cleaned_sheet.freeze_panes)
            self.assertEqual(len(cleaned_sheet.conditional_formatting), 0)
            cleaned.close()

    def test_metadata_validation_checks_audio_and_covers(self) -> None:
        from openpyxl import Workbook

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata_dir = root / "Metadata"
            media_dir = root / "MEDIA" / "Label" / "Album"
            covers_dir = root / "Covers"
            metadata_dir.mkdir()
            media_dir.mkdir(parents=True)
            covers_dir.mkdir()

            metadata = metadata_dir / "SoundMouseMetadata 01 - ALL.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["Filename", "ALBUM ARTWORK FILE NAME"])
            sheet.append(["Track_One.wav", "cover-one.jpg"])
            sheet.append(["Track_Two.wav", "cover-two.jpg"])
            workbook.save(metadata)
            workbook.close()

            (media_dir / "TRACK_ONE.WAV").write_bytes(b"audio")
            (covers_dir / "Cover-One.JPG").write_bytes(b"cover")
            report = root / "reports" / "missing.csv"
            logger = logging.getLogger("test")

            self.assertFalse(validate_soundmouse_delivery(
                [metadata], root / "MEDIA", covers_dir, report, False, logger
            ))
            with report.open(encoding="utf-8-sig", newline="") as handle:
                missing = list(csv.DictReader(handle))
            self.assertEqual(
                {(row["Type"], row["Filename"]) for row in missing},
                {("AUDIO", "Track_Two.wav"), ("COVER", "cover-two.jpg")},
            )

            (media_dir / "Track_Two.wav").write_bytes(b"audio")
            (covers_dir / "cover-two.jpg").write_bytes(b"cover")
            self.assertTrue(validate_soundmouse_delivery(
                [metadata], root / "MEDIA", covers_dir, report, False, logger
            ))
            with report.open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])


if __name__ == "__main__":
    unittest.main()
