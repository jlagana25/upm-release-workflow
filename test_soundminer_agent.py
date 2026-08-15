import json
import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from config import ReleaseContext, SOUNDMINER_HOSTNAME
import soundminer_agent


class SoundminerAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.logger = logging.getLogger(self.id())
        self.ctx = ReleaseContext(2026, 7, 1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_request_is_atomic_and_pins_context(self):
        request_id = soundminer_agent.submit_request(
            self.ctx,
            "sourceaudio",
            self.logger,
            options={"resume": True},
            root=self.root,
        )
        pending = self.root / "pending" / f"{request_id}.json"
        payload = json.loads(pending.read_text(encoding="utf-8"))
        self.assertEqual(payload["release_id"], "UPM-2026-07-P1")
        self.assertEqual(payload["pinned_args"], [
            "--year", "2026", "--month", "7", "--part", "1"
        ])
        self.assertFalse(list((self.root / "pending").glob("*.tmp")))

    def test_health_requires_fresh_hdf1_heartbeat(self):
        paths = soundminer_agent._ensure_dirs(self.root)
        soundminer_agent._atomic_json(paths["agent"], {
            "host": SOUNDMINER_HOSTNAME,
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        })
        healthy, detail = soundminer_agent.agent_health(self.root)
        self.assertTrue(healthy, detail)

    def test_running_job_heartbeat_keeps_busy_agent_healthy(self):
        paths = soundminer_agent._ensure_dirs(self.root)
        soundminer_agent._atomic_json(paths["agent"], {
            "host": SOUNDMINER_HOSTNAME,
            "heartbeat_at": "2000-01-01T00:00:00+00:00",
        })
        soundminer_agent._atomic_json(paths["status"] / "job-1.json", {
            "request_id": "job-1",
            "state": "running",
            "host": SOUNDMINER_HOSTNAME,
            "heartbeat_at": soundminer_agent._utc_now(),
        })
        healthy, detail = soundminer_agent.agent_health(self.root)
        self.assertTrue(healthy, detail)
        self.assertIn("busy with job-1", detail)

    def test_completed_status_returns_without_gui(self):
        request_id = "test-complete"
        paths = soundminer_agent._ensure_dirs(self.root)
        soundminer_agent._atomic_json(paths["status"] / f"{request_id}.json", {
            "request_id": request_id,
            "state": "completed",
            "phase": "done",
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        })
        self.assertTrue(soundminer_agent.wait_for_request(
            request_id, self.logger, root=self.root, timeout=1
        ))

    def test_probe_command_is_non_destructive_preflight(self):
        command = soundminer_agent._build_command({
            "workflow": "probe",
            "pinned_args": ["--year", "2026", "--month", "7", "--part", "1"],
            "options": {},
        })
        self.assertIn("--preflight-only", command)
        self.assertIn("--nbc", command)
        self.assertNotIn("--sourceaudio", command)

    def test_terminal_wrapper_runs_job_under_caffeinate(self):
        request = {"request_id": "awake-job"}
        log_path = self.root / "job.log"
        status_path = self.root / "status.json"

        def fake_run(argv, **kwargs):
            wrapper = Path(argv[-1])
            body = wrapper.read_text(encoding="utf-8")
            self.assertIn("/usr/bin/caffeinate -dimsu", body)
            exit_path = wrapper.with_suffix(".exit")
            exit_path.write_text("0\n", encoding="utf-8")
            return type("Result", (), {"returncode": 0, "stderr": ""})()

        with (
            patch.object(soundminer_agent, "AGENT_LOG_DIR", self.root),
            patch.object(soundminer_agent.subprocess, "run", side_effect=fake_run),
        ):
            code, _ = soundminer_agent._run_in_login_terminal(
                ["python3", "soundminer.py"],
                request,
                status_path,
                log_path,
                self.logger,
            )
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
