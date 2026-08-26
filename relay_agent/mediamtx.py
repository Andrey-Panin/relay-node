"""Minimal MediaMTX v1.20.x Control API client and managed-path reconciler."""

from __future__ import annotations

import json
import ipaddress
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .models import DesiredState, STREAM_ID_RE
from .signing import derive_srt_passphrase
from .state_store import _atomic_secret_json


LOG = logging.getLogger("relay_agent.mediamtx")


class MediaMTXError(RuntimeError):
    pass


class MediaMTXClient:
    def __init__(self, api_url: str, timeout: float = 5.0):
        parsed = urllib.parse.urlsplit(api_url)
        try:
            loopback = parsed.hostname == "localhost" or (
                parsed.hostname is not None and ipaddress.ip_address(parsed.hostname).is_loopback
            )
        except ValueError:
            loopback = False
        if parsed.scheme != "http" or not loopback or parsed.username or parsed.password:
            raise MediaMTXError("MediaMTX Control API must be plain HTTP on loopback")
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @staticmethod
    def _escaped_path(name: str) -> str:
        # MediaMTX routes named-path APIs through a wildcard segment. Its API
        # requires the slash in "live/<id>" to remain literal; %2F is rejected.
        if not name.startswith("live/") or not STREAM_ID_RE.fullmatch(name.removeprefix("live/")):
            raise MediaMTXError("refusing unsafe managed path name")
        return urllib.parse.quote(name, safe="/")

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        encoded = None
        headers = {"Accept": "application/json"}
        if body is not None:
            encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.api_url + path,
            data=encoded,
            method=method,
            headers=headers,
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise MediaMTXError(f"MediaMTX API {method} failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise MediaMTXError("MediaMTX API is unavailable") from exc
        if len(raw) > 2 * 1024 * 1024:
            raise MediaMTXError("MediaMTX API response too large")
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MediaMTXError("MediaMTX API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise MediaMTXError("MediaMTX API response is not an object")
        return value

    def list_active_paths(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        page = 0
        while page < 100:
            response = self.request("GET", f"/v3/paths/list?page={page}&itemsPerPage=100")
            items = response.get("items", [])
            if not isinstance(items, list):
                raise MediaMTXError("MediaMTX path list has invalid items")
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    result[item["name"]] = item
            page_count = response.get("pageCount", 1)
            if not isinstance(page_count, int) or page + 1 >= page_count:
                break
            page += 1
        return result

    def get_path_config(self, name: str) -> dict[str, Any] | None:
        escaped = self._escaped_path(name)
        try:
            return self.request("GET", f"/v3/config/paths/get/{escaped}")
        except MediaMTXError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def add_path(self, name: str, values: dict[str, Any]) -> None:
        escaped = self._escaped_path(name)
        self.request("POST", f"/v3/config/paths/add/{escaped}", values)

    def patch_path(self, name: str, values: dict[str, Any]) -> None:
        escaped = self._escaped_path(name)
        self.request("PATCH", f"/v3/config/paths/patch/{escaped}", values)

    def delete_path(self, name: str) -> None:
        escaped = self._escaped_path(name)
        self.request("DELETE", f"/v3/config/paths/delete/{escaped}")


class PathReconciler:
    """Own only path names previously recorded in managed-paths.json."""

    def __init__(self, client: MediaMTXClient, signing_key: bytes, managed_path: Path):
        self.client = client
        self.signing_key = signing_key
        self.managed_path = managed_path
        self._lock = threading.Lock()
        self._managed = self._load_managed()

    def _load_managed(self) -> set[str]:
        try:
            raw = json.loads(self.managed_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, json.JSONDecodeError):
            LOG.warning("managed path registry is unreadable; refusing to claim existing paths")
            return set()
        if not isinstance(raw, dict) or raw.get("schema_version") != 1 or not isinstance(raw.get("paths"), list):
            LOG.warning("managed path registry is invalid; refusing to claim existing paths")
            return set()
        return {item for item in raw["paths"] if isinstance(item, str)}

    def _save_managed(self) -> None:
        _atomic_secret_json(
            self.managed_path,
            {"schema_version": 1, "paths": sorted(self._managed)},
        )

    def reconcile(self, state: DesiredState) -> None:
        desired_streams = {stream.path: stream for stream in state.streams if stream.enabled}
        with self._lock:
            for path, stream in desired_streams.items():
                values = {
                    "source": "publisher",
                    "overridePublisher": False,
                    "maxReaders": 8,
                    "record": False,
                    "srtPublishPassphrase": derive_srt_passphrase(self.signing_key, stream.stream_id),
                }
                existing = self.client.get_path_config(path)
                if existing is None:
                    self.client.add_path(path, values)
                    self._managed.add(path)
                elif path in self._managed:
                    # API mutations reload MediaMTX configuration. Never patch
                    # an unchanged path on every manager poll.
                    if any(existing.get(key) != expected for key, expected in values.items()):
                        self.client.patch_path(path, values)
                else:
                    # Existing production configuration is never silently taken over.
                    raise MediaMTXError(f"refusing to overwrite unmanaged MediaMTX path {path!r}")

            for stale in sorted(self._managed - set(desired_streams)):
                if self.client.get_path_config(stale) is not None:
                    self.client.delete_path(stale)
                self._managed.discard(stale)
            self._save_managed()

