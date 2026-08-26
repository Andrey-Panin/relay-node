"""Canonical JSON signatures and deterministic SRT passphrases."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    """Return the byte representation shared with Relay Manager."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def b64url_no_padding(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode_b64url(value: str) -> bytes:
    padded = value + ("=" * (-len(value) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def sign_payload(payload: dict[str, Any], signing_key: bytes) -> str:
    return b64url_no_padding(
        hmac.new(signing_key, canonical_json(payload), hashlib.sha256).digest()
    )


def verify_payload(payload: dict[str, Any], signature: str, signing_key: bytes) -> bool:
    try:
        expected = sign_payload(payload, signing_key)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)


def derive_srt_passphrase(signing_key: bytes, stream_id: str) -> str:
    """Derive the same 32-character per-stream passphrase as the WEB service.

    Contract v1 (do not change in place):
      base64url(HMAC-SHA256(key, UTF8("srt-passphrase:v1:" + stream_id)))[:32]
    """
    material = ("srt-passphrase:v1:" + stream_id).encode("utf-8")
    return b64url_no_padding(hmac.new(signing_key, material, hashlib.sha256).digest())[:32]

