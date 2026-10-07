import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import ReleaseContext
from release_storage_consolidation import consolidate_release


class ReleaseStorageConsolidationTests(unittest.TestCase):
    def test_context_keeps_all_generated_release_output_under_main_root(self):
        ctx = ReleaseContext.for_date_range("2026-09-12", "2026-09-25")
        self.assertTrue(ctx.hd_staging_dir.is_relative_to(ctx.specials_dir))
        self.assertTrue(ctx.hd_final_dir.is_relative_to(ctx.specials_dir))
        self.assertTrue(ctx.soundmouse_release_dir.is_relative_to(ctx.specials_dir))
        self.assertEqual("Hard Drive Updates", ctx.hd_staging_dir.name)
        self.assertEqual("2-STAGING", ctx.hd_staging_dir.parent.name)
        self.assertEqual("Hard Drive Updates", ctx.hd_final_dir.name)
        self.assertEqual("3-FINAL PACKAGING", ctx.hd_final_dir.parent.name)
        self.assertTrue(ctx.soundmouse_release_dir.name.endswith(" - SoundMouse"))
        self.assertEqual("3-FINAL PACKAGING", ctx.soundmouse_release_dir.parent.name)

    def test_consolidates_and_rewrites_absolute_workflow_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy_sm = root / "legacy-sm"
            legacy_stage = root / "legacy-stage"
            legacy_final = root / "legacy-final"
            for component in (legacy_sm, legacy_stage, legacy_final):
                component.mkdir()
                (component / "nested").mkdir()
                (component / "nested" / "file.bin").write_bytes(component.name.encode())

            ctx = ReleaseContext.for_date_range("2026-09-12", "2026-09-25")
            ctx.specials_dir = root / "main" / "UPM-2026-09-12"
            ctx.specials_dir.mkdir(parents=True)
            ctx.soundmouse_release_dir = (
                ctx.specials_dir / "3-FINAL PACKAGING" / "SoundMouse Package"
            )
            ctx.hd_staging_dir = ctx.specials_dir / "2-STAGING" / "Hard Drive Updates"
            ctx.hd_final_dir = (
                ctx.specials_dir / "3-FINAL PACKAGING" / "Hard Drive Updates"
            )
            workflow = ctx.specials_dir / "_WORKFLOW"
            workflow.mkdir()
            (workflow / "state.json").write_text(json.dumps({
                "soundmouse": str(legacy_sm / "nested" / "file.bin"),
                "hd": str(legacy_final / "nested" / "file.bin"),
            }), encoding="utf-8")
            logs = root / "logs"

            with (
                patch("release_storage_consolidation.LEGACY_SOUNDMOUSE_BASE", legacy_sm.parent),
                patch("release_storage_consolidation.LEGACY_HD_STAGING_BASE", legacy_stage.parent),
                patch("release_storage_consolidation.LEGACY_HD_FINAL_BASE", legacy_final.parent),
                patch("release_storage_consolidation.component_operations", return_value=(
                    ("SoundMouse", (legacy_sm,), ctx.soundmouse_release_dir),
                    ("HD Staging", (legacy_stage,), ctx.hd_staging_dir),
                    ("HD Final", (legacy_final,), ctx.hd_final_dir),
                )),
            ):
                result = consolidate_release(
                    ctx,
                    execute=True,
                    confirmation=ctx.release_id,
                    logger=logging.getLogger("storage-consolidation"),
                    logs_dir=logs,
                )

            self.assertEqual({
                "SoundMouse": "consolidated",
                "HD Staging": "consolidated",
                "HD Final": "consolidated",
            }, result)
            self.assertFalse(legacy_sm.exists())
            self.assertFalse(legacy_stage.exists())
            self.assertFalse(legacy_final.exists())
            self.assertTrue((ctx.soundmouse_release_dir / "nested" / "file.bin").is_file())
            state = json.loads((workflow / "state.json").read_text())
            self.assertIn(str(ctx.soundmouse_release_dir), state["soundmouse"])
            self.assertIn(str(ctx.hd_final_dir), state["hd"])
            audit = json.loads((workflow / "storage_consolidation.json").read_text())
            self.assertEqual(ctx.release_id, audit["release_id"])


if __name__ == "__main__":
    unittest.main()
