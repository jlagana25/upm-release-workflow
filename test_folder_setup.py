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


if __name__ == "__main__":
    unittest.main()
