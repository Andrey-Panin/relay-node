"""One supervised FFmpeg process per (stream, platform)."""

from __future__ import annotations

import hashlib
import logging
import os
import signal
import subprocess
import threading
import time
import re
from urllib.parse import quote
from dataclasses import dataclass
from typing import Callable

from .models import Destination, DesiredState, build_destination_url


LOG = logging.getLogger("relay_agent.worker")
BACKOFF_SECONDS = (2, 5, 10, 20, 30)


def _configuration_fingerprint(destination: Destination) -> str:
    raw = "\0".join(
        (destination.destination_id, destination.platform, destination.ingest_url, destination.stream_key)
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


RTMP_URL_RE = re.compile(r"rtmps?://[^\s'\"]+", re.IGNORECASE)


def _redact(line: str, destination: Destination, target: str) -> str:
    # FFmpeg sometimes repeats the output URL in diagnostics. Never let the
    # destination key or the assembled URL reach journald.
    value = line.replace(target, f"{destination.ingest_url}/[REDACTED]")
    value = value.replace(destination.stream_key, "[REDACTED]")
    value = value.replace(quote(destination.stream_key, safe=""), "[REDACTED]")
    value = RTMP_URL_RE.sub("[RTMP_URL_REDACTED]", value)
    return value[:2000]


def _classify_error(line: str) -> str:
    value = line.lower()
    categories = (
        ("reader is too slow", "reader_too_slow"),
        ("i/o timeout", "io_timeout"),
        ("timed out", "io_timeout"),
        ("broken pipe", "broken_pipe"),
        ("connection reset", "connection_reset"),
        ("connection refused", "connection_refused"),
        ("name or service not known", "dns_failure"),
        ("temporary failure in name resolution", "dns_failure"),
        ("unauthorized", "auth_or_rejected"),
        ("forbidden", "auth_or_rejected"),
        ("server error", "platform_rejected"),
        ("tls", "tls_error"),
    )
    for needle, code in categories:
        if needle in value:
            return code
    return "ffmpeg_error"


@dataclass(frozen=True, slots=True)
class WorkerKey:
    stream_id: str
    platform: str


class DestinationWorker:
    def __init__(
        self,
        key: WorkerKey,
        destination: Destination,
        input_url: str,
        ffmpeg_path: str,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
    ):
        self.key = key
        self.destination = destination
        self.input_url = input_url
        self.ffmpeg_path = ffmpeg_path
        self.fingerprint = _configuration_fingerprint(destination)
        self._popen_factory = popen_factory
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"worker-{key.stream_id[:8]}-{key.platform}",
            daemon=True,
        )
        self._process: subprocess.Popen | None = None
        self._process_lock = threading.Lock()
        self._started_at: float | None = None
        self._last_exit_code: int | None = None
        self._restart_count = 0
        self._state = "created"
        self._last_progress_monotonic: float | None = None
        self._output_bytes = 0
        self._media_time_us = 0
        self._last_error_code: str | None = None

    def command(self, target: str) -> list[str]:
        return [
            self.ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "warning",
            "-timeout",
            "15000000",
            "-rtsp_transport",
            "tcp",
            "-fflags",
            "+genpts+discardcorrupt",
            "-i",
            self.input_url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-flvflags",
            "no_duration_filesize",
            "-f",
            "flv",
            "-stats_period",
            "2",
            "-progress",
            "pipe:1",
            target,
        ]

    def start(self) -> None:
        self._thread.start()

    def _consume_stderr(self, process: subprocess.Popen, target: str) -> None:
        if process.stderr is None:
            return
        for raw in iter(process.stderr.readline, b""):
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                code = _classify_error(line)
                self._last_error_code = code
                LOG.warning(
                    "ffmpeg diagnostic stream=%s platform=%s code=%s detail=%s",
                    self.key.stream_id,
                    self.key.platform,
                    code,
                    _redact(line, self.destination, target),
                )

    def _consume_progress(self, process: subprocess.Popen) -> None:
        if process.stdout is None:
            return
        batch: dict[str, str] = {}
        for raw in iter(process.stdout.readline, b""):
            if not raw:
                break
            line = raw.decode("ascii", errors="replace").strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            batch[key] = value
            if key != "progress":
                continue
            try:
                total_size = int(batch.get("total_size", self._output_bytes))
            except ValueError:
                total_size = self._output_bytes
            media_raw = batch.get("out_time_us", batch.get("out_time_ms", "0"))
            try:
                media_time = int(media_raw)
            except ValueError:
                media_time = self._media_time_us
            if total_size > self._output_bytes or media_time > self._media_time_us:
                self._output_bytes = max(self._output_bytes, total_size)
                self._media_time_us = max(self._media_time_us, media_time)
                self._last_progress_monotonic = time.monotonic()
                self._last_error_code = None
                self._state = "online"
            batch = {}

    @staticmethod
    def _terminate_process(process: subprocess.Popen, force: bool = False) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass

    def _run(self) -> None:
        target = build_destination_url(self.destination.ingest_url, self.destination.stream_key)
        backoff_index = 0
        while not self._stop.is_set():
            self._state = "starting"
            self._last_progress_monotonic = None
            self._output_bytes = 0
            self._media_time_us = 0
            started_monotonic = time.monotonic()
            try:
                process = self._popen_factory(
                    self.command(target),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    close_fds=True,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                )
            except OSError:
                LOG.exception(
                    "unable to start ffmpeg stream=%s platform=%s",
                    self.key.stream_id,
                    self.key.platform,
                )
                self._state = "failed"
                if self._stop.wait(BACKOFF_SECONDS[backoff_index]):
                    break
                backoff_index = min(backoff_index + 1, len(BACKOFF_SECONDS) - 1)
                continue

            with self._process_lock:
                self._process = process
                self._started_at = time.time()
            self._state = "running"
            LOG.info("worker started stream=%s platform=%s", self.key.stream_id, self.key.platform)
            stderr_thread = threading.Thread(
                target=self._consume_stderr,
                args=(process, target),
                name=f"stderr-{self.key.stream_id[:8]}-{self.key.platform}",
                daemon=True,
            )
            stderr_thread.start()
            progress_thread = threading.Thread(
                target=self._consume_progress,
                args=(process,),
                name=f"progress-{self.key.stream_id[:8]}-{self.key.platform}",
                daemon=True,
            )
            progress_thread.start()
            while process.poll() is None and not self._stop.wait(1):
                last_progress = self._last_progress_monotonic
                age = time.monotonic() - (last_progress or started_monotonic)
                if age > 30:
                    self._last_error_code = "stalled_output"
                    self._state = "stalled"
                    LOG.warning(
                        "worker stalled stream=%s platform=%s; restarting",
                        self.key.stream_id,
                        self.key.platform,
                    )
                    self._terminate_process(process)
                    break
            if self._stop.is_set() and process.poll() is None:
                self._terminate_process(process)
            try:
                exit_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._terminate_process(process, force=True)
                exit_code = process.wait()
            stderr_thread.join(timeout=1)
            progress_thread.join(timeout=1)
            runtime = time.monotonic() - started_monotonic
            with self._process_lock:
                self._process = None
                self._last_exit_code = exit_code
            if self._stop.is_set():
                break
            self._restart_count += 1
            self._state = "backoff"
            LOG.warning(
                "worker exited stream=%s platform=%s code=%s runtime_seconds=%.1f",
                self.key.stream_id,
                self.key.platform,
                exit_code,
                runtime,
            )
            if runtime >= 60:
                backoff_index = 0
            delay = BACKOFF_SECONDS[backoff_index]
            backoff_index = min(backoff_index + 1, len(BACKOFF_SECONDS) - 1)
            if self._stop.wait(delay):
                break
        self._state = "stopped"

    def stop(self, timeout: float = 7.0) -> None:
        self._stop.set()
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            self._terminate_process(process)
            try:
                process.wait(timeout=max(1.0, timeout - 2.0))
            except subprocess.TimeoutExpired:
                self._terminate_process(process, force=True)
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)

    def status(self) -> dict[str, object]:
        with self._process_lock:
            pid = self._process.pid if self._process and self._process.poll() is None else None
        return {
            "stream_id": self.key.stream_id,
            "destination_id": self.destination.destination_id,
            "platform": self.key.platform,
            "state": self._state,
            "pid": pid,
            "started_at": self._started_at,
            "last_exit_code": self._last_exit_code,
            "error_code": self._last_error_code,
            "restart_count": self._restart_count,
            "output_bytes": self._output_bytes,
            "media_time_us": self._media_time_us,
            "last_progress_age_seconds": (
                round(time.monotonic() - self._last_progress_monotonic, 1)
                if self._last_progress_monotonic is not None
                else None
            ),
        }


