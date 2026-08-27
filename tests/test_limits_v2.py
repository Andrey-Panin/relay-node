from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from relay_agent.http_server import AdmissionController
from relay_agent.models import DesiredState
from relay_agent.state_store import StateStore
from relay_agent.traffic import TrafficAccountant
from relay_agent.worker import WorkerSupervisor

from .helpers import RELAY_ID, SIGNING_KEY, envelope, stream


def v2_envelope(
    *,
    generation: int = 2,
    max_connections: int = 1,
    quota_bytes: int = 1234,
    streams: list[dict] | None = None,
):
    value = envelope(
        generation=generation,
        streams=streams or [stream(1), stream(2), stream(3)],
    )
    payload = value["payload"]
    payload["schema_version"] = 2
    payload["limits"] = {
        "revision": generation,
        "max_server_connections": max_connections,
        "monthly_traffic_quota_bytes": quota_bytes,
    }
    from relay_agent.signing import sign_payload

    value["signature"] = sign_payload(payload, SIGNING_KEY)
    return value


class V2LimitsTests(unittest.TestCase):
    def test_v2_limits_are_strict_and_include_exclusivelive(self):
        raw = v2_envelope()
        raw["payload"]["streams"][0]["destinations"] = [
            {
                "destination_id": "00000000-0000-0000-0000-000000000099",
                "platform": "exclusivelive",
                "enabled": True,
                "ingest_url": "rtmp://streaming.exclulive.com/LiveApp/",
                "stream_key": "exclusive-key",
            }
        ]
        state = DesiredState.from_payload(raw["payload"], RELAY_ID)
        self.assertEqual(state.schema_version, 2)
        self.assertEqual(state.limits.max_server_connections, 1)
        self.assertEqual(
            state.streams[0].destinations[0].platform,
            "exclusivelive",
        )

    def test_v2_limit_revision_must_match_signed_generation(self):
        raw = v2_envelope(generation=7)
        raw["payload"]["limits"]["revision"] = 6
        with self.assertRaisesRegex(
            ValueError,
            "limits.revision must match generation",
        ):
            DesiredState.from_payload(raw["payload"], RELAY_ID)

    def test_lowered_cap_keeps_existing_publishers_but_denies_new_ones(self):
        with tempfile.TemporaryDirectory() as folder:
            store = StateStore(RELAY_ID, SIGNING_KEY, Path(folder) / "state.json")
            store.accept_remote(
                v2_envelope(
                    max_connections=1,
                    streams=[
                        stream(1, ("chaturbate",)),
                        stream(2, ("chaturbate",)),
                        stream(3, ("chaturbate",)),
                    ],
                )
            )
            controller = AdmissionController(store, 15)
            controller.set_max_server_connections(1)
            controller.update_ready_paths(
                {
                    "live/stream_00000001": {"online": True},
                    "live/stream_00000002": {"online": True},
                }
            )
            self.assertTrue(controller.limit_status()["over_capacity"])
            self.assertFalse(
                controller.authorize(
                    {
                        "action": "publish",
                        "protocol": "srt",
                        "path": "live/stream_00000003",
                        "user": "source",
                        "password": "credential_00000003_abcdefghijklmnop",
                    }
                )
            )

            supervisor = WorkerSupervisor(
                "/bin/true",
                "rtsp://127.0.0.1:8554",
                15,
            )
            supervisor.set_max_server_connections(1)
            with patch("relay_agent.worker.DestinationWorker.start"):
                supervisor.reconcile(
                    store.snapshot(),
                    {
                        "live/stream_00000001": {"online": True},
                        "live/stream_00000002": {"online": True},
                    },
                )
            self.assertTrue(supervisor.over_capacity)
            self.assertEqual(len(supervisor.statuses()), 2)

    def test_quota_can_change_without_resetting_counters(self):
        samples = iter(((0, 100), (0, 200)))
        with tempfile.TemporaryDirectory() as folder:
            accountant = TrafficAccountant(
                Path(folder) / "traffic.json",
                1_000,
                network_reader=lambda: next(samples),
                boot_reader=lambda: "boot-a",
            )
            accountant.sample()
            accountant.set_quota_bytes(150)
            second = accountant.sample()
            self.assertEqual(second["tx_bytes"], 200)
            self.assertEqual(second["quota_bytes"], 150)


if __name__ == "__main__":
    unittest.main()
