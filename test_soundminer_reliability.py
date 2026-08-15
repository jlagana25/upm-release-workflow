import csv
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import soundminer


class SoundminerReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.logger = logging.getLogger(self.id())

    def tearDown(self):
        self.tmp.cleanup()

    def _write_nbc_csv(self, rows):
        path = self.root / "nbc.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["Filename", "Source", "TrackTitle"]
            )
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_nbc_preflight_builds_exact_output_manifest(self):
        audio = self.root / "MEDIA"
        audio.mkdir()
        (audio / "input-one.wav").touch()
        (audio / "input-two.WAV").touch()
        metadata = self._write_nbc_csv([
            {"Filename": "input-one.wav", "Source": "ABC 1", "TrackTitle": "First Song"},
            {"Filename": "input-two.wav", "Source": "ABC 2", "TrackTitle": "Second Song"},
        ])
        result = soundminer._validate_nbc_source_manifest(
            metadata, audio, self.logger
        )
        self.assertEqual(result, {"abc 1_first song", "abc 2_second song"})

    def test_nbc_preflight_fails_before_gui_for_missing_audio(self):
        audio = self.root / "MEDIA"
        audio.mkdir()
        metadata = self._write_nbc_csv([
            {"Filename": "missing.wav", "Source": "ABC 1", "TrackTitle": "Missing"},
        ])
        with (
            patch.object(soundminer, "RUNTIME_DIR", self.root / "runtime"),
            self.assertRaises(soundminer._SoundminerError),
        ):
            soundminer._validate_nbc_source_manifest(metadata, audio, self.logger)

    def test_destination_manifest_rejects_equal_count_wrong_file(self):
        destination = self.root / "dest"
        destination.mkdir()
        (destination / "expected-one.wav").touch()
        (destination / "wrong-two.wav").touch()
        with (
            patch.object(soundminer, "RUNTIME_DIR", self.root / "runtime"),
            self.assertRaises(soundminer._SoundminerError),
        ):
            soundminer._validate_destination_manifest(
                destination,
                {"expected-one", "expected-two"},
                ("wav",),
                self.logger,
                "NBC mirror",
            )

    def test_destination_manifest_allows_missing_only_incremental_refresh(self):
        destination = self.root / "dest"
        destination.mkdir()
        (destination / "expected-one.wav").touch()
        state = soundminer._validate_destination_manifest(
            destination,
            {"expected-one", "expected-two"},
            ("wav",),
            self.logger,
            "NBC mirror",
            allow_partial=True,
        )
        self.assertEqual(state, "partial")

    def test_unmatched_dialog_allowlist_rejects_new_field(self):
        allowed = (
            "Unmatched Fields | Warning...The following field headers aren't "
            "in the database: is_SongBasedonLyrics,HasVocals,Is_Explicit "
            "If you expected to use these fields"
        )
        self.assertEqual(
            soundminer._validate_unmatched_dialog_text(allowed),
            soundminer.ALLOWED_UNMATCHED_FIELDS,
        )
        unexpected = allowed.replace("Is_Explicit", "NewSensitiveField")
        with self.assertRaises(soundminer._SoundminerError):
            soundminer._validate_unmatched_dialog_text(unexpected)

    def test_console_lock_parser_distinguishes_locked_session(self):
        self.assertTrue(soundminer._ioreg_reports_locked(
            '"CGSSessionScreenIsLocked"=Yes,"kCGSSessionOnConsoleKey"=Yes'
        ))
        self.assertFalse(soundminer._ioreg_reports_locked(
            '"CGSSessionScreenIsLocked"=No,"kCGSSessionOnConsoleKey"=Yes'
        ))


if __name__ == "__main__":
    unittest.main()