class WorkerSupervisor:
    def __init__(self, ffmpeg_path: str, input_base: str, max_active_models: int):
        self.ffmpeg_path = ffmpeg_path
        self.input_base = input_base.rstrip("/")
        self.max_active_models = max_active_models
        self._workers: dict[WorkerKey, DestinationWorker] = {}
        self._lock = threading.RLock()
        self.over_capacity = False

    def reconcile(self, state: DesiredState, path_items: dict[str, dict]) -> None:
        streams = state.stream_map()
        ready_ids = sorted(
            path.removeprefix("live/")
            for path, item in path_items.items()
            if path.startswith("live/") and item.get("online", item.get("ready", False)) is True
        )
        self.over_capacity = len(ready_ids) > self.max_active_models
        admitted_ids = set(ready_ids[: self.max_active_models])
        desired: dict[WorkerKey, tuple[Destination, str]] = {}
        for stream_id in admitted_ids:
            stream = streams.get(stream_id)
            if stream is None or not stream.enabled:
                continue
            input_url = f"{self.input_base}/{stream.path}"
            for destination in stream.destinations:
                if destination.enabled:
                    key = WorkerKey(stream_id, destination.platform)
                    desired[key] = (destination, input_url)

        to_stop: list[DestinationWorker] = []
        with self._lock:
            for key, worker in list(self._workers.items()):
                target = desired.get(key)
                if target is None or worker.fingerprint != _configuration_fingerprint(target[0]):
                    LOG.info("worker stopping stream=%s platform=%s", key.stream_id, key.platform)
                    to_stop.append(worker)
                    del self._workers[key]

        # Process shutdown can take several seconds; never hold the status lock.
        for worker in to_stop:
            worker.stop()

        to_start: list[DestinationWorker] = []
        with self._lock:
            for key, (destination, input_url) in desired.items():
                if key not in self._workers:
                    worker = DestinationWorker(
                        key=key,
                        destination=destination,
                        input_url=input_url,
                        ffmpeg_path=self.ffmpeg_path,
                    )
                    self._workers[key] = worker
                    to_start.append(worker)
        for worker in to_start:
            worker.start()

    def stop_all(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.stop()

    def statuses(self) -> list[dict[str, object]]:
        with self._lock:
            return [self._workers[key].status() for key in sorted(self._workers, key=lambda x: (x.stream_id, x.platform))]

