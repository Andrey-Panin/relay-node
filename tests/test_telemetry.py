from __future__ import annotations

import unittest
from unittest.mock import patch

from relay_agent.models import DesiredState
from relay_agent.telemetry import TelemetryCollector, parse_srt_metrics

from .helpers import RELAY_ID, envelope, stream


class TelemetryTests(unittest.TestCase):
    def test_srt_metrics_are_grouped_without_remote_address(self):
        raw = """
srt_conns_ms_rtt{id="abc",path="live/stream_00000001",remoteAddr="198.51.100.1:1",state="publish"} 42.5
srt_conns_packets_received_loss{id="abc",path="live/stream_00000001",remoteAddr="198.51.100.1:1",state="publish"} 3
srt_conns_packets_received_retrans{id="abc",path="live/stream_00000001",remoteAddr="198.51.100.1:1",state="publish"} 7
"""
        value = parse_srt_metrics(raw)
        self.assertEqual(value[0]["stream_id"], "stream_00000001")
        self.assertEqual(value[0]["rtt_ms"], 42.5)
        self.assertEqual(value[0]["packets_lost"], 3)
        self.assertNotIn("remoteAddr", value[0])

    def test_same_platform_workers_are_matched_by_destination_id(self):
        payload = envelope(streams=[stream(1, ("chaturbate", "chaturbate"))])["payload"]
        state = DesiredState.from_payload(payload, RELAY_ID)
        first, second = state.streams[0].destinations
        collector = TelemetryCollector(RELAY_ID, "http://127.0.0.1:9998/metrics")
        workers = [
            {
                "stream_id": state.streams[0].stream_id,
                "destination_id": first.destination_id,
                "platform": first.platform,
                "state": "online",
                "restart_count": 1,
            },
            {
                "stream_id": state.streams[0].stream_id,
                "destination_id": second.destination_id,
                "platform": second.platform,
                "state": "backoff",
                "restart_count": 3,
                "error_code": "connection_reset",
            },
        ]
        with (
            patch("relay_agent.telemetry._read_cpu", return_value=(100, 20)),
            patch("relay_agent.telemetry._read_memory", return_value=(1000, 250)),
            patch.object(collector, "fetch_mediamtx_metrics", return_value=""),
        ):
            snapshot = collector.collect(
                state=state,
                path_items={state.streams[0].path: {"online": True}},
                state_status={"available": True, "stale": False},
                worker_statuses=workers,
                traffic={},
                mediamtx_healthy=True,
                applied_limits={
                    "revision": 1,
                    "max_server_connections": 15,
                    "monthly_traffic_quota_bytes": 32_000_000_000_000,
                },
            )
        reported = {
            item["destination_id"]: item
            for item in snapshot["streams"][0]["destinations"]
        }
        self.assertEqual(reported[first.destination_id]["status"], "online")
        self.assertEqual(reported[first.destination_id]["reconnect_count"], 1)
        self.assertEqual(reported[second.destination_id]["status"], "backoff")
        self.assertEqual(reported[second.destination_id]["reconnect_count"], 3)


if __name__ == "__main__":
    unittest.main()
