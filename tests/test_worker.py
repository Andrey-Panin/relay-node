from __future__ import annotations

import unittest

from relay_agent.models import Destination
from relay_agent.worker import DestinationWorker, WorkerKey, _redact


class WorkerCommandTests(unittest.TestCase):
    def setUp(self):
        self.destination = Destination(
            destination_id="00000000-0000-0000-0000-000000000001",
            platform="bongacams",
            enabled=True,
            ingest_url="rtmp://auto.origin.example.test:1934/live",
            stream_key="fake/key?token=do_not_log",
        )
        self.worker = DestinationWorker(
            WorkerKey("stream_00000001", "bongacams"),
            self.destination,
            "rtsp://127.0.0.1:8554/live/stream_00000001",
            "/usr/bin/ffmpeg",
        )

    def test_copy_command_requires_video_and_tracks_progress(self):
        target = "rtmp://auto.origin.example.test:1934/live/fake/key?token=do_not_log"
        command = self.worker.command(target)
        self.assertNotIn("-rw_timeout", command)
        timeout_option = command.index("-timeout")
        self.assertEqual(command[timeout_option + 1], "15000000")
        self.assertLess(timeout_option, command.index("-i"))
        video_map = command.index("0:v:0")
        self.assertGreater(video_map, 0)
        self.assertIn("0:a:0?", command)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertIn("pipe:1", command)
        self.assertEqual(command[-1], target)

    def test_stderr_redaction_removes_raw_and_encoded_key_and_url(self):
        target = "rtmp://auto.origin.example.test:1934/live/fake/key?token=do_not_log"
        line = (
            target
            + " fake/key?token=do_not_log "
            + "fake%2Fkey%3Ftoken%3Ddo_not_log"
        )
        redacted = _redact(line, self.destination, target)
        self.assertNotIn("do_not_log", redacted)
        self.assertNotIn("rtmp://", redacted)


if __name__ == "__main__":
    unittest.main()

