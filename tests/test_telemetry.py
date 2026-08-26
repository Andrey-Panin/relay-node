from __future__ import annotations

import unittest

from relay_agent.telemetry import parse_srt_metrics


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


if __name__ == "__main__":
    unittest.main()

