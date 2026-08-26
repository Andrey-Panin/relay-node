"""Signed desired-state download, validation and durable fail-open cache."""

from __future__ import annotations

import http.client
import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import DesiredState, StateValidationError
from .signing import canonical_json, verify_payload


class StateFetchError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the bearer token to a redirected host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _atomic_secret_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        # Directory fsync makes rename durable on Linux. Windows does not allow
        # opening a directory this way, which is relevant to local unit tests.
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class ManagerClient:
    def __init__(self, manager_url: str, relay_id: str, token: str, ssl_context=None):
        self._state_url = f"{manager_url}/api/v1/relay-state/{relay_id}"
        self._telemetry_url = f"{manager_url}/api/v1/relays/{relay_id}/telemetry"
        self._token = token
        handlers: list[Any] = [_NoRedirect()]
        if ssl_context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
        self._opener = urllib.request.build_opener(*handlers)

    def _request(self, request: urllib.request.Request, timeout: float = 10.0) -> bytes:
        try:
            with self._opener.open(request, timeout=timeout) as response:
                if response.status < 200 or response.status >= 300:
                    raise StateFetchError(f"manager returned HTTP {response.status}")
                return response.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise StateFetchError(f"manager returned HTTP {exc.code}") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise StateFetchError("manager request failed") from exc

    def fetch(self) -> dict[str, Any]:
        request = urllib.request.Request(
            self._state_url,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            method="GET",
        )
        raw = self._request(request)
        if len(raw) > 4 * 1024 * 1024:
            raise StateFetchError("manager response is too large")
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateFetchError("manager returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise StateFetchError("manager envelope must be an object")
        return decoded

    def send_telemetry(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self._telemetry_url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        self._request(request)


class StateStore:
    def __init__(self, relay_id: str, signing_key: bytes, cache_path: Path):
        self.relay_id = relay_id
        self.signing_key = signing_key
        self.cache_path = cache_path
        self._lock = threading.RLock()
        self._state: DesiredState | None = None
        self._envelope: dict[str, Any] | None = None
        self.last_manager_success: datetime | None = None
        self.last_error: str | None = None

    def _validate_envelope(
        self,
        envelope: dict[str, Any],
        *,
        now: datetime,
        allow_expired: bool,
    ) -> DesiredState:
        if set(envelope) != {"payload", "signature"}:
            raise StateValidationError("envelope must contain only payload and signature")
        payload = envelope["payload"]
        signature = envelope["signature"]
        if not isinstance(payload, dict) or not isinstance(signature, str):
            raise StateValidationError("invalid envelope types")
        if not verify_payload(payload, signature, self.signing_key):
            raise StateValidationError("invalid desired-state signature")
        state = DesiredState.from_payload(payload, self.relay_id)
        if state.issued_at > now + timedelta(minutes=2):
            raise StateValidationError("desired state is issued too far in the future")
        if not allow_expired and state.expires_at <= now:
            raise StateValidationError("desired state is expired")
        return state

    def load_cache(self, now: datetime | None = None) -> DesiredState | None:
        now = now or datetime.now(timezone.utc)
        try:
            raw = self.cache_path.read_bytes()
        except FileNotFoundError:
            return None
        if len(raw) > 4 * 1024 * 1024:
            raise StateValidationError("cached state is too large")
        try:
            envelope = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateValidationError("cached state is invalid JSON") from exc
        state = self._validate_envelope(envelope, now=now, allow_expired=True)
        with self._lock:
            self._state = state
            self._envelope = envelope
            self.last_error = "manager state is stale; using signed local cache" if state.expires_at <= now else None
        return state

    def accept_remote(self, envelope: dict[str, Any], now: datetime | None = None) -> DesiredState:
        now = now or datetime.now(timezone.utc)
        state = self._validate_envelope(envelope, now=now, allow_expired=False)
        with self._lock:
            if self._state is not None and state.generation < self._state.generation:
                raise StateValidationError("desired-state generation rollback refused")
            if self._state is not None and state.generation == self._state.generation:
                if self._envelope is None:
                    raise StateValidationError("same-generation state has no comparison envelope")
                old_payload = self._envelope.get("payload")
                new_payload = envelope.get("payload")
                if not isinstance(old_payload, dict) or not isinstance(new_payload, dict):
                    raise StateValidationError("same-generation comparison payload is invalid")
                old_immutable = {
                    key: value for key, value in old_payload.items() if key not in {"issued_at", "expires_at"}
                }
                new_immutable = {
                    key: value for key, value in new_payload.items() if key not in {"issued_at", "expires_at"}
                }
                if canonical_json(new_immutable) != canonical_json(old_immutable):
                    raise StateValidationError("same-generation desired-state content equivocation refused")
                if state.issued_at < self._state.issued_at or state.expires_at < self._state.expires_at:
                    raise StateValidationError("same-generation desired-state temporal rollback refused")
                # Exact replay is harmless. A signed monotonic TTL refresh from
                # WEB is cached atomically without requiring a new generation.
                if canonical_json(envelope) != canonical_json(self._envelope):
                    _atomic_secret_json(self.cache_path, envelope)
                    self._state = state
                    self._envelope = envelope
                self.last_manager_success = now
                self.last_error = None
                return state
            _atomic_secret_json(self.cache_path, envelope)
            self._state = state
            self._envelope = envelope
            self.last_manager_success = now
            self.last_error = None
        return state

    def mark_error(self, message: str) -> None:
        # Callers provide redacted, categorical messages only.
        with self._lock:
            self.last_error = message[:256]

    def snapshot(self) -> DesiredState | None:
        with self._lock:
            return self._state

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            state = self._state
            return {
                "available": state is not None,
                "generation": state.generation if state else None,
                "stale": state is None or state.expires_at <= now,
                "expires_at": state.expires_at.isoformat() if state else None,
                "last_manager_success": self.last_manager_success.isoformat() if self.last_manager_success else None,
                "last_error": self.last_error,
            }

