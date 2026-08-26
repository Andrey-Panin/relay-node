from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from relay_agent.http_server import AdmissionController
from relay_agent.state_store import StateStore

from .helpers import RELAY_ID, SIGNING_KEY, envelope, stream


class AdmissionTests(unittest.TestCase):
    def make_controller(self, count: int = 16):
        temporary = tempfile.TemporaryDirectory()
        store = StateStore(RELAY_ID, SIGNING_KEY, Path(temporary.name) / "state.json")
        value = envelope(streams=[stream(index) for index in range(1, count + 1)])
        store.accept_remote(value)
        return temporary, store, AdmissionController(store, 15)

    def test_sixteenth_ready_stream_is_denied(self):
        temporary, _store, controller = self.make_controller()
        self.addCleanup(temporary.cleanup)
        controller.update_ready_paths(
            {f"live/stream_{index:08d}": {"online": True} for index in range(1, 16)}
        )
        self.assertFalse(
            controller.authorize(
                {
                    "action": "publish",
                    "protocol": "srt",
                    "path": "live/stream_00000016",
                    "user": "source",
                    "password": "credential_00000016_abcdefghijklmnop",
                    "ip": "192.0.2.1",
                }
            )
        )

    def test_valid_publish_and_loopback_worker_read(self):
        temporary, _store, controller = self.make_controller(1)
        self.addCleanup(temporary.cleanup)
        publish = {
            "action": "publish",
            "protocol": "rtmp",
            "path": "live/stream_00000001",
            "user": "source",
            "password": "credential_00000001_abcdefghijklmnop",
            "ip": "192.0.2.1",
        }
        self.assertTrue(controller.authorize(publish))
        self.assertTrue(
            controller.authorize(
                {
                    "action": "read",
                    "protocol": "rtsp",
                    "path": "live/stream_00000001",
                    "ip": "127.0.0.1",
                }
            )
        )
        publish["password"] = "wrong_credential_which_is_long_enough"
        self.assertFalse(controller.authorize(publish))

    def test_external_reader_is_denied(self):
        temporary, _store, controller = self.make_controller(1)
        self.addCleanup(temporary.cleanup)
        self.assertFalse(
            controller.authorize(
                {
                    "action": "read",
                    "protocol": "rtsp",
                    "path": "live/stream_00000001",
                    "ip": "198.51.100.1",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()

