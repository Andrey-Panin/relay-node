from __future__ import annotations

import http.client
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from relay_agent.models import StateValidationError
from relay_agent.signing import sign_payload
from relay_agent.state_store import ManagerClient, StateFetchError, StateStore

from .helpers import RELAY_ID, SIGNING_KEY, envelope, stream


class ManagerClientTests(unittest.TestCase):
    def setUp(self):
        self.client = ManagerClient("https://manager.invalid", RELAY_ID, "token")

    def test_timeout_is_wrapped_as_state_fetch_error(self):
        error = TimeoutError("timed out")
        with mock.patch.object(self.client._opener, "open", side_effect=error):
            with self.assertRaises(StateFetchError) as caught:
                self.client.fetch()
        self.assertIs(caught.exception.__cause__, error)

    def test_remote_disconnect_is_wrapped_as_state_fetch_error(self):
        error = http.client.RemoteDisconnected("connection closed")
        with mock.patch.object(self.client._opener, "open", side_effect=error):
            with self.assertRaises(StateFetchError) as caught:
                self.client.fetch()
        self.assertIs(caught.exception.__cause__, error)

    def test_incomplete_read_is_wrapped_as_state_fetch_error(self):
        error = http.client.IncompleteRead(b"partial", 10)
        response = mock.MagicMock(status=200)
        response.__enter__.return_value = response
        response.read.side_effect = error
        with mock.patch.object(self.client._opener, "open", return_value=response):
            with self.assertRaises(StateFetchError) as caught:
                self.client.fetch()
        self.assertIs(caught.exception.__cause__, error)


class StateStoreTests(unittest.TestCase):
    def test_same_generation_equivocation_is_rejected(self):
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            store = StateStore(RELAY_ID, SIGNING_KEY, Path(folder) / "state.json")
            first = envelope(generation=3, streams=[stream(1)], now=now)
            store.accept_remote(first, now=now)
            changed = envelope(generation=3, streams=[stream(2)], now=now)
            with self.assertRaises(StateValidationError):
                store.accept_remote(changed, now=now)

    def test_same_generation_monotonic_ttl_refresh_is_accepted_and_cached(self):
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            store = StateStore(RELAY_ID, SIGNING_KEY, path)
            first = envelope(generation=3, streams=[stream(1)], now=now)
            store.accept_remote(first, now=now)
            refreshed = envelope(
                generation=3,
                streams=[stream(1)],
                now=now + timedelta(minutes=1),
            )
            state = store.accept_remote(refreshed, now=now + timedelta(minutes=1))
            self.assertEqual(state.issued_at, now + timedelta(minutes=1))
            reloaded = StateStore(RELAY_ID, SIGNING_KEY, path)
            cached = reloaded.load_cache(now=now + timedelta(minutes=2))
            self.assertEqual(cached.issued_at, now + timedelta(minutes=1))

    def test_same_generation_temporal_rollback_is_rejected(self):
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            store = StateStore(RELAY_ID, SIGNING_KEY, Path(folder) / "state.json")
            first = envelope(
                generation=3,
                streams=[stream(1)],
                now=now + timedelta(minutes=10),
            )
            store.accept_remote(first, now=now + timedelta(minutes=10))
            rollback = envelope(
                generation=3,
                streams=[stream(1)],
                now=now + timedelta(minutes=9),
            )
            with self.assertRaises(StateValidationError):
                store.accept_remote(rollback, now=now + timedelta(minutes=11))

    def test_expired_but_signed_cache_is_usable_during_web_outage(self):
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            store = StateStore(RELAY_ID, SIGNING_KEY, path)
            value = envelope(streams=[stream(1)], now=now)
            store.accept_remote(value, now=now)
            reloaded = StateStore(RELAY_ID, SIGNING_KEY, path)
            state = reloaded.load_cache(now=now.replace(hour=1))
            self.assertIsNotNone(state)
            self.assertTrue(reloaded.status(now=now.replace(hour=1))["stale"])

    def test_bad_signature_is_rejected(self):
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            store = StateStore(RELAY_ID, SIGNING_KEY, Path(folder) / "state.json")
            value = envelope(now=now)
            value["payload"]["generation"] = 99
            with self.assertRaises(StateValidationError):
                store.accept_remote(value, now=now)


if __name__ == "__main__":
    unittest.main()

