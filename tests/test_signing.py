from __future__ import annotations

import unittest

from relay_agent.signing import derive_srt_passphrase, sign_payload

from .helpers import SIGNING_KEY


class GoldenVectorTests(unittest.TestCase):
    def test_web_and_relay_share_raw_key_contract(self):
        payload = {
            "expires_at": "2026-08-25T00:05:00Z",
            "generation": 7,
            "issued_at": "2026-08-25T00:00:00Z",
            "relay_id": "00000000-0000-0000-0000-000000000001",
            "schema_version": 1,
            "streams": [],
        }
        self.assertEqual(
            sign_payload(payload, SIGNING_KEY),
            "jW9fwH3ddY1J76ggIkPmFSkpX5McNP3V9htcXMLHKtk",
        )
        self.assertEqual(
            derive_srt_passphrase(SIGNING_KEY, "123e4567-e89b-12d3-a456-426614174000"),
            "gnqv0u-e207ThQgMi_0NSyNie96Zvrj2",
        )


if __name__ == "__main__":
    unittest.main()

