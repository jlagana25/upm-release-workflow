import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import ai_team_demo
import domo_exports


class AiTeamDemoTests(unittest.TestCase):
    def test_exact_us_tracklist_key_does_not_select_exus(self) -> None:
        args = type("Args", (), {"only": "us_tracklist"})()
        cards = domo_exports._select_cards(args, __import__("logging").getLogger("test"))
        self.assertIsNotNone(cards)
        self.assertEqual(["us_tracklist"], [card["key"] for card in cards or []])

    def test_dry_run_builds_authentic_sourceaudio_demo_plan(self) -> None:
        output = StringIO()
        with patch("ai_team_demo.subprocess.run") as run:
            with redirect_stdout(output):
                result = ai_team_demo.main(
                    [
                        "--previous-month",
                        "--year",
                        "2026",
                        "--month",
                        "8",
                        "--dry-run",
                    ]
                )

        self.assertEqual(0, result)
        run.assert_not_called()
        plan = output.getvalue()
        self.assertIn("--only sourceaudio_metadata", plan)
        self.assertIn("20 varied SourceAudio tracks", plan)
        self.assertIn("--job 'US WAV'", plan)
        self.assertIn("--timeout 0.06", plan)
        self.assertNotIn("--no-unisync-xml-setup", plan)
        self.assertIn("1-ORIGINAL", plan)
        self.assertIn("WAV w COVERS", plan)
        self.assertIn("3-FINAL PACKAGING", plan)
        self.assertIn("HDF1 login-session agent", plan)

    def test_workaudioids_use_shared_column_detection(self) -> None:
        import csv
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "metadata.csv"
            with metadata.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["Filename", "workAudioId"])
                writer.writerows([["one", "101"], ["two", "102"], ["copy", "101"]])

            self.assertEqual({"101", "102"}, ai_team_demo._workaudioids(metadata))

    def test_source_files_rejects_duplicate_leaf_filenames(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "album_a").mkdir()
            (root / "album_b").mkdir()
            (root / "album_a" / "same.mp3").write_bytes(b"a")
            (root / "album_b" / "same.mp3").write_bytes(b"b")

            with self.assertRaisesRegex(RuntimeError, "Duplicate staged filenames"):
                ai_team_demo._source_files(root, {"same.mp3"})


if __name__ == "__main__":
    unittest.main()
