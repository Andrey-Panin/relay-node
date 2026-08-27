"""Loopback HTTP authentication, health and Prometheus endpoints."""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .models import STREAM_ID_RE
from .state_store import StateStore
from .telemetry import TelemetryCollector


LOG = logging.getLogger("relay_agent.http")


def _is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        try:
            host = urlsplit("//" + value).hostname
            return host is not None and ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False


class AdmissionController:
    def __init__(self, state_store: StateStore, max_active_models: int):
        self.state_store = state_store
        self.max_active_models = max_active_models
        self._lock = threading.RLock()
        self._ready_paths: set[str] = set()
        self._reservations: dict[str, float] = {}

    def set_max_server_connections(self, value: int) -> None:
        """Apply a signed cap without disturbing already-publishing clients."""
        if value < 1:
            raise ValueError("max_server_connections must be positive")
        with self._lock:
            self.max_active_models = value

    def update_ready_paths(self, paths: dict[str, dict]) -> None:
        ready = {
            name
            for name, item in paths.items()
            if name.startswith("live/") and item.get("online", item.get("ready", False)) is True
        }
        with self._lock:
            self._ready_paths = ready
            for path in ready:
                self._reservations.pop(path, None)

    def active_count(self) -> int:
        with self._lock:
            return len(self._ready_paths)

    def limit_status(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "max_server_connections": self.max_active_models,
                "active_publishers": len(self._ready_paths),
                "over_capacity": len(self._ready_paths) > self.max_active_models,
            }

    def _reserve_capacity(self, path: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._reservations = {
                item: deadline for item, deadline in self._reservations.items() if deadline > now
            }
            if path in self._ready_paths or path in self._reservations:
                return True
            occupied = self._ready_paths | set(self._reservations)
            if len(occupied) >= self.max_active_models:
                return False
            self._reservations[path] = now + 20
            return True

    @staticmethod
    def _stream_id(path: object) -> str | None:
        if not isinstance(path, str) or not path.startswith("live/"):
            return None
        stream_id = path.removeprefix("live/")
        return stream_id if STREAM_ID_RE.fullmatch(stream_id) else None

    def authorize(self, request: object) -> bool:
        if not isinstance(request, dict):
            return False
        action = request.get("action")
        path = request.get("path")
        protocol = request.get("protocol")
        stream_id = self._stream_id(path)
        state = self.state_store.snapshot()
        if state is None or stream_id is None:
            return False
        stream = state.stream_map().get(stream_id)
        if stream is None or not stream.enabled:
            return False

        if action == "read":
            return protocol == "rtsp" and _is_loopback(str(request.get("ip", "")))
        if action != "publish" or protocol not in {"srt", "rtmp"}:
            return False
        user = request.get("user")
        password = request.get("password")
        if not isinstance(user, str) or not isinstance(password, str):
            return False
        if not hmac.compare_digest(user, "source"):
            return False
        if not hmac.compare_digest(password, stream.publish_credential):
            return False
        return self._reserve_capacity(stream.path)


class AgentHTTPServer:
    def __init__(
        self,
        host: str,
        port: int,
        admission: AdmissionController,
        state_store: StateStore,
        telemetry: TelemetryCollector,
        health_provider,
    ):
        self.admission = admission
        self.state_store = state_store
        self.telemetry = telemetry
        self.health_provider = health_provider
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "relay-agent"
            sys_version = ""

            def log_message(self, fmt, *args):  # noqa: ANN001
                # Do not let request paths or bodies drift into access logs.
                return

            def _reply(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                if self.path != "/v1/auth":
                    self._reply(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if length < 0 or length > 65536:
                    self._reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b"denied\n", "text/plain")
                    return
                try:
                    payload = json.loads(self.rfile.read(length))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._reply(HTTPStatus.BAD_REQUEST, b"denied\n", "text/plain")
                    return
                allowed = outer.admission.authorize(payload)
                self._reply(
                    HTTPStatus.OK if allowed else HTTPStatus.UNAUTHORIZED,
                    b"ok\n" if allowed else b"denied\n",
                    "text/plain",
                )

            def do_GET(self):  # noqa: N802
                if self.path in {"/healthz", "/readyz"}:
                    health = outer.health_provider()
                    body = json.dumps(health, separators=(",", ":")).encode("utf-8")
                    usable = health.get("mediamtx_healthy") and health.get("state_available")
                    self._reply(
                        HTTPStatus.OK if usable else HTTPStatus.SERVICE_UNAVAILABLE,
                        body,
                        "application/json",
                    )
                elif self.path == "/metrics":
                    self._reply(
                        HTTPStatus.OK,
                        outer.telemetry.prometheus().encode("utf-8"),
                        "text/plain; version=0.0.4",
                    )
                else:
                    self._reply(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="http", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
