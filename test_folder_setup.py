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
        nbc = (
            source
            / "3-FINAL PACKAGING"
            / "Universal Production Music MMMM YYYY Release - NBC"
        )
        nbc.mkdir(parents=True)
        (retained / "keep.txt").write_text("keep", encoding="utf-8")
        (retired / "retired.txt").write_text("retired", encoding="utf-8")
        (nbc / "retired.txt").write_text("retired", encoding="utf-8")
        return source

    def test_retired_name_matching_is_case_and_punctuation_insensitive(self):
        self.assertTrue(config.is_retired_partner_name("MTV-Viacom"))
        self.assertTrue(config.is_retired_partner_name("release - mtv viacom"))
        self.assertTrue(config.is_retired_partner_name("NBCUniversal"))
        self.assertTrue(config.is_retired_partner_name("Release - NBC"))
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
            self.assertFalse(any(destination.rglob("*NBC*")))

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
            self.assertFalse(any(destination.rglob("*NBC*")))

    def test_rolling_baseline_merges_around_early_monthly_packages(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self._build_source(root)
            destination = root / "release"
            monthly = destination / "3-FINAL PACKAGING" / "Monthly Partner"
            monthly.mkdir(parents=True)
            (monthly / "delivered.xlsx").write_text("monthly", encoding="utf-8")
            marker = destination / folder_setup._EARLY_MONTHLY_MARKER
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("{}\n", encoding="utf-8")

            self.assertTrue(folder_setup._safe_copytree(
                source, destination, False, True, "test", self.logger
            ))
            self.assertTrue((monthly / "delivered.xlsx").exists())
            self.assertTrue((
                destination / "3-FINAL PACKAGING" / "Current Partner" / "keep.txt"
            ).exists())
            self.assertFalse(marker.exists())

    def test_non_triggering_run_excludes_monthly_metadata_partner_trees(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self._build_source(root)
            final = source / "3-FINAL PACKAGING"
            for name in (
                "UPM Japan NTT DATA MMMM YYYY Release",
                "UPM Japan JMD and TSS MMMM YYYY Release",
                "Universal Production Music MMMM YYYY Release - Qwire",
                "Universal Production Music MMMM YYYY Release - Scripps",
            ):
                folder = final / name
                folder.mkdir()
                (folder / "metadata.csv").write_text("header\n", encoding="utf-8")
            destination = root / "release"
            self.assertTrue(
                folder_setup._safe_copytree(
                    source,
                    destination,
                    False,
                    False,
                    "test",
                    self.logger,
                    exclude_monthly_metadata=True,
                )
            )
            self.assertFalse(any(destination.rglob("*Qwire*")))
            self.assertFalse(any(destination.rglob("*Scripps*")))
            self.assertFalse(any(destination.rglob("*JMD*")))
            self.assertFalse(any(destination.rglob("*NTT*")))

    def test_part_and_range_delivery_folder_names_are_normalized(self):
        cases = [
            (
                config.ReleaseContext(2026, 8, 1),
                "Universal Production Music August 2026 Part 1 - ESPN",
            ),
            (
                config.ReleaseContext.for_date_range("2026-09-29", "2026-10-12"),
                "Universal Production Music Sep 29–Oct 12 2026 Releases - ESPN",
            ),
        ]
        for ctx, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                (root / f"Universal Production Music {ctx.delivery_display_folder} Release - ESPN").mkdir()
                (root / f"UPM Japan NTT DATA {ctx.delivery_display_folder} Release").mkdir()
                folder_setup._normalize_delivery_folder_names(root, ctx, False, self.logger)
                self.assertTrue((root / expected).is_dir())
                self.assertTrue(
                    (root / ctx.partner_folder_name("Japan NTT DATA")).is_dir()
                )

    def test_standalone_monthly_folder_is_minimal_and_restartable(self):
        ctx = config.ReleaseContext.for_monthly_delivery("2026-10-01")
        with tempfile.TemporaryDirectory() as raw:
            ctx.specials_dir = Path(raw) / ctx.storage_root
            ctx.partner_dirs = ctx._build_partner_dirs()
            ctx.japan_metadata_csv = (
                ctx.partner_dirs["japan_final_media"].parent
                / "September 2026 NTT Data Metadata.csv"
            )
            ctx.partner_metadata = ctx._build_partner_metadata()

            self.assertTrue(
                folder_setup.create_monthly_delivery_folder(
                    ctx, False, self.logger
                )
            )
            self.assertTrue(
                (ctx.specials_dir / "1-ORIGINAL" / "Music" / "Japan" / "MEDIA").is_dir()
            )
            self.assertTrue(ctx.partner_dirs["japan_final_media"].is_dir())
            self.assertTrue(ctx.partner_metadata["qwire"].parent.is_dir())
            self.assertTrue(ctx.partner_metadata["scripps"].parent.is_dir())
            self.assertFalse((ctx.specials_dir / "1-ORIGINAL" / "Music" / "MP3").exists())

            # A resume preserves existing work and remains successful.
            marker = ctx.partner_dirs["japan_final_media"] / "existing.wav"
            marker.write_bytes(b"audio")
            self.assertTrue(
                folder_setup.create_monthly_delivery_folder(
                    ctx, False, self.logger
                )
            )
            self.assertEqual(marker.read_bytes(), b"audio")


if __name__ == "__main__":
    unittest.main()
