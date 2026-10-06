import json
import logging
import tempfile
import unittest
from pathlib import Path

from config import ReleaseContext
from delivery_common import checkpoint
from delivery_state import partner_status, set_partner_status
import soundmouse_web_delivery as web


LOG = logging.getLogger("test-soundmouse-web-delivery")


def result(name: str, *, errors: int = 0) -> web.ProcessingResult:
    return web.ProcessingResult(
        filename=name,
        tracks=10,
        updates=10,
        new_tracks=0,
        missing_audio=errors,
        missing_recommended=10,
        missing_artwork=0,
        missing_mandatory=0,
        duplicate_filenames=0,
        manually_edited=0,
        missing_titles=0,
        empty_filenames=0,
        last_changed="2 October 2026 • 18:50:56",
    )


class FakeGateway:
    def __init__(self, names, *, error_name=None):
        self.names = tuple(names)
        self.error_name = error_name
        self.processed = []

    def available_workbooks(self):
        return self.names

    def process_workbook(self, filename):
        self.processed.append(filename)
        return result(filename, errors=1 if filename == self.error_name else 0)

    def close(self):
        pass


class SoundMouseWebDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ctx = ReleaseContext.for_date_range("2026-09-12", "2026-09-25")
        self.ctx.specials_dir = self.root / "specials"
        self.ctx.soundmouse_release_dir = self.root / "soundmouse"
        metadata = self.ctx.soundmouse_release_dir / "Metadata"
        metadata.mkdir(parents=True)
        self.names = (
            "SoundMouseMetadata 01 - ALL.xlsx",
            "SoundMouseMetadata 02 - UK DE SE OZ.xlsx",
        )
        for name in self.names:
            (metadata / name).write_bytes(b"xlsx")

    def tearDown(self):
        self.temporary.cleanup()

    def uploaded(self):
        workflow = self.ctx.specials_dir / "_WORKFLOW"
        workflow.mkdir(parents=True, exist_ok=True)
        (workflow / "soundmouse_uploader_receipt.json").write_text(
            json.dumps({
                "release_id": self.ctx.release_id,
                "files": [{"path": f"Metadata/{name}"} for name in self.names],
            }),
            encoding="utf-8",
        )
        set_partner_status(self.ctx.specials_dir, "soundmouse", "uploaded")

    def test_parse_review_accepts_recommended_warnings_only(self):
        text = """
        Summary: Warning:
        0 Tracks missing a corresponding audio file.
        68 Tracks missing recommended metadata.
        0 Albums missing artwork.
        0 Tracks missing mandatory metadata.
        0 Duplicate filenames in spreadsheet.
        0 Manually edited tracks to be updated.
        0 Tracks missing title field in spreadsheet.
        0 Tracks with empty filenames in spreadsheet.
        Tracks in spreadsheet. 68 New 0 Update 68
        Albums in spreadsheet. 1 New 0 Update 1
        Libraries in spreadsheet. 1 New 0 Update 1
        """
        parsed = web.parse_review(self.names[0], text)
        self.assertEqual(parsed.tracks, 68)
        self.assertEqual(parsed.updates, 68)
        self.assertEqual(parsed.missing_recommended, 68)
        self.assertEqual(parsed.blocking_errors, 0)

    def test_processes_every_uploaded_workbook_and_marks_delivered(self):
        self.uploaded()
        gateway = FakeGateway(self.names)
        self.assertTrue(web.deliver_soundmouse_web(
            self.ctx,
            False,
            LOG,
            gateway=gateway,
            live_confirmation=self.ctx.release_id,
        ))
        self.assertEqual(gateway.processed, list(self.names))
        self.assertEqual(partner_status(self.ctx.specials_dir, "soundmouse"), "delivered")
        receipt_path = self.ctx.specials_dir / "_WORKFLOW" / "soundmouse_delivery_receipt.json"
        self.assertTrue(receipt_path.is_file())
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["details"]["completed_workbooks"], list(self.names))

    def test_resume_skips_manifest_bound_completed_workbook(self):
        self.uploaded()
        manifest = web.metadata_manifest(self.ctx)
        checkpoint(
            self.ctx,
            "soundmouse",
            manifest,
            "website_processing",
            "first complete",
            remote_ids=(self.names[0],),
        )
        gateway = FakeGateway(self.names)
        self.assertTrue(web.deliver_soundmouse_web(
            self.ctx,
            False,
            LOG,
            gateway=gateway,
            live_confirmation=self.ctx.release_id,
        ))
        self.assertEqual(gateway.processed, [self.names[1]])

    def test_blocking_error_leaves_uploaded_state(self):
        self.uploaded()
        gateway = FakeGateway(self.names, error_name=self.names[0])
        self.assertFalse(web.deliver_soundmouse_web(
            self.ctx,
            False,
            LOG,
            gateway=gateway,
            live_confirmation=self.ctx.release_id,
        ))
        self.assertEqual(partner_status(self.ctx.specials_dir, "soundmouse"), "uploaded")

    def test_dry_run_does_not_require_uploader_receipt(self):
        self.assertTrue(web.deliver_soundmouse_web(self.ctx, True, LOG))
        self.assertFalse((self.ctx.specials_dir / "_WORKFLOW").exists())


if __name__ == "__main__":
    unittest.main()
