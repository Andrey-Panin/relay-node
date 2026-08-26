"""Persistent UTC-month network accounting for a provider traffic quota."""

from __future__ import annotations

import calendar
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .state_store import _atomic_secret_json


LOG = logging.getLogger("relay_agent.traffic")


def read_boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def read_network_bytes() -> tuple[int, int]:
    """Return RX/TX totals for non-loopback interfaces.

    This intentionally over-counts management traffic. It is safer to warn early
    than to under-count a provider quota. A provider portal remains authoritative.
    """
    rx_total = 0
    tx_total = 0
    for line in Path("/proc/net/dev").read_text(encoding="ascii").splitlines()[2:]:
        if ":" not in line:
            continue
        interface, values = line.split(":", 1)
        if interface.strip() == "lo":
            continue
        fields = values.split()
        if len(fields) >= 9:
            rx_total += int(fields[0])
            tx_total += int(fields[8])
    return rx_total, tx_total


class TrafficAccountant:
    def __init__(
        self,
        state_path: Path,
        quota_bytes: int,
        network_reader: Callable[[], tuple[int, int]] = read_network_bytes,
        boot_reader: Callable[[], str] = read_boot_id,
    ):
        self.state_path = state_path
        self.quota_bytes = quota_bytes
        self.network_reader = network_reader
        self.boot_reader = boot_reader
        self._state = self._load()
        self._last_warning_level = 0

    def _load(self) -> dict:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) and value.get("schema_version") == 1 else {}

    def sample(self, now: datetime | None = None) -> dict[str, int | float | str]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        month = now.strftime("%Y-%m")
        boot_id = self.boot_reader()
        rx_bytes, tx_bytes = self.network_reader()
        if self._state.get("month") != month:
            first_sample_ever = not self._state
            initial_rx = rx_bytes if first_sample_ever else 0
            initial_tx = tx_bytes if first_sample_ever else 0
            self._state = {
                "schema_version": 1,
                "month": month,
                "boot_id": boot_id,
                "last_rx_bytes": rx_bytes,
                "last_tx_bytes": tx_bytes,
                # Include current boot counters on a new VPS. This safely
                # over-counts non-relay traffic and is explicitly a local estimate.
                "rx_bytes": initial_rx,
                "tx_bytes": initial_tx,
                "projection_baseline_tx_bytes": initial_tx,
                "tracking_started_at": now.isoformat(),
            }
            self._last_warning_level = 0
        elif self._state.get("boot_id") != boot_id:
            # Previous deltas were saved before reboot. Counters restarted; use
            # the current values only as a new baseline to avoid double counting.
            self._state["boot_id"] = boot_id
            self._state["last_rx_bytes"] = rx_bytes
            self._state["last_tx_bytes"] = tx_bytes
            self._state["rx_bytes"] = int(self._state.get("rx_bytes", 0)) + rx_bytes
            self._state["tx_bytes"] = int(self._state.get("tx_bytes", 0)) + tx_bytes
        else:
            previous_rx = int(self._state.get("last_rx_bytes", rx_bytes))
            previous_tx = int(self._state.get("last_tx_bytes", tx_bytes))
            self._state["rx_bytes"] = int(self._state.get("rx_bytes", 0)) + max(0, rx_bytes - previous_rx)
            self._state["tx_bytes"] = int(self._state.get("tx_bytes", 0)) + max(0, tx_bytes - previous_tx)
            self._state["last_rx_bytes"] = rx_bytes
            self._state["last_tx_bytes"] = tx_bytes
        self._state["updated_at"] = now.isoformat()
        _atomic_secret_json(self.state_path, self._state)

        used = int(self._state.get("tx_bytes", 0))
        ratio = used / self.quota_bytes
        level = 100 if ratio >= 1 else 95 if ratio >= 0.95 else 85 if ratio >= 0.85 else 70 if ratio >= 0.70 else 0
        if level > self._last_warning_level:
            LOG.warning(
                "monthly egress quota threshold crossed level=%s used_bytes=%s quota_bytes=%s",
                level,
                used,
                self.quota_bytes,
            )
            self._last_warning_level = level

        days_in_month = calendar.monthrange(now.year, now.month)[1]
        try:
            tracking_started = datetime.fromisoformat(str(self._state["tracking_started_at"]))
            if tracking_started.tzinfo is None:
                tracking_started = tracking_started.replace(tzinfo=timezone.utc)
            tracked_seconds = max(0.0, (now - tracking_started.astimezone(timezone.utc)).total_seconds())
        except (KeyError, ValueError):
            tracked_seconds = 0.0
        remaining_seconds = (
            days_in_month * 86400
            - ((now.day - 1) * 86400 + now.hour * 3600 + now.minute * 60 + now.second)
        )
        projection_baseline = int(self._state.get("projection_baseline_tx_bytes", 0))
        measured_for_rate = max(0, used - projection_baseline)
        projected = (
            int(used + (measured_for_rate / tracked_seconds) * remaining_seconds)
            if tracked_seconds >= 300
            else used
        )
        return {
            "month": month,
            "rx_bytes": int(self._state.get("rx_bytes", 0)),
            "tx_bytes": used,
            "quota_bytes": self.quota_bytes,
            "quota_ratio": ratio,
            "warning_level": level,
            "projected_tx_bytes": projected,
            "tracking_started_at": str(self._state.get("tracking_started_at", now.isoformat())),
            "kernel_rx_bytes": rx_bytes,
            "kernel_tx_bytes": tx_bytes,
        }

