"""Environment and systemd-credential configuration."""

from __future__ import annotations

import ipaddress
import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .signing import decode_b64url


class ConfigError(RuntimeError):
    pass


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _read_secret(name: str) -> str:
    credentials_dir = os.getenv("CREDENTIALS_DIRECTORY")
    explicit = os.getenv(f"{name}_FILE")
    path = Path(explicit) if explicit else Path(credentials_dir or "/run/credentials/relay-agent.service") / name.lower()
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"unable to read credential {name} from {path}") from exc
    if not value:
        raise ConfigError(f"credential {name} is empty")
    return value


@dataclass(frozen=True, slots=True)
class Config:
    relay_id: str
    manager_url: str
    manager_token: str
    signing_key: bytes
    manager_ca_file: str | None
    allow_insecure_manager_http: bool
    mediamtx_api_url: str
    mediamtx_input_url: str
    cache_path: Path
    managed_paths_path: Path
    traffic_state_path: Path
    listen_host: str
    listen_port: int
    state_poll_seconds: int
    path_poll_seconds: int
    telemetry_seconds: int
    max_active_models: int
    traffic_quota_bytes: int
    ffmpeg_path: str

    @classmethod
    def from_env(cls) -> "Config":
        relay_id = os.getenv("RELAY_ID", "").strip()
        if not relay_id:
            raise ConfigError("RELAY_ID is required")
        manager_url = os.getenv("MANAGER_URL", "").rstrip("/")
        parsed = urlsplit(manager_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigError("MANAGER_URL must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigError("MANAGER_URL must not contain credentials, query, or fragment")
        allow_http = os.getenv("ALLOW_INSECURE_MANAGER_HTTP", "false").lower() in {"1", "true", "yes"}
        if parsed.scheme == "http" and not allow_http:
            try:
                is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                is_loopback = parsed.hostname == "localhost"
            if not is_loopback:
                raise ConfigError("plain HTTP manager is refused; configure HTTPS/CA or explicit break-glass override")
        token = _read_secret("MANAGER_TOKEN")
        signing_encoded = _read_secret("STATE_SIGNING_KEY")
        try:
            signing_key = decode_b64url(signing_encoded)
        except Exception as exc:  # base64 errors vary between Python versions
            raise ConfigError("STATE_SIGNING_KEY must be base64url") from exc
        if len(signing_key) < 32:
            raise ConfigError("STATE_SIGNING_KEY must decode to at least 32 bytes")
        state_dir = Path(os.getenv("STATE_DIRECTORY", "/var/lib/relay-agent").split(":")[0])
        ca_path = os.getenv("MANAGER_CA_FILE") or None
        if ca_path and not Path(ca_path).is_file():
            raise ConfigError("MANAGER_CA_FILE does not exist")
        return cls(
            relay_id=relay_id,
            manager_url=manager_url,
            manager_token=token,
            signing_key=signing_key,
            manager_ca_file=ca_path,
            allow_insecure_manager_http=allow_http,
            mediamtx_api_url=os.getenv("MEDIAMTX_API_URL", "http://127.0.0.1:9997").rstrip("/"),
            mediamtx_input_url=os.getenv("MEDIAMTX_INPUT_URL", "rtsp://127.0.0.1:8554").rstrip("/"),
            cache_path=state_dir / "desired-state.json",
            managed_paths_path=state_dir / "managed-paths.json",
            traffic_state_path=state_dir / "traffic-accounting.json",
            listen_host=os.getenv("AGENT_LISTEN_HOST", "127.0.0.1"),
            listen_port=_env_int("AGENT_LISTEN_PORT", 8091),
            state_poll_seconds=_env_int("STATE_POLL_SECONDS", 5),
            path_poll_seconds=_env_int("PATH_POLL_SECONDS", 2),
            telemetry_seconds=_env_int("TELEMETRY_SECONDS", 15),
            max_active_models=_env_int("MAX_ACTIVE_MODELS", 15),
            traffic_quota_bytes=_env_int("TRAFFIC_QUOTA_BYTES", 32_000_000_000_000),
            ffmpeg_path=os.getenv("FFMPEG_PATH", "/usr/bin/ffmpeg"),
        )

    def ssl_context(self) -> ssl.SSLContext | None:
        if self.manager_url.startswith("https://"):
            return ssl.create_default_context(cafile=self.manager_ca_file)
        return None

