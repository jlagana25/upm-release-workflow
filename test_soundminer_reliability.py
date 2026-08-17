import csv
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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

    def test_modal_progress_state_does_not_look_like_a_missing_window(self):
        unlocked = Mock(returncode=0, stdout='"CGSSessionScreenIsLocked"=No')
        modal = Mock(returncode=0, stdout="0|1|true\n", stderr="")
        with patch.object(soundminer.subprocess, "run", side_effect=[unlocked, modal]):
            soundminer._assert_soundminer_gui_available(
                self.logger, require_window=True
            )

    def test_zero_windows_without_frontmost_menu_fails_closed(self):
        unlocked = Mock(returncode=0, stdout='"CGSSessionScreenIsLocked"=No')
        missing_ui = Mock(returncode=0, stdout="0|1|false\n", stderr="")
        with (
            patch.object(
                soundminer.subprocess, "run", side_effect=[unlocked, missing_ui]
            ),
            self.assertRaises(soundminer._SoundminerError),
        ):
            soundminer._assert_soundminer_gui_available(
                self.logger, require_window=True
            )

    def test_directory_picker_selects_folder_from_parent(self):
        writes: list[str] = []
        fake = types.SimpleNamespace(
            hotkey=lambda *args: None,
            press=lambda *args: None,
            write=lambda value, **kwargs: writes.append(value),
        )
        with (
            patch.dict(sys.modules, {"pyautogui": fake}),
            patch.object(soundminer, "_set_clipboard", return_value=False),
            patch.object(soundminer, "_save_step_screenshot"),
            patch.object(soundminer.time, "sleep"),
        ):
            soundminer._open_panel_go_to_path(
                "/Volumes/Test/MEDIA",
                self.logger,
                select_directory=True,
            )
        self.assertEqual(writes, ["/Volumes/Test", "MEDIA"])

    def test_mirror_ok_uses_verified_logical_geometry(self):
        clicks: list[tuple[int, int]] = []
        fake = types.SimpleNamespace(
            click=lambda x, y: clicks.append((x, y)),
        )
        bounds = (100, 200, 600, 800)
        with (
            patch.dict(sys.modules, {"pyautogui": fake}),
            patch.object(soundminer, "_save_step_screenshot"),
            patch.object(
                soundminer,
                "_assert_mirror_dialog_visible",
                side_effect=soundminer._SoundminerError("closed"),
            ),
            patch.object(soundminer.time, "sleep"),
        ):
            soundminer._click_mirror_ok(self.logger, bounds)
        self.assertEqual(clicks, [soundminer._mirror_point(bounds, "ok")])


if __name__ == "__main__":
    unittest.main()
