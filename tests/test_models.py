from __future__ import annotations

import unittest

from relay_agent.models import DesiredState, StateValidationError, build_destination_url

from .helpers import RELAY_ID, destination, envelope, stream


class ModelTests(unittest.TestCase):
    def test_same_platform_destinations_are_allowed_when_ids_differ(self):
        state = DesiredState.from_payload(
            envelope(streams=[stream(1, ("chaturbate", "chaturbate"))])["payload"],
            RELAY_ID,
        )
        self.assertEqual(
            [item.platform for item in state.streams[0].destinations],
            ["chaturbate", "chaturbate"],
        )

    def test_all_four_platforms_and_port_are_preserved(self):
        platforms = ("chaturbate", "stripchat", "bongacams", "camsoda")
        state = DesiredState.from_payload(envelope(streams=[stream(1, platforms)])["payload"], RELAY_ID)
        self.assertEqual([item.platform for item in state.streams[0].destinations], list(platforms))
        bonga = state.streams[0].destinations[2]
        self.assertTrue(build_destination_url(bonga.ingest_url, bonga.stream_key).startswith("rtmp://ingest-bongacams.example.test:1934/"))

    def test_url_join_cannot_replace_allowlisted_host(self):
        result = build_destination_url("rtmp://trusted.example/live", "//evil.example/key")
        self.assertEqual(result, "rtmp://trusted.example/live/evil.example/key")

    def test_non_rtmp_destination_is_rejected(self):
        item = destination("chaturbate")
        item["ingest_url"] = "https://evil.example/upload"
        value = stream()
        value["destinations"] = [item]
        with self.assertRaises(StateValidationError):
            DesiredState.from_payload(envelope(streams=[value])["payload"], RELAY_ID)


if __name__ == "__main__":
    unittest.main()
