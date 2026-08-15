"""Synthetic-filesystem tests for refreshed SourceAudio metadata handling."""

from __future__ import annotations

import csv
import logging
import tempfile
import unittest
from pathlib import Path

from sourceaudio_delta import (
    _derive_cover_url,
    _propagate_downloaded_audio,
    _write_unisync_request,
    reconcile_sourceaudio_refresh,
)


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

    def test_pending_delivery_reconciles_in_place(self) -> None:
        self._write_metadata([
            ("100", "Renamed_100.aif"),
            ("300", "Added_300.aif"),
        ])
        old = self.media / "Label" / "Album" / "Old_100.aif"
        removed = self.media / "Label" / "Album" / "Removed_200.aif"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old")
        removed.write_bytes(b"remove")
        master = self.sources / "Label" / "New Album" / "Master_300.wav"
        master.parent.mkdir(parents=True)
        master.write_bytes(b"new")

        result = reconcile_sourceaudio_refresh(
            metadata_path=self.metadata,
            media_dir=self.media,
            source_dir=self.sources,
            logger=self.logger,
            correction_package=False,
            converter=self._fake_convert,
        )
        self.assertTrue(result.ok)
        self.assertFalse(old.exists())
        self.assertFalse(removed.exists())
        self.assertEqual((old.parent / "Renamed_100.aif").read_bytes(), b"old")
        self.assertEqual(
            (self.media / "Label" / "New Album" / "Added_300.aif").read_bytes(),
            b"converted:new",
        )
        self.assertFalse((self.root / "Missing").exists())
        self.assertTrue((self.metadata.parent / "SourceAudio Refresh Audit.csv").exists())

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

    def test_unisync_request_uses_known_header_shape(self) -> None:
        self._write_metadata([
            ("100", "First_100.aif"),
            ("200", "Second_200.aif"),
        ])
        request = _write_unisync_request(self.metadata, {"200"})
        try:
            raw = request.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbfLabel,"))
            self.assertIn(b",workAudioId,Filename,", raw.splitlines()[0])
            self.assertNotIn(b"\r\n", raw)
            with request.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["workAudioId"], "200")
            self.assertEqual(rows[0]["Filename"], "Second_200.wav")
        finally:
            request.unlink(missing_ok=True)

    def test_propagates_canonical_wav_structure(self) -> None:
        client = self.root / "Canonical" / "WAV"
        source = client / "MEDIA" / "Label" / "ALB1 - Album" / "Track_100.wav"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"wav")
        destination = self.root / "WAV w COVERS" / "MEDIA"
        ok = _propagate_downloaded_audio(
            client, destination, {"100"}, self.logger
        )
        self.assertTrue(ok)
        self.assertEqual(
            (destination / "Label" / "ALB1 - Album" / "Track_100.wav").read_bytes(),
            b"wav",
        )

    def test_derives_new_webp_from_existing_cdn_structure(self) -> None:
        self.assertEqual(
            _derive_cover_url(
                "https://dams.cdn.unippm.com/AlbumImages/740x740/old.webp",
                "3907ad0e.jpg",
            ),
            "https://dams.cdn.unippm.com/AlbumImages/740x740/3907ad0e.webp",
        )


if __name__ == "__main__":
    unittest.main()
