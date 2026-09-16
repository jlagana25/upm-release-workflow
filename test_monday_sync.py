import logging
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from config import ReleaseContext
from monday_sync import (
    BoardItem,
    BoardSubitem,
    GROUP_CONTENT,
    GROUP_HD,
    GROUP_SOUNDMOUSE,
    MondayClient,
    MondayError,
    SourceBoardSchema,
    SourceItem,
    SourceRow,
    build_status_plan,
    load_source_rows,
    monday_batch_key,
    monday_batch_month,
    run_monday_source_preflight,
    run_monday_sync,
    soundmouse_batch,
)


def _content(batch="202609", status="", child_status="Not Started"):
    names = [
        "UPM Japan - TSS & JMD Metadata (Album Date Format YYYY/MM/DD)",
        "UPM Japan - NTT DATA", "SourceAudio", "SourceAudio (Ex-US)",
        "Netmix", "TuneSat (+Bruton & Kosinus)", "ESPN",
        "SynchTank", "Discovery", "Scripps (Metadata only)",
        "QWire (Metadata only)", "SoundExchange (Metadata only)",
    ]
    children = [
        BoardSubitem(1000 + index, name, child_status)
        for index, name in enumerate(names)
    ]
    children.append(BoardSubitem(1099, "Adrev/Fuga (+APM Shared Assets)", "API Client - Not Needed"))
    return BoardItem(100, f"{batch} - Delivery", GROUP_CONTENT, batch, "Aggregator (AG)", status, tuple(children))


def _hd(batch="202609", status="", child_status="Not Started"):
    return BoardItem(
        200, f"{batch} - Delivery", GROUP_HD, batch, "Hard Drive (HD)", status,
        (BoardSubitem(2001, "MP3", child_status), BoardSubitem(2002, "WAV", child_status)),
    )


def _soundmouse(batch="202609 -1", status="", child_status="Not Started"):
    names = (
        "Download Media from UniSync", "Export Metadata", "Export Album Covers",
        "Upload to SoundMouse", "Process Metadata in SoundMouse",
    )
    return BoardItem(
        300, f"{batch} Update", GROUP_SOUNDMOUSE, batch, "SoundMouse (SM)", status,
        tuple(BoardSubitem(3000 + index, name, child_status) for index, name in enumerate(names)),
    )


READY_RESULTS = {
    "9 Verification": "completed",
    "10 Final packaging": "completed",
    "10 SoundExchange forms": "completed",
    "11 SourceAudio": "completed",
    "15 Final metadata check": "completed",
    "16 SoundMouse": "completed",
}


class FakeGateway:
    def __init__(self, items_by_batch):
        self.items_by_batch = items_by_batch
        self.validated = False
        self.changes = []
        self.source_items = []
        self.source_schema = SourceBoardSchema(999, {
            title: (f"column_{index}", "date" if title == "Release Date" else "text")
            for index, title in enumerate((
                "Batch", "Catalog", "Release Date", "LabelId", "Album Code",
                "Album Title", "Digital Fulfillment", "Batch Master",
            ))
        })

    def validate_schema(self):
        self.validated = True

    def fetch_batch(self, batch):
        overlay = {change.item_id: change.new_status for change in self.changes}
        result = []
        for item in self.items_by_batch.get(batch, []):
            result.append(BoardItem(
                item.id, item.name, item.group, item.batch, item.delivery_type,
                overlay.get(item.id, item.status),
                tuple(
                    BoardSubitem(child.id, child.name, overlay.get(child.id, child.status))
                    for child in item.subitems
                ),
            ))
        return result

    def set_status(self, change):
        self.changes.append(change)

    def validate_source_schema(self):
        return self.source_schema

    def fetch_source_batch(self, _schema, batch):
        return [item for item in self.source_items if item.row.batch == batch]

    def create_source_group(self, _schema, _title):
        return "source-group"

    def create_source_item(self, _schema, group_id, row):
        item_id = 9000 + len(self.source_items)
        self.source_items.append(SourceItem(item_id, group_id, row))
        return item_id

    def set_source_item_values(self, _schema, item_id, row, *, include_master):
        for index, item in enumerate(self.source_items):
            if item.item_id == item_id:
                updated = row if include_master else SourceRow(**{
                    **row.__dict__, "batch_master": item.row.batch_master,
                })
                self.source_items[index] = SourceItem(item_id, item.group_id, updated)
                return
        raise AssertionError("source item not found")


