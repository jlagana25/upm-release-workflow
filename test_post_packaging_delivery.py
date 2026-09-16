from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook

import delivery_common as common
import email_deliveries as email
import espn_delivery as espn
import post_packaging_delivery as runner
import outlook_connector_bridge as bridge
import soundexchange_delivery as sx


LOG = logging.getLogger("delivery-tests")


class FakeEspn:
    def __init__(self, states=(None, "active", "uploaded"), visible=False):
        self.states = list(states)
        self.visible = visible
        self.started = 0
        self.resumed = 0

    def require_authenticated(self): pass
    def transfer_status(self, _name):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]
    def destination_contains(self, _name): return self.visible or self.started > 0
    def start_folder_upload(self, _package, _destination): self.started += 1
    def resume_transfer(self, _name): self.resumed += 1
    def close(self): pass


class FakeSoundExchange:
    def __init__(self, expected):
        self.expected = expected
        self.current = None
        self.submitted = []
        self.imported = []

    def require_authenticated(self): pass
    def select_registrant(self, registrant): self.current = registrant
    def pending_count(self): return 0
    def bulk_import(self, workbook): self.imported.append(workbook.name)
    def wait_for_validation(self, expected_total, _timeout):
        rows = self.expected[self.current.key]
        assert len(rows) == expected_total
        return tuple(sx.ValidationEntry(str(i + 1), title, isrc, True) for i, (isrc, title) in enumerate(rows))
    def submit_recordings(self): self.submitted.append(self.current.key)
    def verify_upload_history(self, registrant, _count):
        return f"history-{registrant.key}" if registrant.key in self.submitted else None
    def close(self): pass


class FakeOutlook:
    def __init__(self): self.plan = None
    def create_draft(self, plan): self.plan = plan; return "draft-1"
    def _snapshot(self):
        return email.EmailSnapshot(
            "message-1", (self.plan.to,), (), (), self.plan.subject, self.plan.body,
            tuple((item.path.name, item.size, item.sha256) for item in self.plan.attachments),
        )
    def inspect_draft(self, _draft_id): return self._snapshot()
    def send_draft(self, _draft_id): pass
    def find_sent(self, _draft_id): return self._snapshot()


class MissingSentOutlook(FakeOutlook):
    def find_sent(self, _draft_id): return None


class PostPackagingDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ctx = SimpleNamespace(
            release_id="UPM20260912",
            specials_dir=self.root / "release",
            delivery_display_folder="Sep 12–25 2026",
            soundexchange_final_dir=self.root / "soundexchange",
            partner_metadata={},
        )

    def tearDown(self): self.temp.cleanup()

    def test_manifest_hash_changes_with_content(self):
        package = self.root / "package"
        package.mkdir()
        path = package / "file.wav"
        path.write_bytes(b"one")
        first = common.collect_manifest(package)
        path.write_bytes(b"two")
        second = common.collect_manifest(package)
        self.assertNotEqual(common.manifest_digest(first), common.manifest_digest(second))

    def test_checkpoint_refuses_changed_package(self):
        package = self.root / "package"
        package.mkdir()
        path = package / "file.wav"
        path.write_bytes(b"one")
        first = common.collect_manifest(package)
        common.checkpoint(self.ctx, "espn", first, "submitted", "started")
        path.write_bytes(b"two")
        with self.assertRaisesRegex(common.DeliverySafetyError, "changed"):
            common.checkpoint(self.ctx, "espn", common.collect_manifest(package), "submitted", "retry")

    def test_espn_upload_requires_history_and_destination(self):
        package = self.ctx.specials_dir / "3-FINAL PACKAGING" / "ESPN Exact"
        (package / "Music").mkdir(parents=True)
        (package / "Music" / "track.wav").write_bytes(b"audio")
        self.ctx.partner_folder_name = lambda name: "ESPN Exact" if name == "ESPN" else name
        gateway = FakeEspn()
        with patch.object(espn, "latest_workflow_gates", return_value=(True, "ok")), patch.object(
            espn, "set_partner_status"
        ) as state:
            ok = espn.deliver_espn(
                self.ctx, False, LOG, gateway=gateway,
                live_confirmation=self.ctx.release_id, poll_seconds=0.001,
            )
        self.assertTrue(ok)
        self.assertEqual(gateway.started, 1)
        state.assert_called_once_with(self.ctx.specials_dir, "espn", "uploaded")

    def _workbook(self, path: Path, isrc: str, title: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        book = Workbook()
        book.active.append(["ISRC", "Title"])
        book.active.append([isrc, title])
        book.save(path)

    def test_soundexchange_template_header_is_found_below_instructions(self):
        path = self.ctx.soundexchange_final_dir / "template.xlsx"
        path.parent.mkdir(parents=True, exist_ok=True)
        book = Workbook()
        sheet = book.active
        sheet.append([None, "ISRC Ingest File"])
        for _ in range(8):
            sheet.append([])
        sheet.append(["Artist (*1)", "Recording Title (*1)", "ISRC (*1)"])
        sheet.append(["Artist", "A Recording", "USAAA2600001"])
        book.save(path)
        from soundexchange_delivery import workbook_rows
        self.assertEqual(
            (("USAAA2600001", "A Recording"),), workbook_rows(path)
        )

    def test_soundexchange_processes_registrants_in_order(self):
        self._workbook(self.ctx.soundexchange_final_dir / "ISRC Ingest Form - MGB NA LLC - Part 1.xlsx", "USAAA2600001", "MGB")
        self._workbook(self.ctx.soundexchange_final_dir / "ISRC Ingest Form - Z TUNES LLC - Part 1.xlsx", "USAAA2600002", "Z")
        expected = {item.key: tuple(
            row for path in sx.registrant_workbooks(self.ctx, item) for row in sx.workbook_rows(path)
        ) for item in sx.REGISTRANTS}
        gateway = FakeSoundExchange(expected)
        with patch.object(sx, "latest_workflow_gates", return_value=(True, "ok")), patch.object(
            sx, "set_partner_status"
        ) as state:
            ok = sx.deliver_soundexchange(
                self.ctx, False, LOG, gateway=gateway,
                live_confirmation=self.ctx.release_id,
            )
        self.assertTrue(ok)
        self.assertEqual(gateway.submitted, ["mgb", "ztunes"])
        state.assert_called_once_with(self.ctx.specials_dir, "soundexchange", "delivered")

    def test_email_plan_preserves_small_source_and_sent_verification(self):
        source = self.root / "qwire.csv"
        source.write_text("Trackid,Title\n1,One\n", encoding="utf-8")
        self.ctx.partner_metadata["qwire"] = source
        gateway = FakeOutlook()
        with patch.object(email, "latest_workflow_gates", return_value=(True, "ok")), patch.object(
            email, "set_partner_status"
        ) as state:
            ok = email.deliver_email(
                self.ctx, "qwire", False, LOG, gateway=gateway,
                signature="Joe\nUniversal Production Music",
                live_confirmation=self.ctx.release_id,
            )
        self.assertTrue(ok)
        self.assertEqual(gateway.plan.attachments[0].path, source.resolve())
        self.assertIn("Sep 12–25 2026", gateway.plan.subject)
        self.assertIn("Thanks,\n\n\nJoe", gateway.plan.body)
        state.assert_called_once_with(self.ctx.specials_dir, "qwire", "delivered")

    def test_email_dry_run_does_not_write_workflow_artifacts(self):
        source = self.root / "scripps.csv"
        source.write_text("Trackid,Title\n1,One\n", encoding="utf-8")
        self.ctx.partner_metadata["scripps"] = source
        self.assertTrue(email.deliver_email(self.ctx, "scripps", True, LOG))
        self.assertFalse((self.ctx.specials_dir / "_WORKFLOW").exists())

    def test_email_uncertain_send_refuses_duplicate(self):
        source = self.root / "qwire.csv"
        source.write_text("Trackid,Title\n1,One\n", encoding="utf-8")
        self.ctx.partner_metadata["qwire"] = source
        gateway = MissingSentOutlook()
        with patch.object(email, "latest_workflow_gates", return_value=(True, "ok")):
            self.assertFalse(email.deliver_email(
                self.ctx, "qwire", False, LOG, gateway=gateway,
                signature="Signature", live_confirmation=self.ctx.release_id,
            ))
            with patch.object(gateway, "create_draft", side_effect=AssertionError("duplicate")):
                self.assertFalse(email.deliver_email(
                    self.ctx, "qwire", False, LOG, gateway=gateway,
                    signature="Signature", live_confirmation=self.ctx.release_id,
                ))

    def test_outlook_connector_bridge_records_only_verified_boundaries(self):
        source = self.root / "qwire.csv"
        source.write_text("Trackid,Title\n1,One\n", encoding="utf-8")
        self.ctx.partner_metadata["qwire"] = source
        with patch.object(bridge, "latest_workflow_gates", return_value=(True, "ok")), patch.object(
            bridge, "load_private_signature", return_value="Joe\nUniversal Production Music"
        ), patch.object(bridge, "set_partner_status") as state:
            handoff_path, _plan = bridge.prepare_handoff(self.ctx, "qwire")
            self.assertTrue(handoff_path.is_file())
            bridge.record_verified_draft(self.ctx, "qwire", "draft-1")
            with self.assertRaisesRegex(common.DeliverySafetyError, self.ctx.release_id):
                bridge.authorize_send(self.ctx, "qwire", "draft-1", "wrong")
            bridge.authorize_send(
                self.ctx, "qwire", "draft-1", self.ctx.release_id
            )
            receipt_path = bridge.record_verified_sent_item(
                self.ctx, "qwire", "draft-1", "sent-1", self.ctx.release_id
            )
        self.assertTrue(receipt_path.is_file())
        state.assert_called_once_with(self.ctx.specials_dir, "qwire", "delivered")

    def test_outlook_signature_enrollment_is_private_and_not_argv_based(self):
        values = iter(["Joe Example", "Universal Production Music", "."])
        with patch.object(bridge, "PRIVATE_STATE_DIR", self.root / "private"):
            path = bridge.enroll_private_signature(input_func=lambda: next(values))
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            "Joe Example\nUniversal Production Music\n",
        )
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_runner_requires_exact_live_confirmation(self):
        with self.assertRaisesRegex(common.DeliverySafetyError, self.ctx.release_id):
            runner.run_deliveries(
                self.ctx, ("espn",), LOG, dry_run=False,
                live_confirmation="wrong", runners={"espn": lambda *_: True},
            )

    def test_runner_skips_already_delivered_endpoint(self):
        self.ctx.specials_dir.mkdir(parents=True)
        from delivery_state import set_partner_status
        set_partner_status(self.ctx.specials_dir, "qwire", "delivered")
        called = []
        result = runner.run_deliveries(
            self.ctx, ("qwire",), LOG, dry_run=True,
            runners={"qwire": lambda *_: called.append(True) or True},
        )
        self.assertEqual(result, {"qwire": "already_delivered"})
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
