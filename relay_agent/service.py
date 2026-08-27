"""Relay-agent orchestration loops."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone

from .config import Config
from .http_server import AdmissionController, AgentHTTPServer
from .mediamtx import MediaMTXClient, MediaMTXError, PathReconciler
from .models import StateValidationError
from .state_store import ManagerClient, StateFetchError, StateStore
from .telemetry import TelemetryCollector
from .traffic import TrafficAccountant
from .worker import WorkerSupervisor


LOG = logging.getLogger("relay_agent")


class Runtime:
    def __init__(self, config: Config):
        self.config = config
        self.stop_event = threading.Event()
        self.state_store = StateStore(config.relay_id, config.signing_key, config.cache_path)
        self.manager = ManagerClient(
            config.manager_url,
            config.relay_id,
            config.manager_token,
            config.ssl_context(),
        )
        self.mediamtx = MediaMTXClient(config.mediamtx_api_url)
        self.path_reconciler = PathReconciler(
            self.mediamtx,
            config.signing_key,
            config.managed_paths_path,
        )
        self.supervisor = WorkerSupervisor(
            config.ffmpeg_path,
            config.mediamtx_input_url,
            config.max_active_models,
        )
        self.admission = AdmissionController(self.state_store, config.max_active_models)
        self.traffic = TrafficAccountant(config.traffic_state_path, config.traffic_quota_bytes)
        self.telemetry = TelemetryCollector(config.relay_id, "http://127.0.0.1:9998/metrics")
        self._runtime_lock = threading.RLock()
        self._paths: dict[str, dict] = {}
        self._mediamtx_healthy = False
        self._applied_limit_revision: int | None = None
        self.http = AgentHTTPServer(
            config.listen_host,
            config.listen_port,
            self.admission,
            self.state_store,
            self.telemetry,
            self.health,
        )
        self._threads: list[threading.Thread] = []
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False

    def health(self) -> dict[str, object]:
        state = self.state_store.status()
        with self._runtime_lock:
            mediamtx_healthy = self._mediamtx_healthy
            applied_limit_revision = self._applied_limit_revision
        limit_status = self.admission.limit_status()
        return {
            "status": "ok" if mediamtx_healthy and state["available"] else "unavailable",
            "mediamtx_healthy": mediamtx_healthy,
            "state_available": state["available"],
            "state_stale": state["stale"],
            "generation": state["generation"],
            "active_streams": self.admission.active_count(),
            "worker_count": len(self.supervisor.statuses()),
            "over_capacity": self.supervisor.over_capacity,
            "applied_limit_revision": applied_limit_revision,
            **limit_status,
            "monthly_traffic_quota_bytes": self.traffic.quota_bytes,
        }

    def _apply_limits(self, state) -> None:  # Avoid a runtime-only import cycle.
        """Apply v2 limits, or retain the documented v1 environment defaults."""
        limits = state.limits
        max_connections = (
            limits.max_server_connections
            if limits is not None
            else self.config.max_active_models
        )
        quota_bytes = (
            limits.monthly_traffic_quota_bytes
            if limits is not None
            else self.config.traffic_quota_bytes
        )
        self.admission.set_max_server_connections(max_connections)
        self.supervisor.set_max_server_connections(max_connections)
        self.traffic.set_quota_bytes(quota_bytes)
        with self._runtime_lock:
            self._applied_limit_revision = (
                limits.revision if limits is not None else None
            )

    def _state_loop(self) -> None:
        last_logged = 0.0
        last_generation: int | None = None
        while not self.stop_event.is_set():
            try:
                envelope = self.manager.fetch()
                state = self.state_store.accept_remote(envelope)
                self._apply_limits(state)
                self.path_reconciler.reconcile(state)
                if state.generation != last_generation:
                    LOG.info(
                        "desired state reconciled generation=%s streams=%s",
                        state.generation,
                        len(state.streams),
                    )
                    last_generation = state.generation
            except (StateFetchError, StateValidationError, MediaMTXError) as exc:
                self.state_store.mark_error(type(exc).__name__)
                now = time.monotonic()
                if now - last_logged >= 60:
                    LOG.warning("desired-state sync degraded category=%s; signed cache remains active", type(exc).__name__)
                    last_logged = now
            self.stop_event.wait(self.config.state_poll_seconds)

    def _path_loop(self) -> None:
        last_reconcile = 0.0
        last_over_capacity = False
        while not self.stop_event.is_set():
            try:
                paths = self.mediamtx.list_active_paths()
            except MediaMTXError:
                with self._runtime_lock:
                    self._mediamtx_healthy = False
                # Existing FFmpeg processes are left to fail/reconnect naturally;
                # supervisor cleanup happens when their publishers disappear.
                self.stop_event.wait(self.config.path_poll_seconds)
                continue
            with self._runtime_lock:
                self._paths = paths
                self._mediamtx_healthy = True
            self.admission.update_ready_paths(paths)
            state = self.state_store.snapshot()
            if state is not None:
                self.supervisor.reconcile(state, paths)
                # Reassert runtime path configuration after MediaMTX restarts.
                if time.monotonic() - last_reconcile >= 30:
                    try:
                        self.path_reconciler.reconcile(state)
                        last_reconcile = time.monotonic()
                    except MediaMTXError:
                        LOG.error("managed MediaMTX path reconciliation failed")
            if self.supervisor.over_capacity and not last_over_capacity:
                limit_status = self.admission.limit_status()
                LOG.error(
                    "relay is over capacity active=%s limit=%s; no additional workers admitted",
                    limit_status["active_publishers"],
                    limit_status["max_server_connections"],
                )
            last_over_capacity = self.supervisor.over_capacity
            self.stop_event.wait(self.config.path_poll_seconds)

    def _telemetry_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                traffic = self.traffic.sample()
                with self._runtime_lock:
                    paths = dict(self._paths)
                    healthy = self._mediamtx_healthy
                    applied_limit_revision = self._applied_limit_revision
                limit_status = self.admission.limit_status()
                self.telemetry.collect(
                    state=self.state_store.snapshot(),
                    path_items=paths,
                    state_status=self.state_store.status(),
                    worker_statuses=self.supervisor.statuses(),
                    traffic=traffic,
                    mediamtx_healthy=healthy,
                    applied_limits={
                        "revision": applied_limit_revision,
                        "max_server_connections": limit_status[
                            "max_server_connections"
                        ],
                        "monthly_traffic_quota_bytes": self.traffic.quota_bytes,
                    },
                )
                self.manager.send_telemetry(self.telemetry.manager_payload())
            except (OSError, ValueError, StateFetchError):
                LOG.warning("telemetry sample or delivery failed", exc_info=False)
            self.stop_event.wait(self.config.telemetry_seconds)

    def start(self) -> None:
        if not os.path.isfile(self.config.ffmpeg_path) or not os.access(self.config.ffmpeg_path, os.X_OK):
            raise RuntimeError("configured FFmpeg binary is missing or not executable")
        try:
            cached = self.state_store.load_cache()
            if cached:
                self._apply_limits(cached)
                LOG.warning(
                    "bootstrapped from signed cache generation=%s expires_at=%s",
                    cached.generation,
                    cached.expires_at.isoformat(),
                )
        except StateValidationError:
            LOG.error("signed desired-state cache is invalid; waiting for manager")
        # Promote capabilities with the existing per-relay token. This does not
        # re-enroll the node, rotate secrets, or restart MediaMTX.
        try:
            self.manager.update_capabilities(
                agent_version=self.config.agent_version,
                supported_state_schemas=[1, 2],
            )
        except (OSError, StateFetchError):
            LOG.warning(
                "capability promotion unavailable; continuing with signed state",
                exc_info=False,
            )
        self.http.start()
        for name, target in (
            ("state", self._state_loop),
            ("paths", self._path_loop),
            ("telemetry", self._telemetry_loop),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()
        LOG.info("relay agent started relay_id=%s", self.config.relay_id)

    def stop(self) -> None:
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
            self.stop_event.set()
            self.http.stop()
            self.supervisor.stop_all()
            for thread in self._threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=10)
            LOG.info("relay agent stopped")


def run(config: Config) -> None:
    runtime = Runtime(config)

    def handle_signal(_signum, _frame):  # noqa: ANN001
        runtime.stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    runtime.start()
    while not runtime.stop_event.wait(1):
        pass
    runtime.stop()