class NonPersistingGateway(FakeGateway):
    def fetch_batch(self, batch):
        return self.items_by_batch.get(batch, [])


class MondaySyncTests(unittest.TestCase):
    def test_client_retries_401_with_bearer_authorization(self):
        first = Mock(status_code=401)
        second = Mock(status_code=200)
        second.raise_for_status.return_value = None
        second.json.return_value = {"data": {"me": {"id": "1"}}}
        session = Mock()
        session.post.side_effect = [first, second]
        MondayClient("  sample-value  ", session=session).validate_auth()
        headers = [call.kwargs["headers"] for call in session.post.call_args_list]
        self.assertEqual(headers[0]["Authorization"], "sample-value")
        self.assertEqual(headers[1]["Authorization"], "Bearer sample-value")
        self.assertNotIn("API-Version", headers[0])
        self.assertNotIn("API-Version", headers[1])

    def test_source_board_schema_uses_stable_ids_despite_duplicate_titles(self):
        columns = [
            {"id": "text", "title": "Batch", "type": "text"},
            {"id": "text_mm6sxbha", "title": "Catalog", "type": "text"},
            {"id": "dropdown_duplicate", "title": "Catalog", "type": "dropdown"},
            {"id": "date_mm6sxypv", "title": "Release Date", "type": "date"},
            {"id": "text4", "title": "LabelID", "type": "text"},
            {"id": "text7", "title": "Album Code", "type": "text"},
            {"id": "text6", "title": "Album Title", "type": "text"},
            {"id": "status0", "title": "Digital Fulfillment", "type": "status"},
            {"id": "status1", "title": "Batch Master", "type": "status"},
        ]
        client = MondayClient("sample-value")
        with patch.object(client, "_request", return_value={
            "boards": [{
                "id": "5981022568",
                "name": "UPPM Audio Batch Releases",
                "columns": columns,
            }],
        }) as request:
            schema = client.validate_source_schema()
        self.assertEqual(5981022568, schema.board_id)
        self.assertEqual("text_mm6sxbha", schema.columns["Catalog"][0])
        self.assertEqual(1, request.call_count)

    def test_transition_and_range_batch_derivation(self):
        transition = ReleaseContext(2026, 7, 1, previous_month=True)
        self.assertEqual(monday_batch_month(transition), "202608")
        self.assertEqual(soundmouse_batch(transition, "202608"), "202608 -1")

        august_part_two = ReleaseContext(
            2026, 8, 2, full_month_content=True,
        )
        self.assertEqual(monday_batch_key(august_part_two), "UPM20260801")
        self.assertEqual(
            soundmouse_batch(august_part_two, "UPM20260801"), "UPM20260801"
        )

        rolling = ReleaseContext(
            2026, 9, 1,
            range_start=date(2026, 9, 1), range_end=date(2026, 9, 14),
        )
        self.assertEqual(monday_batch_key(rolling), "UPM20260901")
        self.assertEqual(monday_batch_month(rolling), "UPM20260901")
        self.assertEqual(
            soundmouse_batch(rolling, "UPM20260901"), "UPM20260901"
        )

    @patch("monday_sync.partner_status", return_value="pending")
    def test_ready_plan_sets_packages_clear_to_send(self, _status):
        ctx = ReleaseContext(
            2026, 9, 1,
            range_start=date(2026, 9, 1), range_end=date(2026, 9, 14),
        )
        plan = build_status_plan(
            ctx,
            READY_RESULTS,
            {
                "UPM20260901": [
                    _content(batch="UPM20260901"),
                    _hd(batch="UPM20260901"),
                    _soundmouse(batch="UPM20260901"),
                ],
            },
        )
        by_name = {change.item_name: change.new_status for change in plan}
        self.assertEqual(by_name["SourceAudio"], "Clear to Send")
        self.assertEqual(by_name["SourceAudio (Ex-US)"], "Clear to Send")
        self.assertEqual(by_name["MP3"], "Clear to Send")
        self.assertEqual(by_name["WAV"], "Clear to Send")
        self.assertEqual(by_name["Upload to SoundMouse"], "Clear to Send")
        self.assertEqual(by_name["Download Media from UniSync"], "Complete")
        main_changes = [change for change in plan if not change.is_subitem]
        self.assertEqual(
            {(change.item_id, change.new_status) for change in main_changes},
            {(100, "Ready to Close"), (200, "Ready to Close"), (300, "Prepping Content")},
        )

    @patch("monday_sync.partner_status", return_value="pending")
    def test_soundmouse_phase_progress_updates_only_finished_subitem(self, _status):
        ctx = ReleaseContext(
            2026, 9, 1,
            range_start=date(2026, 9, 1), range_end=date(2026, 9, 14),
        )
        plan = build_status_plan(
            ctx,
            {"16 SoundMouse media": "completed"},
            {"UPM20260901": [
                _content(batch="UPM20260901"),
                _hd(batch="UPM20260901"),
                _soundmouse(batch="UPM20260901"),
            ]},
        )
        desired = {change.item_name: change.new_status for change in plan}
        self.assertEqual(desired["Download Media from UniSync"], "Complete")
        self.assertNotIn("Export Metadata", desired)
        self.assertNotIn("Export Album Covers", desired)
        self.assertNotIn("Upload to SoundMouse", desired)

    @patch("monday_sync.partner_status", return_value="pending")
    def test_final_subitem_statuses_are_never_downgraded(self, _status):
        ctx = ReleaseContext(
            2026, 9, 1,
            range_start=date(2026, 9, 1), range_end=date(2026, 9, 14),
        )
        plan = build_status_plan(
            ctx,
            READY_RESULTS,
            {
                "UPM20260901": [
                    _content(batch="UPM20260901", child_status="Complete"),
                    _hd(batch="UPM20260901", child_status="Complete"),
                    _soundmouse(batch="UPM20260901", child_status="Complete"),
                ],
            },
        )
        self.assertFalse(any(change.is_subitem for change in plan))

    @patch("monday_sync.partner_status")
    def test_uploaded_soundmouse_advances_processing_only(self, status):
        status.side_effect = lambda _root, partner: "uploaded" if partner == "soundmouse" else "pending"
        ctx = ReleaseContext(
            2026, 9, 1,
            range_start=date(2026, 9, 1), range_end=date(2026, 9, 14),
        )
        sm = _soundmouse(batch="UPM20260901")
        plan = build_status_plan(
            ctx,
            READY_RESULTS,
            {"UPM20260901": [
                _content(batch="UPM20260901"),
                _hd(batch="UPM20260901"),
                sm,
            ]},
        )
        desired = {change.item_name: change.new_status for change in plan}
        self.assertEqual(desired["Upload to SoundMouse"], "Complete")
        self.assertEqual(desired["Process Metadata in SoundMouse"], "Clear to Send")

    @patch("monday_sync.partner_status", return_value="delivered")
    def test_all_individual_delivery_states_complete_packages(self, _status):
        ctx = ReleaseContext(
            2026, 9, 1,
            range_start=date(2026, 9, 1), range_end=date(2026, 9, 14),
        )
        plan = build_status_plan(
            ctx, {},
            {"UPM20260901": [
                _content(batch="UPM20260901"),
                _hd(batch="UPM20260901"),
                _soundmouse(batch="UPM20260901"),
            ]},
        )
        desired = {change.item_name: change.new_status for change in plan}
        for package in (
            "UPM Japan - TSS & JMD Metadata (Album Date Format YYYY/MM/DD)",
            "Scripps (Metadata only)", "QWire (Metadata only)",
            "SoundExchange (Metadata only)", "MP3", "WAV",
            "Process Metadata in SoundMouse",
        ):
            self.assertEqual(desired[package], "Complete")
        main = {
            change.item_id: change.new_status
            for change in plan if not change.is_subitem
        }
        self.assertEqual(main, {100: "Delivered", 200: "Delivered", 300: "Delivered"})

    @patch("monday_sync.partner_status", return_value="pending")
    def test_missing_exact_batch_item_fails_closed(self, _status):
        ctx = ReleaseContext(
            2026, 9, 1,
            range_start=date(2026, 9, 1), range_end=date(2026, 9, 14),
        )
        with self.assertRaises(MondayError):
            build_status_plan(
                ctx,
                READY_RESULTS,
                {"UPM20260901": [
                    _content(batch="UPM20260901"),
                    _soundmouse(batch="UPM20260901"),
                ]},
            )

    @patch("monday_sync.partner_status", return_value="pending")
    def test_part_two_requires_only_source_automation_soundmouse_item(self, _status):
        ctx = ReleaseContext(2026, 6, 2)
        plan = build_status_plan(
            ctx,
            READY_RESULTS,
            {"202606 -2": [_soundmouse(batch="202606 -2")]},
        )
        desired = {change.item_name: change.new_status for change in plan}
        self.assertEqual(desired["Upload to SoundMouse"], "Clear to Send")
        self.assertNotIn("MP3", desired)
        self.assertNotIn("SourceAudio", desired)

    @patch("monday_sync.partner_status", return_value="pending")
    def test_august_part_two_compact_batch_requires_all_packages(self, _status):
        ctx = ReleaseContext(2026, 8, 2, full_month_content=True)
        plan = build_status_plan(
            ctx,
            READY_RESULTS,
            {"UPM20260801": [
                _content(batch="UPM20260801"),
                _hd(batch="UPM20260801"),
                _soundmouse(batch="UPM20260801"),
            ]},
        )
        desired = {change.item_name: change.new_status for change in plan}
        self.assertEqual(desired["SourceAudio"], "Clear to Send")
        self.assertEqual(desired["MP3"], "Clear to Send")
        self.assertEqual(desired["Upload to SoundMouse"], "Clear to Send")

    @patch("monday_sync.partner_status", return_value="pending")
    def test_dry_run_validates_and_never_mutates(self, _status):
        ctx = ReleaseContext(
            2026, 9, 1,
            range_start=date(2026, 9, 1), range_end=date(2026, 9, 14),
        )
        gateway = FakeGateway({
            "UPM20260901": [
                _content(batch="UPM20260901"),
                _hd(batch="UPM20260901"),
                _soundmouse(batch="UPM20260901"),
            ],
        })
        self.assertTrue(run_monday_sync(
            ctx, READY_RESULTS, dry_run=True,
            logger=logging.getLogger("test_monday_sync"), gateway=gateway,
        ))
        self.assertTrue(gateway.validated)
        self.assertEqual(gateway.changes, [])

    def test_source_rows_normalize_batch_and_choose_one_master(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "batch.csv"
            path.write_text(
                "WorkGroupingId,Batch,Catalog,Release Date,LabelId,Album Code,"
                "Album Title,Digital Fulfillment,Batch Master\n"
                "42,,UPM-US,2026-09-12 10:00:00,7,ABC1,Album,Create NEW,\n",
                encoding="utf-8",
            )
            rows = load_source_rows(path, "UPM20260912")
        self.assertEqual(1, len(rows))
        self.assertEqual("UPM20260912", rows[0].batch)
        self.assertEqual("2026-09-12", rows[0].release_date)
        self.assertEqual("Batch Master", rows[0].batch_master)

    def test_source_preflight_loads_master_last_and_verifies_destination(self):
        ctx = ReleaseContext(
            2026, 9, 1,
            range_start=date(2026, 9, 12), range_end=date(2026, 9, 25),
        )
        gateway = FakeGateway({
            "UPM20260912": [
                _content(batch="UPM20260912"),
                _hd(batch="UPM20260912"),
                _soundmouse(batch="UPM20260912"),
            ],
        })
        with tempfile.TemporaryDirectory() as tmp:
            ctx.monday_audio_batch_csv = Path(tmp) / "batch.csv"
            ctx.monday_audio_batch_csv.write_text(
                "WorkGroupingId,Batch,Catalog,Release Date,LabelId,Album Code,"
                "Album Title,Digital Fulfillment,Batch Master\n"
                "42,UPM20260912,UPM-US,2026-09-12,7,ABC1,Album,Create NEW,\n",
                encoding="utf-8",
            )
            with patch("domo_exports.run_domo_exports", return_value={"monday_audio_batch": "ok"}):
                self.assertTrue(run_monday_source_preflight(
                    ctx, dry_run=False, logger=logging.getLogger("source-preflight"),
                    gateway=gateway, timeout_seconds=0, poll_seconds=0,
                ))
        self.assertEqual(1, len(gateway.source_items))
        self.assertEqual("Batch Master", gateway.source_items[0].row.batch_master)

    @patch("monday_sync.partner_status", return_value="pending")
    def test_only_step_recovery_uses_latest_real_report(self, _status):
        ctx = ReleaseContext(
            2026, 9, 1,
            range_start=date(2026, 9, 1), range_end=date(2026, 9, 14),
        )
        gateway = FakeGateway({
            "UPM20260901": [
                _content(batch="UPM20260901"),
                _hd(batch="UPM20260901"),
                _soundmouse(batch="UPM20260901"),
            ],
        })
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "reports" / ctx.release_id
            report_dir.mkdir(parents=True)
            (report_dir / "run-20260915-010000.json").write_text(json.dumps({
                "run": {"dry_run": False},
                "steps": {
                    key: {"status": value}
                    for key, value in READY_RESULTS.items()
                },
            }), encoding="utf-8")
            with patch("monday_sync.LOGS_DIR", Path(tmp)):
                self.assertTrue(run_monday_sync(
                    ctx, {key: "skipped" for key in READY_RESULTS}, dry_run=False,
                    logger=logging.getLogger("test_monday_recovery"), gateway=gateway,
                ))
        desired = {change.item_name: change.new_status for change in gateway.changes}
        self.assertEqual(desired["SourceAudio"], "Clear to Send")
        self.assertEqual(desired["MP3"], "Clear to Send")
        self.assertEqual(desired["Upload to SoundMouse"], "Clear to Send")

    @patch("monday_sync.partner_status", return_value="pending")
    def test_live_checkpoint_does_not_reuse_old_report(self, _status):
        ctx = ReleaseContext(
            2026, 9, 1,
            range_start=date(2026, 9, 1), range_end=date(2026, 9, 14),
        )
        gateway = FakeGateway({
            "UPM20260901": [
                _content(batch="UPM20260901"),
                _hd(batch="UPM20260901"),
                _soundmouse(batch="UPM20260901"),
            ],
        })
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "reports" / ctx.release_id
            report_dir.mkdir(parents=True)
            (report_dir / "run-old.json").write_text(json.dumps({
                "run": {"dry_run": False},
                "steps": {
                    key: {"status": value}
                    for key, value in READY_RESULTS.items()
                },
            }), encoding="utf-8")
            with patch("monday_sync.LOGS_DIR", Path(tmp)):
                self.assertTrue(run_monday_sync(
                    ctx,
                    {
                        "9 Verification": "completed",
                        "10 Final packaging": "completed",
                    },
                    dry_run=False,
                    logger=logging.getLogger("test_monday_live"),
                    gateway=gateway,
                    include_history=False,
                ))
        desired = {change.item_name: change.new_status for change in gateway.changes}
        self.assertEqual(desired["MP3"], "Clear to Send")
        self.assertEqual(desired["WAV"], "Clear to Send")
        self.assertNotIn("SourceAudio", desired)
        self.assertNotIn("Upload to SoundMouse", desired)

    @patch("monday_sync.partner_status", return_value="pending")
    def test_recent_failure_wins_over_older_success(self, _status):
        ctx = ReleaseContext(
            2026, 9, 1,
            range_start=date(2026, 9, 1), range_end=date(2026, 9, 14),
        )
        gateway = FakeGateway({
            "UPM20260901": [
                _content(batch="UPM20260901"),
                _hd(batch="UPM20260901"),
                _soundmouse(batch="UPM20260901"),
            ],
        })
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "reports" / ctx.release_id
            report_dir.mkdir(parents=True)
            for stamp, verification in (
                ("20260914-010000", "completed"),
                ("20260915-010000", "failed"),
            ):
                values = dict(READY_RESULTS)
                values["9 Verification"] = verification
                (report_dir / f"run-{stamp}.json").write_text(json.dumps({
                    "run": {"dry_run": False},
                    "steps": {key: {"status": value} for key, value in values.items()},
                }), encoding="utf-8")
            with patch("monday_sync.LOGS_DIR", Path(tmp)):
                self.assertTrue(run_monday_sync(
                    ctx, {}, dry_run=False,
                    logger=logging.getLogger("test_monday_failure"), gateway=gateway,
                ))
        desired = {change.item_name: change.new_status for change in gateway.changes}
        self.assertEqual(desired["MP3"], "Stuck")
        self.assertEqual(desired["WAV"], "Stuck")

    @patch("monday_sync.partner_status", return_value="pending")
    def test_post_write_verification_fails_closed(self, _status):
        ctx = ReleaseContext(
            2026, 9, 1,
            range_start=date(2026, 9, 1), range_end=date(2026, 9, 14),
        )
        gateway = NonPersistingGateway({
            "UPM20260901": [
                _content(batch="UPM20260901"),
                _hd(batch="UPM20260901"),
                _soundmouse(batch="UPM20260901"),
            ],
        })
        self.assertFalse(run_monday_sync(
            ctx, READY_RESULTS, dry_run=False,
            logger=logging.getLogger("test_monday_verify"), gateway=gateway,
        ))


if __name__ == "__main__":
    unittest.main()
