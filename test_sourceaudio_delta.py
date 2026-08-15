"""Synthetic-filesystem tests for refreshed SourceAudio metadata handling."""

from __future__ import annotations

import csv
import logging
import tempfile
import unittest
from pathlib import Path

from sourceaudio_delta import reconcile_sourceaudio_refresh


class SourceAudioDeltaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.metadata = self.root / "Metadata" / "sourceaudio.csv"
        self.media = self.root / "Music"
        self.sources = self.root / "Source" / "MEDIA"
        self.logger = logging.getLogger(f"sourceaudio-delta-{id(self)}")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_metadata(self, rows: list[tuple[str, str]]) -> None:
        self.metadata.parent.mkdir(parents=True, exist_ok=True)
        with self.metadata.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["External Id", "Filename"])
            writer.writeheader()
            for track_id, filename in rows:
                writer.writerow({"External Id": track_id, "Filename": filename})

    @staticmethod
    def _fake_convert(source: Path, destination: Path) -> tuple[bool, str]:
        destination.write_bytes(b"converted:" + source.read_bytes())
        return True, "test conversion"

    def test_initial_export_skips_when_no_existing_aiffs(self) -> None:
        self._write_metadata([("100", "Track_100.aif")])
        result = reconcile_sourceaudio_refresh(
            metadata_path=self.metadata,
            media_dir=self.media,
            source_dir=self.sources,
            logger=self.logger,
        )
        self.assertTrue(result.initial_delivery)
        self.assertTrue(result.ok)
        self.assertFalse((self.root / "Missing").exists())

    def test_addition_rename_and_removal_build_missing_package(self) -> None:
        self._write_metadata([
            ("100", "Corrected_Name_100.aif"),
            ("300", "New_Track_300.aif"),
        ])
        self.media.mkdir(parents=True)
        (self.media / "Old_Name_100.aif").write_bytes(b"old-aiff")
        (self.media / "Removed_Track_200.aif").write_bytes(b"removed-aiff")
        self.sources.mkdir(parents=True)
        (self.sources / "Master_Track_300.wav").write_bytes(b"new-wav")

        result = reconcile_sourceaudio_refresh(
            metadata_path=self.metadata,
            media_dir=self.media,
            source_dir=self.sources,
            logger=self.logger,
            converter=self._fake_convert,
        )

        missing = self.root / "Missing"
        self.assertTrue(result.ok)
        self.assertEqual((result.additions, result.renames, result.removals), (1, 1, 1))
        self.assertEqual(result.files_prepared, 2)
        self.assertEqual((missing / "Corrected_Name_100.aif").read_bytes(), b"old-aiff")
        self.assertEqual(
            (missing / "New_Track_300.aif").read_bytes(), b"converted:new-wav"
        )
        self.assertFalse((self.media / "Old_Name_100.aif").exists())
        self.assertFalse((self.media / "Removed_Track_200.aif").exists())
        self.assertEqual(result.local_files_removed, 2)

        with (missing / "SourceAudio Missing Audit.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            report = list(csv.DictReader(handle))
        self.assertEqual(
            {row["Action"] for row in report},
            {"ADDITION_UPLOAD", "RENAMED_FILENAME_UPLOAD", "REMOVE_FROM_SOURCEAUDIO"},
        )

    def test_missing_source_fails_closed_and_is_reported(self) -> None:
        self._write_metadata([
            ("400", "Unavailable_400.aif"),
        ])
        self.media.mkdir(parents=True)
        (self.media / "Existing_100.aif").write_bytes(b"aiff")

        result = reconcile_sourceaudio_refresh(
            metadata_path=self.metadata,
            media_dir=self.media,
            source_dir=self.sources,
            logger=self.logger,
            converter=self._fake_convert,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.unavailable_sources, 1)
        self.assertEqual(result.removals, 1)
        self.assertTrue((self.media / "Existing_100.aif").exists())
        report_path = self.root / "Missing" / "SourceAudio Missing Audit.csv"
        self.assertIn("NOT PREPARED", report_path.read_text(encoding="utf-8-sig"))

    def test_matching_delivery_creates_no_missing_folder(self) -> None:
        self._write_metadata([("100", "Matching_100.aif")])
        self.media.mkdir(parents=True)
        (self.media / "Matching_100.aif").write_bytes(b"aiff")
        result = reconcile_sourceaudio_refresh(
            metadata_path=self.metadata,
            media_dir=self.media,
            source_dir=self.sources,
            logger=self.logger,
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.has_changes)
        self.assertFalse((self.root / "Missing").exists())

    def test_clean_refresh_archives_stale_missing_package(self) -> None:
        self._write_metadata([("100", "Matching_100.aif")])
        self.media.mkdir(parents=True)
        (self.media / "Matching_100.aif").write_bytes(b"aiff")
        stale = self.root / "Missing"
        stale.mkdir()
        (stale / "stale.aif").write_bytes(b"stale")

        result = reconcile_sourceaudio_refresh(
            metadata_path=self.metadata,
            media_dir=self.media,
            source_dir=self.sources,
            logger=self.logger,
        )
        self.assertTrue(result.ok)
        self.assertFalse(stale.exists())
        archives = list(self.root.glob("Missing-archived-*"))
        self.assertEqual(len(archives), 1)
        self.assertTrue((archives[0] / "stale.aif").exists())


if __name__ == "__main__":
    unittest.main()
