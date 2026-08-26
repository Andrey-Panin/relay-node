from __future__ import annotations

from datetime import datetime, timedelta, timezone

from relay_agent.signing import sign_payload


SIGNING_KEY = bytes(range(32))
RELAY_ID = "00000000-0000-0000-0000-000000000001"


def destination(platform: str, index: int = 1) -> dict:
    return {
        "destination_id": f"00000000-0000-0000-0000-{index:012d}",
        "platform": platform,
        "enabled": True,
        "ingest_url": f"rtmp://ingest-{platform}.example.test:1934/live",
        "stream_key": f"fake_{platform}_key_{index}",
    }


def stream(index: int = 1, platforms: tuple[str, ...] = ()) -> dict:
    return {
        "stream_id": f"stream_{index:08d}",
        "publish_credential": f"credential_{index:08d}_abcdefghijklmnop",
        "enabled": True,
        "destinations": [destination(platform, index * 10 + pos + 1) for pos, platform in enumerate(platforms)],
    }


def envelope(
    *,
    generation: int = 1,
    streams: list[dict] | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    payload = {
        "schema_version": 1,
        "relay_id": RELAY_ID,
        "generation": generation,
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "streams": streams or [],
    }
    return {"payload": payload, "signature": sign_payload(payload, SIGNING_KEY)}

