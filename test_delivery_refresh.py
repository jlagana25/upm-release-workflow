"""Tests for explicit delivery state and exact pending-destination syncing."""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from delivery_state import (
    partner_is_delivered,
    partner_needs_correction_package,
    partner_status,
    set_partner_status,
)
from cleanup import remove_non_maintracks
from final_packaging import (
    CopyOp,
    _copy_tree_files,
    _reconcile_uploaded_copy_op,
    _remove_destination_extras,
)
from prune import _scan_tree
from soundminer_agent import _build_command


class DeliveryRefreshTests(unittest.TestCase):
    def test_soundminer_agent_forwards_transition_overrides(self) -> None:
        command = _build_command({
            "workflow": "nbc",
            "pinned_args": ["--year", "2026", "--month", "8", "--part", "1"],
            "options": {
                "specials_dir_override": "/Volumes/Pegasus/release",
                "client_label_override": "August 2026 Part 1",
                "nbc_metadata_override": "/Volumes/Pegasus/release/nbc.csv",
            },
        })
        self.assertIn("--specials-dir-override", command)
        self.assertIn("August 2026 Part 1", command)
        self.assertIn("--nbc-metadata-override", command)

    def test_non_maintracks_can_be_removed_to_recoverable_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata = root / "metadata.csv"
            metadata.write_text(
                "File Name\nKEEP_100.mp3\nKEEP_200.mp3\n", encoding="utf-8"
            )
            target = root / "delivery"
            source = root / "source"
            album = target / "Label" / "Album"
            source_album = source / "Label" / "Album"
            album.mkdir(parents=True)
            source_album.mkdir(parents=True)
            for name in ("KEEP_100.mp3", "KEEP_200.mp3"):
                (album / name).write_bytes(b"keep")
                (source_album / name).write_bytes(b"keep")
            extra = album / "EXTRA_300.mp3"
            extra.write_bytes(b"extra")
            archive = root / "archive"
            ctx = type("Context", (), {"specials_dir": root})()
            ok = remove_non_maintracks(
                ctx,
                dry_run=False,
                actually_delete=True,
                logger=logging.getLogger("delivery-refresh"),
                metadata_csv=metadata,
                target_folder=target,
                source_folder=source,
                archive_extras_to=archive,
            )
            self.assertTrue(ok)
            self.assertFalse(extra.exists())
            self.assertEqual(
                (archive / "Label" / "Album" / "EXTRA_300.mp3").read_bytes(),
                b"extra",
            )

    def test_prune_accepts_stripped_label_and_nested_pitch_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            track = root / "BTV " / "pitch" / "BTV347 - Album" / "Track.wav"
            track.parent.mkdir(parents=True)
            track.write_bytes(b"wav")
            extras, _junk, by_album = _scan_tree(
                root,
                {("BTV / pitch", "BTV347", "track.wav")},
                set(),
                False,
            )
            self.assertEqual(extras, [])
            self.assertIn(("BTV / pitch", "BTV347"), by_album)

    def test_delivery_state_defaults_pending_and_can_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(partner_is_delivered(root, "netmix"))
            path = set_partner_status(root, "Netmix", True)
            self.assertTrue(path.is_file())
            self.assertTrue(partner_is_delivered(root, "netmix"))
            set_partner_status(root, "netmix", False)
            self.assertFalse(partner_is_delivered(root, "NETMIX"))
            set_partner_status(root, "netmix", "uploaded")
            self.assertEqual(partner_status(root, "netmix"), "uploaded")
            self.assertTrue(partner_needs_correction_package(root, "netmix"))
            set_partner_status(root, "discovery", "uploaded")
            self.assertFalse(
                partner_needs_correction_package(root, "discovery")
            )
            with self.assertRaises(ValueError):
                set_partner_status(root, "typo-partner", True)

    def test_uploaded_netmix_builds_delta_and_removes_obsolete_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical" / "Label" / "Album"
            destination = root / "Netmix" / "Music" / "Label" / "Album"
            source.mkdir(parents=True)
            destination.mkdir(parents=True)
            (source / "new.wav").write_bytes(b"new")
            (source / "cover.jpg").write_bytes(b"cover")
            stale = destination / "old.wav"
            stale.write_bytes(b"old")
            op = CopyOp(
                "Netmix",
                root / "canonical",
                root / "Netmix" / "Music",
                partner_key="netmix",
            )
            self.assertTrue(_reconcile_uploaded_copy_op(
                op, dry_run=False, logger=logging.getLogger("delivery-refresh")
            ))
            missing = root / "Netmix" / "Missing"
            self.assertTrue((missing / "Label" / "Album" / "new.wav").is_file())
            self.assertTrue((missing / "Label" / "Album" / "cover.jpg").is_file())
            self.assertTrue((missing / "Netmix Missing Audit.csv").is_file())
            self.assertFalse(stale.exists())

    def test_pending_destination_sync_uses_union_and_removes_extras(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            us = root / "us"
            exus = root / "exus"
            destination = root / "delivery"
            (us / "US" / "Album").mkdir(parents=True)
            (exus / "EX" / "Album").mkdir(parents=True)
            (destination / "US" / "Album").mkdir(parents=True)
            (destination / "EX" / "Album").mkdir(parents=True)
            (destination / "Old" / "Album").mkdir(parents=True)
            (us / "US" / "Album" / "one.mp3").write_bytes(b"1")
            (exus / "EX" / "Album" / "two.mp3").write_bytes(b"2")
            (destination / "US" / "Album" / "one.mp3").write_bytes(b"1")
            (destination / "EX" / "Album" / "two.mp3").write_bytes(b"2")
            stale = destination / "Old" / "Album" / "stale.mp3"
            stale.write_bytes(b"old")
            ops = [
                CopyOp("US", us, destination, partner_key="partner"),
                CopyOp("ExUS", exus, destination, partner_key="partner"),
            ]
            removed, errors = _remove_destination_extras(
                ops, dry_run=False, logger=logging.getLogger("delivery-refresh")
            )
            self.assertEqual((removed, errors), (1, 0))
            self.assertFalse(stale.exists())
            self.assertTrue((destination / "US" / "Album" / "one.mp3").exists())
            self.assertTrue((destination / "EX" / "Album" / "two.mp3").exists())

    def test_tunesat_filename_filter_avoids_copying_full_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source" / "Label" / "Album"
            destination = root / "destination"
            source.mkdir(parents=True)
            (source / "keep_100.mp3").write_bytes(b"keep")
            (source / "omit_200.mp3").write_bytes(b"omit")
            copied, skipped, errors = _copy_tree_files(
                root / "source",
                destination,
                dry_run=False,
                overwrite=False,
                logger=logging.getLogger("delivery-refresh"),
                filename_filter=frozenset({"keep_100"}),
            )
            self.assertEqual((copied, skipped, errors), (1, 0, 0))
            self.assertTrue((destination / "Label" / "Album" / "keep_100.mp3").exists())
            self.assertFalse((destination / "Label" / "Album" / "omit_200.mp3").exists())


if __name__ == "__main__":
    unittest.main()
