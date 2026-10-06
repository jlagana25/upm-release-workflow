from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from openpyxl import Workbook

import delivery_common as common
from config import ReleaseContext, context_from_cli_args
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
    def verify_upload_history(self, registrant, _expected_isrcs):
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

    def test_monthly_context_and_report_gate_aliases(self):
        args = SimpleNamespace(
            delivery_date="2026-10-01", previous_month=False,
            year=None, month=None, part=None, start_date=None, end_date=None,
            full_month_content=False,
        )
        ctx = context_from_cli_args(args)
        self.assertTrue(ctx.is_monthly_delivery)
        self.assertEqual(ctx.release_id, "UPM-2026-09-MONTHLY")

        logs = self.root / "logs"
        report_dir = logs / "reports" / ctx.release_id
        report_dir.mkdir(parents=True)
        (report_dir / "run-20261001-120000.json").write_text(json.dumps({
            "run": {"dry_run": False},
            "steps": {
                "final_packaging": "completed",
                "final_verification": "completed",
            },
        }), encoding="utf-8")
        ok, _detail = common.latest_workflow_gates(
            ctx, ("10 Final packaging", "15 Final metadata check"),
            logs_dir=logs,
        )
        self.assertTrue(ok)

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
        state.assert_called_once_with(self.ctx.specials_dir, "espn", "delivered")

    def test_espn_transfer_status_uses_exact_completion_phrase(self):
        self.assertEqual(
            "uploaded",
            espn.classify_transfer_text(
                "Uploaded 1 file(s) Universal Production Music Sep 1–11 2026 Releases - ESPN"
            ),
        )
        self.assertEqual("interrupted", espn.classify_transfer_text("Transfer interrupted"))

    def test_espn_native_picker_selects_exact_directory_from_parent(self):
        package = self.root / "Universal Production Music Sep 12–25 2026 Releases - ESPN"
        package.mkdir()
        gui = Mock()
        with (
            patch.dict(sys.modules, {"pyautogui": gui}),
            patch.object(espn.time, "sleep"),
            patch.object(
                espn.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ) as run,
        ):
            espn._select_native_folder(package)
        run.assert_called_once_with(
            ["/usr/bin/pbcopy"], input=str(package), text=True, check=False,
            stdout=espn.subprocess.DEVNULL, stderr=espn.subprocess.DEVNULL,
        )
        self.assertEqual(
            gui.hotkey.call_args_list,
            [
                call("command", "shift", "g"),
                call("command", "a"),
                call("command", "v"),
                call("command", "up"),
            ],
        )
        self.assertEqual(gui.press.call_args_list, [call("enter"), call("enter")])

    def test_espn_launches_installed_signiant_app_by_exact_path(self):
        app = self.root / "SigniantApp.app"
        app.mkdir()
        with (
            patch.object(espn, "SIGNIANT_APP_CANDIDATES", (app,)),
            patch.object(
                espn.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ) as run,
        ):
            self.assertEqual(espn._launch_signiant_app(), app)
        run.assert_called_once_with(
            ["/usr/bin/open", str(app)],
            check=False,
            stdout=espn.subprocess.DEVNULL,
            stderr=espn.subprocess.DEVNULL,
        )

    def test_espn_waits_for_signiant_handoff_confirmation(self):
        page = Mock()
        upload = Mock()
        upload.count.return_value = 1
        upload.first.is_enabled.return_value = True
        confirmation = Mock()
        confirmation.count.return_value = 1
        confirmation.first.is_visible.return_value = True
        add_files = Mock()
        add_files.count.return_value = 0
        page.get_by_text.side_effect = (upload, confirmation, add_files)
        espn._begin_signiant_handoff(page)
        upload.first.click.assert_called_once_with()
        confirmation.first.click.assert_called_once_with()

    def test_espn_uses_current_inline_add_files_handoff(self):
        page = Mock()
        upload = Mock()
        upload.count.return_value = 1
        upload.first.is_enabled.return_value = True
        confirmation = Mock()
        confirmation.count.return_value = 0
        add_files = Mock()
        add_files.count.return_value = 1
        add_files.first.is_visible.return_value = True
        page.get_by_text.side_effect = (upload, confirmation, add_files)
        espn._begin_signiant_handoff(page)
        upload.first.click.assert_called_once_with()
        add_files.first.click.assert_called_once_with()

    def test_espn_refuses_missing_signiant_handoff_confirmation(self):
        page = Mock()
        upload = Mock()
        upload.count.return_value = 1
        upload.first.is_enabled.return_value = True
        confirmation = Mock()
        confirmation.count.return_value = 0
        add_files = Mock()
        add_files.count.return_value = 0
        page.get_by_text.side_effect = (upload, confirmation, add_files)
        with (
            patch.object(espn.time, "monotonic", side_effect=(0, 31)),
            self.assertRaisesRegex(espn.EspnDeliveryError, "did not present"),
        ):
            espn._begin_signiant_handoff(page)

    def test_espn_does_not_checkpoint_before_signiant_accepts_folder(self):
        package = self.ctx.specials_dir / "3-FINAL PACKAGING" / "ESPN Exact"
        (package / "Music").mkdir(parents=True)
        (package / "Music" / "track.wav").write_bytes(b"audio")
        self.ctx.partner_folder_name = lambda name: "ESPN Exact" if name == "ESPN" else name
        gateway = FakeEspn()
        gateway.start_folder_upload = Mock(side_effect=espn.EspnDeliveryError("picker failed"))
        with patch.object(espn, "latest_workflow_gates", return_value=(True, "ok")):
            self.assertFalse(espn.deliver_espn(
                self.ctx, False, LOG, gateway=gateway,
                live_confirmation=self.ctx.release_id,
            ))
        self.assertFalse(common.endpoint_state_path(self.ctx, "espn").exists())

    def test_espn_refuses_duplicate_after_signiant_accepted_submission(self):
        package = self.ctx.specials_dir / "3-FINAL PACKAGING" / "ESPN Exact"
        (package / "Music").mkdir(parents=True)
        (package / "Music" / "track.wav").write_bytes(b"audio")
        self.ctx.partner_folder_name = lambda name: "ESPN Exact" if name == "ESPN" else name
        files = common.collect_manifest(package, include_root=True)
        common.checkpoint(
            self.ctx, "espn", files, "submitted",
            "folder upload accepted by Signiant",
        )
        gateway = FakeEspn(states=(None,), visible=False)
        with patch.object(espn, "latest_workflow_gates", return_value=(True, "ok")):
            self.assertFalse(espn.deliver_espn(
                self.ctx, False, LOG, gateway=gateway,
                live_confirmation=self.ctx.release_id,
            ))
        self.assertEqual(gateway.started, 0)

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

    def test_soundexchange_history_csv_requires_exact_isrc_column(self):
        path = self.root / "history.csv"
        path.write_text(
            "Recording Title,Sound Recording ISRC\nA Recording,US-AAA-26-00001\n",
            encoding="utf-8",
        )
        self.assertEqual(("USAAA2600001",), sx._history_csv_isrcs(path))

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

    def test_unified_runner_uses_outlook_gateway_and_reports_delivered(self):
        source = self.root / "qwire.csv"
        source.write_text("Trackid,Title\n1,One\n", encoding="utf-8")
        self.ctx.partner_metadata["qwire"] = source
        gateway = FakeOutlook()
        with patch.object(email, "latest_workflow_gates", return_value=(True, "ok")):
            runners = runner._implemented_runners(
                live_confirmation=self.ctx.release_id,
                outlook_gateway=gateway,
                signature="Joe\nUniversal Production Music",
            )
            result = runner.run_deliveries(
                self.ctx,
                ("qwire",),
                LOG,
                dry_run=False,
                live_confirmation=self.ctx.release_id,
                runners=runners,
                progress_sync=lambda *_: True,
            )
        self.assertEqual(result, {"qwire": "delivered", "monday": "completed"})
        self.assertIsNotNone(gateway.plan)

    def test_email_dry_run_does_not_write_workflow_artifacts(self):
        source = self.root / "scripps.csv"
        source.write_text("Trackid,Title\n1,One\n", encoding="utf-8")
        self.ctx.partner_metadata["scripps"] = source
        self.assertTrue(email.deliver_email(self.ctx, "scripps", True, LOG))
        self.assertFalse((self.ctx.specials_dir / "_WORKFLOW").exists())

    def test_monthly_email_uses_content_month_subject(self):
        ctx = ReleaseContext.for_monthly_delivery("2026-10-01")
        source = self.root / "qwire-monthly.csv"
        source.write_text("Trackid,Title\n1,One\n", encoding="utf-8")
        ctx.partner_metadata["qwire"] = source
        plan = email.prepare_email_plan(ctx, "qwire", "Signature")
        self.assertEqual(
            plan.subject,
            "Universal Production Music - September 2026 Metadata Delivery",
        )

    def test_november_delivery_uses_october_part_2_for_email_endpoints_only(self):
        ctx = ReleaseContext.for_monthly_delivery("2026-11-01")
        self.assertEqual(ctx.release_id, "UPM20261024")
        self.assertEqual(ctx.monthly_monday_batch, "UPM20261024")
        for endpoint in ("qwire", "scripps"):
            source = self.root / f"{endpoint}-monthly.csv"
            source.write_text("Trackid,Title\n1,One\n", encoding="utf-8")
            ctx.partner_metadata[endpoint] = source
            plan = email.prepare_email_plan(ctx, endpoint, "Signature")
            self.assertEqual(
                plan.subject,
                "Universal Production Music - October 2026 Part 2 Metadata Delivery",
            )
        self.assertEqual(
            ctx.partner_folder_name("Japan NTT DATA"),
            "Universal Production Music October 2026 - Japan NTT DATA",
        )

    def test_monthly_email_refuses_non_triggering_run(self):
        ctx = ReleaseContext.for_date_range("2026-09-12", "2026-09-25")
        with self.assertRaisesRegex(email.EmailDeliveryError, "not due"):
            email.prepare_email_plan(ctx, "qwire", "Signature")

    def test_monthly_email_live_send_is_held_until_first(self):
        ctx = ReleaseContext.for_monthly_delivery("2026-10-01")
        source = self.root / "qwire-held.csv"
        source.write_text("Trackid,Title\n1,One\n", encoding="utf-8")
        ctx.partner_metadata["qwire"] = source
        with patch("email_deliveries.monthly_metadata_delivery_ready", return_value=False):
            self.assertFalse(
                email.deliver_email(
                    ctx,
                    "qwire",
                    False,
                    LOG,
                    gateway=FakeOutlook(),
                    signature="Signature",
                    live_confirmation=ctx.release_id,
                )
            )

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

    def test_runner_syncs_monday_after_successful_live_endpoint(self):
        synced = []
        result = runner.run_deliveries(
            self.ctx,
            ("espn",),
            LOG,
            dry_run=False,
            live_confirmation=self.ctx.release_id,
            runners={"espn": lambda *_: True},
            progress_sync=lambda ctx, _logger: synced.append(ctx.release_id) or True,
        )
        self.assertEqual(result, {"espn": "delivered", "monday": "completed"})
        self.assertEqual(synced, [self.ctx.release_id])

    def test_acknowledgement_syncs_monday_after_state_change(self):
        self.ctx.specials_dir.mkdir(parents=True)
        from delivery_state import set_partner_status
        set_partner_status(self.ctx.specials_dir, "espn", "uploaded")
        workflow = self.ctx.specials_dir / "_WORKFLOW"
        (workflow / "espn_delivery_receipt.json").write_text("{}\n", encoding="utf-8")
        synced = []
        result = runner.acknowledge_delivered(
            self.ctx,
            ("espn",),
            self.ctx.release_id,
            logger=LOG,
            progress_sync=lambda ctx, _logger: synced.append(ctx.release_id) or True,
        )
        self.assertEqual(result, {"espn": "delivered", "monday": "completed"})
        self.assertEqual(synced, [self.ctx.release_id])

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

    def test_runner_processes_uploaded_soundmouse_without_requeueing(self):
        self.ctx.specials_dir.mkdir(parents=True)
        from delivery_state import set_partner_status
        set_partner_status(self.ctx.specials_dir, "soundmouse", "uploaded")
        called = []
        result = runner.run_deliveries(
            self.ctx,
            ("soundmouse",),
            LOG,
            dry_run=False,
            live_confirmation=self.ctx.release_id,
            runners={"soundmouse": lambda *_: called.append(True) or True},
            progress_sync=lambda *_: True,
        )
        self.assertEqual(result, {"soundmouse": "delivered", "monday": "completed"})
        self.assertEqual(called, [True])

    def test_default_soundmouse_runner_skips_uploader_after_uploaded_state(self):
        self.ctx.specials_dir.mkdir(parents=True)
        from delivery_state import set_partner_status
        set_partner_status(self.ctx.specials_dir, "soundmouse", "uploaded")
        with patch(
            "soundmouse_uploader_delivery.deliver_soundmouse_uploader"
        ) as uploader, patch(
            "soundmouse_web_delivery.deliver_soundmouse_web", return_value=True
        ) as website:
            runners = runner._implemented_runners(
                live_confirmation=self.ctx.release_id
            )
            self.assertTrue(runners["soundmouse"](self.ctx, False, LOG))
        uploader.assert_not_called()
        website.assert_called_once()

    def test_default_soundmouse_runner_uploads_then_processes_website(self):
        self.ctx.specials_dir.mkdir(parents=True)
        with patch(
            "soundmouse_uploader_delivery.deliver_soundmouse_uploader",
            return_value=True,
        ) as uploader, patch(
            "soundmouse_web_delivery.deliver_soundmouse_web", return_value=True
        ) as website:
            runners = runner._implemented_runners(
                live_confirmation=self.ctx.release_id
            )
            self.assertTrue(runners["soundmouse"](self.ctx, False, LOG))
        uploader.assert_called_once()
        website.assert_called_once()

    def test_uploader_receipt_cannot_acknowledge_soundmouse_delivered(self):
        self.ctx.specials_dir.mkdir(parents=True)
        from delivery_state import set_partner_status
        set_partner_status(self.ctx.specials_dir, "soundmouse", "uploaded")
        workflow = self.ctx.specials_dir / "_WORKFLOW"
        (workflow / "soundmouse_uploader_receipt.json").write_text(
            "{}\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(
            runner.PostPackagingError, "website metadata processor"
        ):
            runner.acknowledge_delivered(
                self.ctx,
                ("soundmouse",),
                self.ctx.release_id,
                logger=LOG,
            )

    def test_runner_marks_monthly_endpoint_not_due_without_calling_it(self):
        ctx = ReleaseContext.for_date_range("2026-09-12", "2026-09-25")
        called = []
        result = runner.run_deliveries(
            ctx,
            ("qwire", "scripps", "japan_jmdtss", "japan_ntt"),
            LOG,
            dry_run=True,
            runners={
                key: lambda *_: called.append(key) or True
                for key in ("qwire", "scripps", "japan_jmdtss", "japan_ntt")
            },
        )
        self.assertEqual(
            result,
            {
                "qwire": "not_due",
                "scripps": "not_due",
                "japan_jmdtss": "not_due",
                "japan_ntt": "not_due",
            },
        )
        self.assertEqual(called, [])

    def test_runner_holds_owned_monthly_package_until_first(self):
        ctx = ReleaseContext.for_monthly_delivery("2026-10-01")
        called = []
        with patch(
            "post_packaging_delivery.monthly_metadata_delivery_ready",
            return_value=False,
        ):
            result = runner.run_deliveries(
                ctx,
                ("qwire", "scripps", "japan_jmdtss", "japan_ntt"),
                LOG,
                dry_run=True,
                runners={
                    key: lambda *_: called.append(key) or True
                    for key in ("qwire", "scripps", "japan_jmdtss", "japan_ntt")
                },
            )
        self.assertEqual(
            result,
            {
                "qwire": "scheduled_for_month_start",
                "scripps": "scheduled_for_month_start",
                "japan_jmdtss": "scheduled_for_month_start",
                "japan_ntt": "scheduled_for_month_start",
            },
        )
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
