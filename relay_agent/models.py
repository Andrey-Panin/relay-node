"""Strict desired-state model and URL construction."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit


PLATFORMS = frozenset(
    {"chaturbate", "stripchat", "bongacams", "camsoda", "exclusivelive"}
)
STREAM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
CREDENTIAL_RE = re.compile(r"^[A-Za-z0-9_-]{24,192}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class StateValidationError(ValueError):
    pass


def _strict_keys(value: dict[str, Any], expected: set[str], where: str) -> None:
    found = set(value)
    if found != expected:
        missing = sorted(expected - found)
        extra = sorted(found - expected)
        raise StateValidationError(f"{where}: missing={missing}, extra={extra}")


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise StateValidationError("timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateValidationError("invalid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise StateValidationError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_ingest_url(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise StateValidationError("invalid ingest_url length")
    if CONTROL_RE.search(value):
        raise StateValidationError("ingest_url contains control characters")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"rtmp", "rtmps"}:
        raise StateValidationError("ingest_url must use rtmp or rtmps")
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise StateValidationError("ingest_url must not contain credentials or a fragment")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise StateValidationError("invalid ingest_url port") from exc
    # Normalize only the scheme and trailing slash; preserve explicit ports and paths.
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path.rstrip("/"), parsed.query, ""))


def validate_stream_key(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise StateValidationError("invalid destination stream key length")
    if CONTROL_RE.search(value):
        raise StateValidationError("destination stream key contains control characters")
    return value


def build_destination_url(ingest_url: str, stream_key: str) -> str:
    """Join a trusted RTMP(S) base and an opaque stream key.

    The key is deliberately not passed through urljoin(), since a key beginning
    with // or a scheme could otherwise replace the allowlisted destination host.
    """
    base = validate_ingest_url(ingest_url)
    key = validate_stream_key(stream_key).lstrip("/")
    if "?" in base:
        raise StateValidationError("ingest_url query is ambiguous; put it in stream_key")
    return base + "/" + key


@dataclass(frozen=True, slots=True)
class Destination:
    destination_id: str
    platform: str
    enabled: bool
    ingest_url: str
    stream_key: str

    @classmethod
    def from_dict(cls, value: Any) -> "Destination":
        if not isinstance(value, dict):
            raise StateValidationError("destination must be an object")
        _strict_keys(
            value,
            {"destination_id", "platform", "enabled", "ingest_url", "stream_key"},
            "destination",
        )
        destination_id = value["destination_id"]
        if not isinstance(destination_id, str):
            raise StateValidationError("destination_id must be a UUID string")
        try:
            parsed_id = uuid.UUID(destination_id)
        except ValueError as exc:
            raise StateValidationError("invalid destination_id") from exc
        if str(parsed_id) != destination_id.lower():
            raise StateValidationError("destination_id must use canonical UUID syntax")
        platform = value["platform"]
        if platform not in PLATFORMS:
            raise StateValidationError("unknown platform")
        if not isinstance(value["enabled"], bool):
            raise StateValidationError("destination enabled must be boolean")
        ingest_url = validate_ingest_url(value["ingest_url"])
        stream_key = validate_stream_key(value["stream_key"])
        # Validate joining now, while errors can reject the entire new generation.
        build_destination_url(ingest_url, stream_key)
        return cls(destination_id.lower(), platform, value["enabled"], ingest_url, stream_key)


@dataclass(frozen=True, slots=True)
class Stream:
    stream_id: str
    publish_credential: str
    enabled: bool
    destinations: tuple[Destination, ...]

    @property
    def path(self) -> str:
        return f"live/{self.stream_id}"

    @classmethod
    def from_dict(cls, value: Any) -> "Stream":
        if not isinstance(value, dict):
            raise StateValidationError("stream must be an object")
        _strict_keys(
            value,
            {"stream_id", "publish_credential", "enabled", "destinations"},
            "stream",
        )
        stream_id = value["stream_id"]
        credential = value["publish_credential"]
        if not isinstance(stream_id, str) or not STREAM_ID_RE.fullmatch(stream_id):
            raise StateValidationError("invalid stream_id")
        if not isinstance(credential, str) or not CREDENTIAL_RE.fullmatch(credential):
            raise StateValidationError("invalid publish_credential")
        if not isinstance(value["enabled"], bool):
            raise StateValidationError("stream enabled must be boolean")
        raw_destinations = value["destinations"]
        if not isinstance(raw_destinations, list):
            raise StateValidationError("destinations must be a list")
        destinations = tuple(Destination.from_dict(item) for item in raw_destinations)
        destination_ids = [item.destination_id for item in destinations]
        if len(destination_ids) != len(set(destination_ids)):
            raise StateValidationError("duplicate destination_id")
        return cls(stream_id, credential, value["enabled"], destinations)


@dataclass(frozen=True, slots=True)
class RelayLimits:
    """Signed relay-wide limits introduced with desired-state schema v2."""

    revision: int
    max_server_connections: int
    monthly_traffic_quota_bytes: int

    @classmethod
    def from_dict(cls, value: Any) -> "RelayLimits":
        if not isinstance(value, dict):
            raise StateValidationError("limits must be an object")
        _strict_keys(
            value,
            {"revision", "max_server_connections", "monthly_traffic_quota_bytes"},
            "limits",
        )
        parsed: dict[str, int] = {}
        for field in (
            "revision",
            "max_server_connections",
            "monthly_traffic_quota_bytes",
        ):
            item = value[field]
            if not isinstance(item, int) or isinstance(item, bool):
                raise StateValidationError(f"limits.{field} must be an integer")
            parsed[field] = item
        if parsed["revision"] < 0:
            raise StateValidationError("limits.revision must be non-negative")
        if parsed["max_server_connections"] < 1:
            raise StateValidationError("limits.max_server_connections must be positive")
        if parsed["monthly_traffic_quota_bytes"] < 1:
            raise StateValidationError(
                "limits.monthly_traffic_quota_bytes must be positive"
            )
        return cls(**parsed)


@dataclass(frozen=True, slots=True)
class DesiredState:
    schema_version: int
    relay_id: str
    generation: int
    issued_at: datetime
    expires_at: datetime
    streams: tuple[Stream, ...]
    limits: RelayLimits | None = None

    @classmethod
    def from_payload(cls, value: Any, expected_relay_id: str) -> "DesiredState":
        if not isinstance(value, dict):
            raise StateValidationError("payload must be an object")
        schema_version = value.get("schema_version")
        expected_keys = (
            {
                "schema_version",
                "relay_id",
                "generation",
                "issued_at",
                "expires_at",
                "streams",
            }
            if schema_version == 1
            else {
                "schema_version",
                "relay_id",
                "generation",
                "issued_at",
                "expires_at",
                "streams",
                "limits",
            }
        )
        _strict_keys(value, expected_keys, "payload")
        if schema_version not in {1, 2}:
            raise StateValidationError("unsupported schema_version")
        if value["relay_id"] != expected_relay_id:
            raise StateValidationError("relay_id mismatch")
        generation = value["generation"]
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise StateValidationError("generation must be a non-negative integer")
        issued_at = parse_utc(value["issued_at"])
        expires_at = parse_utc(value["expires_at"])
        if expires_at <= issued_at:
            raise StateValidationError("expires_at must be after issued_at")
        raw_streams = value["streams"]
        if not isinstance(raw_streams, list):
            raise StateValidationError("streams must be a list")
        streams = tuple(Stream.from_dict(item) for item in raw_streams)
        ids = [item.stream_id for item in streams]
        if len(ids) != len(set(ids)):
            raise StateValidationError("duplicate stream_id")
        limits = RelayLimits.from_dict(value["limits"]) if schema_version == 2 else None
        if limits is not None and limits.revision != generation:
            raise StateValidationError("limits.revision must match generation")
        return cls(
            schema_version,
            expected_relay_id,
            generation,
            issued_at,
            expires_at,
            streams,
            limits,
        )

    def stream_map(self) -> dict[str, Stream]:
        return {stream.stream_id: stream for stream in self.streams}
