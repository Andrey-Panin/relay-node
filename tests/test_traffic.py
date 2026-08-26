from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from relay_agent.traffic import TrafficAccountant


class TrafficTests(unittest.TestCase):
    def test_projection_uses_actual_tracking_duration(self):
        samples = iter([(100, 200), (1100, 1200)])
        with tempfile.TemporaryDirectory() as folder:
            accountant = TrafficAccountant(
                Path(folder) / "traffic.json",
                10_000_000,
                network_reader=lambda: next(samples),
                boot_reader=lambda: "boot-a",
            )
            started = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
            first = accountant.sample(started)
            second = accountant.sample(started + timedelta(minutes=10))
            self.assertEqual(first["tx_bytes"], 200)
            self.assertEqual(second["tx_bytes"], 1200)
            self.assertGreater(second["projected_tx_bytes"], second["tx_bytes"])
            self.assertEqual(second["tracking_started_at"], started.isoformat())

    def test_warning_thresholds_do_not_stop_accounting(self):
        samples = iter([(0, 70), (0, 100)])
        with tempfile.TemporaryDirectory() as folder:
            accountant = TrafficAccountant(
                Path(folder) / "traffic.json",
                100,
                network_reader=lambda: next(samples),
                boot_reader=lambda: "boot-a",
            )
            now = datetime(2026, 8, 1, tzinfo=timezone.utc)
            self.assertEqual(accountant.sample(now)["warning_level"], 70)
            self.assertEqual(accountant.sample(now + timedelta(minutes=10))["warning_level"], 100)


if __name__ == "__main__":
    unittest.main()

