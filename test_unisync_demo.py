import csv
import logging
import tempfile
import unittest
from pathlib import Path

from unisync_automation import _write_limited_test_csv


class UniSyncDemoTests(unittest.TestCase):
    def test_limited_csv_preserves_header_and_requested_row_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "tracklist.csv"
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["Filename", "workAudioId"])
                writer.writerows(
                    [["one", "1"], ["two", "2"], ["three", "3"], ["four", "4"]]
                )

            limited = _write_limited_test_csv(
                str(source), 3, logging.getLogger("test")
            )
            self.assertIsNotNone(limited)
            assert limited is not None
            self.addCleanup(limited.unlink, missing_ok=True)

            with limited.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle))

            self.assertEqual(["Filename", "workAudioId"], rows[0])
            self.assertEqual(3, len(rows) - 1)
            self.assertEqual(["one", "two", "three"], [row[0] for row in rows[1:]])

    def test_limited_csv_prioritizes_different_labels_and_albums(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "tracklist.csv"
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["Label", "AlbumNo", "Filename"])
                writer.writerows(
                    [
                        ["Label A", "A1", "a1_track_1"],
                        ["Label A", "A1", "a1_track_2"],
                        ["Label A", "A2", "a2_track_1"],
                        ["Label B", "B1", "b1_track_1"],
                        ["Label C", "C1", "c1_track_1"],
                    ]
                )

            limited = _write_limited_test_csv(
                str(source), 4, logging.getLogger("test")
            )
            self.assertIsNotNone(limited)
            assert limited is not None
            self.addCleanup(limited.unlink, missing_ok=True)

            with limited.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(4, len(rows))
            self.assertEqual(3, len({row["Label"] for row in rows}))
            self.assertEqual(4, len({row["AlbumNo"] for row in rows}))
            self.assertNotIn("a1_track_2", {row["Filename"] for row in rows})


if __name__ == "__main__":
    unittest.main()
