from __future__ import annotations

import logging
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import volume_mounts as mounts


LOG = logging.getLogger("volume-mount-tests")


class VolumeMountTests(unittest.TestCase):
    def test_pipeline_recovers_known_required_share(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "Pegasus"
            spec = mounts.VolumeSpec(
                "R8", "Pegasus", path, True,
                frozenset({mounts.PIPELINE_HOSTNAME}), "Pegasus",
            )
            ready_paths: set[Path] = set()
            calls = []

            def network(item):
                calls.append(item.name)
                ready_paths.add(item.path)
                return True, "started"

            with patch.object(mounts, "DISABLE_SENTINEL", Path(raw) / "disabled"):
                ok = mounts.ensure_workflow_volumes(
                    LOG, specs=(spec,), hostname=mounts.PIPELINE_HOSTNAME,
                    ready=lambda item: item in ready_paths,
                    network_mounter=network, timeout_seconds=0,
                )
            self.assertTrue(ok)
            self.assertEqual(calls, ["Pegasus"])

    def test_intentional_disable_prevents_mount_attempt(self):
        spec = mounts.VolumeSpec(
            "R8", "Pegasus", Path("/missing"), True,
            frozenset({mounts.PIPELINE_HOSTNAME}), "Pegasus",
        )
        calls = []
        ok = mounts.ensure_workflow_volumes(
            LOG, specs=(spec,), hostname=mounts.PIPELINE_HOSTNAME,
            skip_auto_mount=True, ready=lambda _item: False,
            network_mounter=lambda item: calls.append(item) or (True, "started"),
            timeout_seconds=0,
        )
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_optional_volume_failure_does_not_block(self):
        spec = mounts.VolumeSpec(
            "DOCS", "Documents", Path("/missing"), False,
            frozenset({mounts.PIPELINE_HOSTNAME}), "Documents",
        )
        with tempfile.TemporaryDirectory() as raw, patch.object(
            mounts, "DISABLE_SENTINEL", Path(raw) / "disabled"
        ):
            ok = mounts.ensure_workflow_volumes(
                LOG, specs=(spec,), hostname=mounts.PIPELINE_HOSTNAME,
                ready=lambda _item: False,
                network_mounter=lambda _item: (False, "offline"),
                timeout_seconds=0,
            )
        self.assertTrue(ok)

    def test_diskutil_parser_rejects_ambiguous_volume_name(self):
        payload = plistlib.dumps({
            "AllDisksAndPartitions": [
                {"Partitions": [
                    {"VolumeName": "Pegasus", "DeviceIdentifier": "disk4s1"},
                    {"VolumeName": "Pegasus", "DeviceIdentifier": "disk5s1"},
                    {"VolumeName": "UPM Builds", "DeviceIdentifier": "disk6s2"},
                ]}
            ]
        })
        self.assertEqual(
            mounts.diskutil_devices(payload),
            {"UPM Builds": "disk6s2"},
        )


if __name__ == "__main__":
    unittest.main()
