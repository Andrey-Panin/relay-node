from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from relay_agent.mediamtx import MediaMTXClient, MediaMTXError, PathReconciler
from relay_agent.models import DesiredState

from .helpers import RELAY_ID, SIGNING_KEY, envelope, stream


class RecordingClient(MediaMTXClient):
    def __init__(self):
        super().__init__("http://127.0.0.1:9997")
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {}


class MediaMTXPathTests(unittest.TestCase):
    def test_named_path_methods_keep_literal_slash(self):
        client = RecordingClient()
        name = "live/123e4567-e89b-12d3-a456-426614174000"
        client.get_path_config(name)
        client.add_path(name, {})
        client.patch_path(name, {})
        client.delete_path(name)
        self.assertEqual(
            [call[1] for call in client.calls],
            [
                f"/v3/config/paths/get/{name}",
                f"/v3/config/paths/add/{name}",
                f"/v3/config/paths/patch/{name}",
                f"/v3/config/paths/delete/{name}",
            ],
        )
        self.assertNotIn("%2F", "".join(call[1] for call in client.calls))

    def test_traversal_is_rejected_before_http(self):
        client = RecordingClient()
        with self.assertRaises(MediaMTXError):
            client.add_path("live/../other", {})
        self.assertEqual(client.calls, [])

    def test_reconcile_does_not_patch_unchanged_path(self):
        class ReconcileClient:
            def __init__(self):
                self.patch_calls = 0
                self.value = None

            def get_path_config(self, _name):
                return self.value

            def add_path(self, _name, values):
                self.value = {"name": "live/stream_00000001", **values}

            def patch_path(self, _name, values):
                self.patch_calls += 1
                self.value.update(values)

            def delete_path(self, _name):
                self.value = None

        state = DesiredState.from_payload(envelope(streams=[stream(1)])["payload"], RELAY_ID)
        with tempfile.TemporaryDirectory() as folder:
            client = ReconcileClient()
            reconciler = PathReconciler(client, SIGNING_KEY, Path(folder) / "managed.json")
            reconciler.reconcile(state)
            reconciler.reconcile(state)
            self.assertEqual(client.patch_calls, 0)


if __name__ == "__main__":
    unittest.main()

