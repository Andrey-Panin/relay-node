"""Lightweight host, MediaMTX and worker telemetry."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import DesiredState


SRT_METRIC_RE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)\{([^}]*)\}\s+(-?(?:\d+(?:\.\d*)?|\.\d+))$')
LABEL_RE = re.compile(r'(\w+)="((?:\\.|[^"\\])*)"(?:,|$)')
SELECTED_SRT = {
    "srt_conns_ms_rtt": "rtt_ms",
    "srt_conns_mbps_receive_rate": "receive_mbps",
    "srt_conns_packets_received_loss": "packets_lost",
    "srt_conns_packets_received_retrans": "packets_retransmitted",
    "srt_conns_bytes_received": "bytes_received",
    "srt_conns_ms_receive_tsb_pd_delay": "latency_ms",
}


def _read_cpu() -> tuple[int, int]:
    fields = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()[1:]
    values = [int(value) for value in fields]
    total = sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return total, idle


def _read_memory() -> tuple[int, int]:
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        key, raw = line.split(":", 1)
        values[key] = int(raw.strip().split()[0]) * 1024
    total = values["MemTotal"]
    available = values.get("MemAvailable", values.get("MemFree", 0))
    return total, total - available


def _labels(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    position = 0
    while position < len(value):
        match = LABEL_RE.match(value, position)
        if not match:
            return {}
        result[match.group(1)] = bytes(match.group(2), "utf-8").decode("unicode_escape")
        position = match.end()
    return result


def parse_srt_metrics(raw: str) -> list[dict[str, Any]]:
    sessions: dict[str, dict[str, Any]] = {}
    for line in raw.splitlines():
        match = SRT_METRIC_RE.match(line)
        if not match or match.group(1) not in SELECTED_SRT:
            continue
        labels = _labels(match.group(2))
        connection_id = labels.get("id")
        path = labels.get("path", "")
        if not connection_id or not path.startswith("live/"):
            continue
        item = sessions.setdefault(
            connection_id,
            {
                "connection_id": connection_id,
                "stream_id": path.removeprefix("live/"),
                "state": labels.get("state"),
            },
        )
        number = float(match.group(3))
        item[SELECTED_SRT[match.group(1)]] = int(number) if number.is_integer() else number
    return sorted(sessions.values(), key=lambda item: str(item["connection_id"]))


class TelemetryCollector:
    def __init__(self, relay_id: str, mediamtx_metrics_url: str):
        self.relay_id = relay_id
        self.mediamtx_metrics_url = mediamtx_metrics_url
        self._cpu_previous: tuple[int, int] | None = None
        self._lock = threading.RLock()
        self._snapshot: dict[str, Any] = {}
        self._raw_mediamtx_metrics = ""
        self._last_sample_monotonic: float | None = None
        self._last_kernel_rx = 0
        self._last_kernel_tx = 0
        self._path_byte_samples: dict[str, tuple[int, float]] = {}

    def fetch_mediamtx_metrics(self) -> str:
        request = urllib.request.Request(self.mediamtx_metrics_url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
        except (urllib.error.URLError, TimeoutError):
            return ""
        if len(raw) > 8 * 1024 * 1024:
            return ""
        return raw.decode("utf-8", errors="replace")

    def collect(
        self,
        *,
        state: DesiredState | None,
        path_items: dict[str, dict[str, Any]],
        state_status: dict[str, Any],
        worker_statuses: list[dict[str, object]],
        traffic: dict[str, Any],
        mediamtx_healthy: bool,
        applied_limits: dict[str, Any],
    ) -> dict[str, Any]:
        current_cpu = _read_cpu()
        cpu_percent = 0.0
        if self._cpu_previous:
            total_delta = current_cpu[0] - self._cpu_previous[0]
            idle_delta = current_cpu[1] - self._cpu_previous[1]
            if total_delta > 0:
                cpu_percent = max(0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta))
        self._cpu_previous = current_cpu
        memory_total, memory_used = _read_memory()
        raw_metrics = self.fetch_mediamtx_metrics()
        now = datetime.now(timezone.utc)
        monotonic_now = time.monotonic()
        previous_time = self._last_sample_monotonic
        elapsed = monotonic_now - previous_time if previous_time is not None else 0.0
        kernel_rx = int(traffic.get("kernel_rx_bytes", 0))
        kernel_tx = int(traffic.get("kernel_tx_bytes", 0))
        rx_bps = int(max(0, kernel_rx - self._last_kernel_rx) * 8 / elapsed) if elapsed > 0 else 0
        tx_bps = int(max(0, kernel_tx - self._last_kernel_tx) * 8 / elapsed) if elapsed > 0 else 0
        self._last_sample_monotonic = monotonic_now
        self._last_kernel_rx = kernel_rx
        self._last_kernel_tx = kernel_tx

        worker_map = {
            (str(item.get("stream_id")), str(item.get("destination_id"))): item
            for item in worker_statuses
        }
        srt_items = parse_srt_metrics(raw_metrics)
        srt_by_stream = {str(item["stream_id"]): item for item in srt_items}
        stream_telemetry: list[dict[str, Any]] = []
        for stream in state.streams if state else ():
            path_item = path_items.get(stream.path, {})
            online = bool(path_item.get("online", path_item.get("ready", False)))
            source = path_item.get("source") if isinstance(path_item.get("source"), dict) else {}
            source_type = source.get("type")
            transport = "srt" if source_type == "srtConn" else "rtmp" if source_type in {"rtmpConn", "rtmpsConn"} else None
            inbound_bytes = int(path_item.get("inboundBytes", path_item.get("bytesReceived", 0)) or 0)
            previous = self._path_byte_samples.get(stream.stream_id)
            bitrate_bps = 0
            if previous and monotonic_now > previous[1]:
                bitrate_bps = int(max(0, inbound_bytes - previous[0]) * 8 / (monotonic_now - previous[1]))
            self._path_byte_samples[stream.stream_id] = (inbound_bytes, monotonic_now)
            duration_seconds = None
            online_time = path_item.get("onlineTime", path_item.get("readyTime"))
            if online and isinstance(online_time, str):
                try:
                    started = datetime.fromisoformat(online_time.replace("Z", "+00:00"))
                    duration_seconds = max(0, int((now - started.astimezone(timezone.utc)).total_seconds()))
                except ValueError:
                    duration_seconds = None
            srt = srt_by_stream.get(stream.stream_id, {})
            destinations: list[dict[str, Any]] = []
            for destination in stream.destinations:
                worker = worker_map.get((stream.stream_id, destination.destination_id))
                internal = str(worker.get("state")) if worker else "missing"
                status = {
                    "online": "online",
                    "backoff": "backoff",
                    "failed": "failed",
                    "stalled": "failed",
                    "stopped": "stopped",
                    "created": "starting",
                    "starting": "starting",
                    "running": "starting",
                }.get(internal, "stopped" if (not destination.enabled or not online) else "unknown")
                error_code = str(worker.get("error_code")) if worker and worker.get("error_code") else None
                destinations.append(
                    {
                        "destination_id": destination.destination_id,
                        "platform": destination.platform,
                        "status": status,
                        "reconnect_count": int(worker.get("restart_count", 0)) if worker else 0,
                        "error_code": error_code,
                        "error_summary": error_code,
                    }
                )
            stream_telemetry.append(
                {
                    "stream_id": stream.stream_id,
                    "incoming_online": online,
                    "transport": transport,
                    "bitrate_bps": bitrate_bps,
                    "srt_rtt_ms": srt.get("rtt_ms"),
                    "srt_packet_loss": srt.get("packets_lost"),
                    "srt_retrans_packets": srt.get("packets_retransmitted"),
                    "srt_latency_ms": srt.get("latency_ms"),
                    "duration_seconds": duration_seconds,
                    "diagnostic": {},
                    "destinations": destinations,
                }
            )

        active_streams = sum(
            1
            for name, item in path_items.items()
            if name.startswith("live/") and item.get("online", item.get("ready", False)) is True
        )
        memory_percent = round(100.0 * memory_used / memory_total, 2) if memory_total else 0.0
        snapshot = {
            "generation": state.generation if state else 0,
            "supported_state_schemas": [1, 2],
            "mediamtx_healthy": mediamtx_healthy,
            "cpu_percent": round(cpu_percent, 2),
            "memory_percent": memory_percent,
            "rx_bps": rx_bps,
            "tx_bps": tx_bps,
            "srt_sessions": sum(1 for item in stream_telemetry if item["incoming_online"] and item["transport"] == "srt"),
            "rtmp_sessions": sum(1 for item in stream_telemetry if item["incoming_online"] and item["transport"] == "rtmp"),
            "snapshot_complete": bool(mediamtx_healthy and state_status.get("available", False)),
            "streams": stream_telemetry,
            "monthly_tx_bytes": int(traffic.get("tx_bytes", 0)),
            "quota_bytes": int(traffic.get("quota_bytes", 0)),
            "projected_monthly_tx_bytes": int(traffic.get("projected_tx_bytes", 0)),
            "quota_tracking_started_at": traffic.get("tracking_started_at"),
            "quota_tracking_partial": True,
            "applied_limit_revision": applied_limits.get("revision"),
            "applied_max_server_connections": int(
                applied_limits.get("max_server_connections", 0)
            ),
            "applied_monthly_traffic_quota_bytes": int(
                applied_limits.get("monthly_traffic_quota_bytes", 0)
            ),
            # Local-only fields are stripped before posting by manager_payload().
            "_local": {
                "online": mediamtx_healthy and state_status.get("available", False),
                "state_stale": bool(state_status.get("stale", True)),
                "active_streams": active_streams,
                "memory_used_bytes": memory_used,
                "memory_total_bytes": memory_total,
                "workers": worker_statuses,
                "traffic": traffic,
            },
        }
        with self._lock:
            self._snapshot = snapshot
            self._raw_mediamtx_metrics = raw_metrics
        return snapshot

    def manager_payload(self) -> dict[str, Any]:
        value = self.snapshot()
        value.pop("_local", None)
        return value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._snapshot))

    def prometheus(self) -> str:
        with self._lock:
            snapshot = self._snapshot.copy()
            raw = self._raw_mediamtx_metrics
        local = snapshot.get("_local", {})
        traffic = local.get("traffic", {}) if isinstance(local, dict) else {}
        workers = local.get("workers", []) if isinstance(local, dict) else []
        lines = [
            "# HELP relay_agent_up Whether the local agent has usable state and MediaMTX.",
            "# TYPE relay_agent_up gauge",
            f"relay_agent_up {1 if local.get('online') else 0}",
            "# TYPE relay_agent_state_stale gauge",
            f"relay_agent_state_stale {1 if local.get('state_stale', True) else 0}",
            "# TYPE relay_agent_active_streams gauge",
            f"relay_agent_active_streams {int(local.get('active_streams', 0))}",
            "# TYPE relay_agent_workers gauge",
            f"relay_agent_workers {len(workers)}",
            "# TYPE relay_agent_cpu_percent gauge",
            f"relay_agent_cpu_percent {float(snapshot.get('cpu_percent', 0.0))}",
            "# TYPE relay_agent_memory_used_bytes gauge",
            f"relay_agent_memory_used_bytes {int(local.get('memory_used_bytes', 0))}",
            "# TYPE relay_agent_monthly_egress_bytes gauge",
            f"relay_agent_monthly_egress_bytes {int(traffic.get('tx_bytes', 0))}",
            "# TYPE relay_agent_traffic_quota_bytes gauge",
            f"relay_agent_traffic_quota_bytes {int(traffic.get('quota_bytes', 0))}",
            "# TYPE relay_agent_projected_monthly_egress_bytes gauge",
            f"relay_agent_projected_monthly_egress_bytes {int(traffic.get('projected_tx_bytes', 0))}",
        ]
        # MediaMTX metrics are already Prometheus text with distinct names.
        if raw:
            lines.extend(("", raw.rstrip()))
        return "\n".join(lines) + "\n"
