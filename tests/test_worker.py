from __future__ import annotations

import copy
import unittest

from relay_agent.models import Destination, DesiredState
from relay_agent.worker import DestinationWorker, WorkerKey, WorkerSupervisor, _redact

from .helpers import RELAY_ID, envelope, stream


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
            WorkerKey("stream_00000001", self.destination.destination_id),
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
        self.assertEqual(self.worker.status()["platform"], "bongacams")

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

    def test_same_platform_destinations_are_independent_workers(self):
        state_payload = envelope(
            streams=[stream(1, ("chaturbate", "chaturbate"))]
        )["payload"]
        state = DesiredState.from_payload(state_payload, RELAY_ID)
        destinations = state.streams[0].destinations
        supervisor = WorkerSupervisor("/usr/bin/ffmpeg", "rtsp://127.0.0.1:8554", 5)
        # Avoid starting subprocesses; reconcile's worker map is enough to
        # verify identity and independent replacement behavior.
        started = []
        stopped = []
        original = DestinationWorker.start
        original_stop = DestinationWorker.stop
        DestinationWorker.start = lambda worker: started.append(worker)
        DestinationWorker.stop = lambda worker, timeout=7.0: stopped.append(worker)
        try:
            supervisor.reconcile(
                state,
                {"live/stream_00000001": {"online": True}},
            )
            self.assertEqual(len(started), 2)
            self.assertEqual(
                {worker.key.destination_id for worker in started},
                {destination.destination_id for destination in destinations},
            )
            self.assertEqual({worker.destination.platform for worker in started}, {"chaturbate"})

            changed_payload = copy.deepcopy(state_payload)
            changed_payload["streams"][0]["destinations"][0]["stream_key"] = "changed_chaturbate_key_1"
            changed_state = DesiredState.from_payload(changed_payload, RELAY_ID)
            supervisor.reconcile(
                changed_state,
                {"live/stream_00000001": {"online": True}},
            )
            self.assertEqual(len(stopped), 1)
            self.assertEqual(stopped[0].destination.destination_id, destinations[0].destination_id)
            self.assertEqual(len(started), 3)
            self.assertEqual(
                started[-1].destination.destination_id,
                destinations[0].destination_id,
            )
        finally:
            DestinationWorker.start = original
            DestinationWorker.stop = original_stop


if __name__ == "__main__":
    unittest.main()
