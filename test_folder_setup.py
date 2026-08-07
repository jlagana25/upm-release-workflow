import logging
import tempfile
import unittest
from pathlib import Path

import config
import folder_setup


class RetiredPartnerFolderTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger(self.id())

    @staticmethod
    def _build_source(root: Path) -> Path:
        source = root / "baseline"
        retained = source / "3-FINAL PACKAGING" / "Current Partner"
        retired = (
            source
            / "3-FINAL PACKAGING"
            / "Universal Production Music MMMM YYYY Release - MTV-Viacom"
        )
        retained.mkdir(parents=True)
        retired.mkdir(parents=True)
        (retained / "keep.txt").write_text("keep", encoding="utf-8")
        (retired / "retired.txt").write_text("retired", encoding="utf-8")
        return source

    def test_retired_name_matching_is_case_and_punctuation_insensitive(self):
        self.assertTrue(config.is_retired_partner_name("MTV-Viacom"))
        self.assertTrue(config.is_retired_partner_name("release - mtv viacom"))
        self.assertFalse(config.is_retired_partner_name("Current Partner"))

    def test_fresh_baseline_copy_excludes_retired_partner(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self._build_source(root)
            destination = root / "release"
            self.assertTrue(
                folder_setup._safe_copytree(
                    source, destination, False, False, "test", self.logger
                )
            )
            self.assertTrue(
                (
                    destination
                    / "3-FINAL PACKAGING"
                    / "Current Partner"
                    / "keep.txt"
                ).exists()
            )
            self.assertFalse(any(destination.rglob("*MTV*")))

    def test_additive_baseline_merge_excludes_retired_partner(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self._build_source(root)
            destination = root / "release"
            destination.mkdir()
            (destination / "seed.csv").write_text("header\n", encoding="utf-8")
            self.assertTrue(
                folder_setup._safe_copytree(
                    source, destination, False, False, "test", self.logger
                )
            )
            self.assertTrue(
                (
                    destination
                    / "3-FINAL PACKAGING"
                    / "Current Partner"
                    / "keep.txt"
                ).exists()
            )
            self.assertFalse(any(destination.rglob("*MTV*")))

    def test_part_and_range_delivery_folder_names_are_normalized(self):
        cases = [
            (
                config.ReleaseContext(2026, 8, 1),
                "Universal Production Music August 2026 Part 1 - NBC",
            ),
            (
                config.ReleaseContext.for_date_range("2026-09-29", "2026-10-12"),
                "Universal Production Music September 29–October 12 2026 Releases - NBC",
            ),
        ]
        for ctx, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                (root / f"Universal Production Music {ctx.month_display_folder} Release - NBC").mkdir()
                (root / f"UPM Japan NTT DATA {ctx.month_display_folder} Release").mkdir()
                folder_setup._normalize_delivery_folder_names(root, ctx, False, self.logger)
                self.assertTrue((root / expected).is_dir())
                self.assertTrue(
                    (root / ctx.partner_folder_name("Japan NTT DATA")).is_dir()
                )


if __name__ == "__main__":
    unittest.main()
