#!/usr/bin/env python3
"""One-time relay enrollment and authenticated activation polling.

Secrets are accepted through getpass or root-only files only. They are never
placed in a URL, command-line argument, environment variable, or log message.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import secrets
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_RESPONSE_BYTES = 1024 * 1024
CLAIM_RE = re.compile(r"^[A-Za-z0-9_-]{22,128}$")
PAIRING_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{12,256}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{24,512}$")
SIGNING_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{43,512}={0,2}$")


class BootstrapError(RuntimeError):
    """Safe, operator-facing bootstrap failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_ORIGINAL_GETADDRINFO = socket.getaddrinfo


def _force_ipv4_networking() -> None:
    """Force manager traffic over IPv4 to match public-host enrollment binding."""

    def ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        return _ORIGINAL_GETADDRINFO(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = ipv4_getaddrinfo


@dataclass(frozen=True, slots=True)
class ManagerConfig:
    manager_url: str
    enrollment_path: str
    activation_timeout_seconds: int

    @classmethod
    def load(cls, path: Path) -> "ManagerConfig":
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BootstrapError("manager bootstrap configuration is unreadable") from exc
        expected = {
            "schema_version",
            "manager_url",
            "enrollment_path",
            "activation_timeout_seconds",
        }
        if not isinstance(value, dict) or set(value) != expected or value.get("schema_version") != 1:
            raise BootstrapError("manager bootstrap configuration has an unsupported schema")
        manager_url = _validate_https_url(value.get("manager_url"), "manager_url").rstrip("/")
        if (urllib.parse.urlsplit(manager_url).hostname or "").endswith(".invalid"):
            raise BootstrapError("release bootstrap manager URL is still a placeholder")
        enrollment_path = _validate_api_path(value.get("enrollment_path"), "enrollment_path")
        timeout = value.get("activation_timeout_seconds")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 30 <= timeout <= 1800:
            raise BootstrapError("activation_timeout_seconds must be between 30 and 1800")
        return cls(manager_url, enrollment_path, timeout)


def _validate_https_url(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise BootstrapError(f"{field} is invalid")
    if any(ord(character) < 0x21 or ord(character) == 0x7F for character in value):
        raise BootstrapError(f"{field} contains unsupported characters")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise BootstrapError(f"{field} must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BootstrapError(f"{field} must not contain credentials, query, or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise BootstrapError(f"{field} contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise BootstrapError(f"{field} contains an invalid port")
    return value


def _validate_api_path(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/api/")
        or len(value) > 512
        or "?" in value
        or "#" in value
        or "\\" in value
        or "//" in value
        or not re.fullmatch(r"/[A-Za-z0-9._~/-]+", value)
    ):
        raise BootstrapError(f"{field} is not a safe relative API path")
    return value


def _same_origin(url: str, manager_url: str, field: str) -> str:
    validated = _validate_https_url(url, field)
    manager = urllib.parse.urlsplit(manager_url)
    candidate = urllib.parse.urlsplit(validated)
    if (candidate.scheme, candidate.hostname, candidate.port) != (
        manager.scheme,
        manager.hostname,
        manager.port,
    ):
        raise BootstrapError(f"{field} must use the pinned manager origin")
    if not candidate.path.startswith("/api/"):
        raise BootstrapError(f"{field} must use an API path")
    return validated


def _atomic_write(path: Path, data: bytes, mode: int, *, parent_mode: int = 0o700) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=parent_mode)
    os.chmod(path.parent, parent_mode)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory = None
        if directory is not None:
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path, description: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BootstrapError(f"{description} is unreadable") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BootstrapError(f"{description} is too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"{description} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise BootstrapError(f"{description} must be a JSON object")
    return value


def _load_or_create_claim(state_dir: Path) -> str:
    path = state_dir / "claim_id"
    try:
        claim = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        claim = secrets.token_urlsafe(24)
        _atomic_write(path, (claim + "\n").encode("ascii"), 0o600)
    except OSError as exc:
        raise BootstrapError("local enrollment claim ID is unreadable") from exc
    if not CLAIM_RE.fullmatch(claim):
        raise BootstrapError("local enrollment claim ID is invalid; refusing a new identity")
    return claim


def _decode_signing_key(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise BootstrapError("manager returned an invalid state signing key") from exc


def _validate_bundle(value: object, manager: ManagerConfig) -> dict[str, Any]:
    expected = {
        "schema_version",
        "relay_id",
        "agent_token",
        "state_signing_key",
        "manager_url",
        "relay_state_url",
        "telemetry_url",
        "publish_authorization_url",
        "bootstrap_status_url",
        "public_host",
        "srt_port",
        "rtmp_port",
        "max_active_models",
        "quota_bytes",
        "desired_state_schema",
    }
    if not isinstance(value, dict) or set(value) != expected or value.get("schema_version") != 1:
        raise BootstrapError("manager returned an unsupported enrollment bundle")
    relay_id = value.get("relay_id")
    try:
        parsed_id = uuid.UUID(str(relay_id))
    except (ValueError, AttributeError) as exc:
        raise BootstrapError("manager returned an invalid relay ID") from exc
    if str(parsed_id) != relay_id:
        raise BootstrapError("manager returned a non-canonical relay ID")
    token = value.get("agent_token")
    signing_key = value.get("state_signing_key")
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise BootstrapError("manager returned an invalid agent token")
    if not isinstance(signing_key, str) or not SIGNING_KEY_RE.fullmatch(signing_key):
        raise BootstrapError("manager returned an invalid state signing key")
    if len(_decode_signing_key(signing_key)) < 32:
        raise BootstrapError("manager state signing key is too short")
    response_manager_url = value.get("manager_url")
    if not isinstance(response_manager_url, str) or response_manager_url.rstrip("/") != manager.manager_url:
        raise BootstrapError("manager enrollment bundle changed the pinned manager URL")
    for field in (
        "relay_state_url",
        "telemetry_url",
        "publish_authorization_url",
        "bootstrap_status_url",
    ):
        _same_origin(value.get(field), manager.manager_url, field)
    public_host = value.get("public_host")
    if (
        not isinstance(public_host, str)
        or not public_host
        or len(public_host) > 253
        or not re.fullmatch(r"[A-Za-z0-9:._-]+", public_host)
    ):
        raise BootstrapError("manager returned an invalid public host")
    if value.get("srt_port") != 8890 or value.get("rtmp_port") != 1935:
        raise BootstrapError("manager ports do not match the fixed relay release ports")
    max_models = value.get("max_active_models")
    quota = value.get("quota_bytes")
    if not isinstance(max_models, int) or isinstance(max_models, bool) or not 1 <= max_models <= 100:
        raise BootstrapError("manager returned an invalid model capacity")
    if not isinstance(quota, int) or isinstance(quota, bool) or not 1_000_000_000 <= quota <= 10**18:
        raise BootstrapError("manager returned an invalid traffic quota")
    if value.get("desired_state_schema") != 1:
        raise BootstrapError("manager and agent desired-state schemas are incompatible")
    return value


def _ssl_context(ca_file: Path) -> ssl.SSLContext:
    try:
        return ssl.create_default_context(cafile=str(ca_file))
    except (OSError, ssl.SSLError) as exc:
        raise BootstrapError(
            "bundled manager CA is invalid or still a release placeholder"
        ) from exc


def _opener(context: ssl.SSLContext) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=context),
    )


def _request_json(
    opener: urllib.request.OpenerDirector,
    request: urllib.request.Request,
    *,
    expected_statuses: set[int],
) -> dict[str, Any]:
    try:
        with opener.open(request, timeout=15) as response:
            if response.status not in expected_statuses:
                raise BootstrapError(f"manager returned HTTP {response.status}")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise BootstrapError(f"manager returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BootstrapError("manager request failed") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BootstrapError("manager response is too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("manager returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise BootstrapError("manager response must be a JSON object")
    return value


def _identity_paths(state_dir: Path) -> tuple[Path, Path, Path]:
    return (
        state_dir / "identity.json",
        state_dir / "credentials" / "manager_token",
        state_dir / "credentials" / "state_signing_key",
    )


def load_identity(state_dir: Path, manager: ManagerConfig) -> dict[str, Any] | None:
    identity_path, token_path, signing_path = _identity_paths(state_dir)
    if not identity_path.exists():
        return None
    identity = _read_json(identity_path, "local relay identity")
    expected = {
        "schema_version",
        "relay_id",
        "manager_url",
        "relay_state_url",
        "telemetry_url",
        "publish_authorization_url",
        "bootstrap_status_url",
        "public_host",
        "srt_port",
        "rtmp_port",
        "max_active_models",
        "quota_bytes",
        "desired_state_schema",
    }
    if set(identity) != expected or identity.get("schema_version") != 1:
        raise BootstrapError("local relay identity has an unsupported schema")
    if identity.get("manager_url") != manager.manager_url:
        raise BootstrapError("local identity belongs to a different manager")
    try:
        token = token_path.read_text(encoding="ascii").strip()
        signing_key = signing_path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise BootstrapError("local identity is incomplete; retry with the original pairing code") from exc
    probe = dict(identity)
    probe["agent_token"] = token
    probe["state_signing_key"] = signing_key
    return _validate_bundle(probe, manager)


def enroll(
    manager: ManagerConfig,
    ca_file: Path,
    state_dir: Path,
    version: str,
) -> tuple[dict[str, Any], bool]:
    existing = load_identity(state_dir, manager)
    if existing is not None:
        return existing, True
    claim_id = _load_or_create_claim(state_dir)
    pairing_code = getpass.getpass("Одноразовый код из админки: ").strip()
    if not PAIRING_CODE_RE.fullmatch(pairing_code):
        raise BootstrapError("pairing code has an invalid format")
    body = json.dumps(
        {
            "claim_id": claim_id,
            "agent_version": version,
            "supported_state_schemas": [1],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        manager.manager_url + manager.enrollment_path,
        data=body,
        headers={
            "Authorization": f"Relay-Enrollment {pairing_code}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "User-Agent": f"relay-node-installer/{version}",
        },
        method="POST",
    )
    try:
        _force_ipv4_networking()
        value = _request_json(_opener(_ssl_context(ca_file)), request, expected_statuses={200})
    except BootstrapError as exc:
        if str(exc) == "manager returned HTTP 403":
            raise BootstrapError(
                "enrollment rejected: pairing code or configured public IPv4 does not match this server"
            ) from exc
        raise
    finally:
        pairing_code = ""
    bundle = _validate_bundle(value, manager)
    identity_path, token_path, signing_path = _identity_paths(state_dir)
    _atomic_write(token_path, (bundle["agent_token"] + "\n").encode("ascii"), 0o400)
    _atomic_write(signing_path, (bundle["state_signing_key"] + "\n").encode("ascii"), 0o400)
    public_identity = {
        key: value
        for key, value in bundle.items()
        if key not in {"agent_token", "state_signing_key"}
    }
    _atomic_write(
        identity_path,
        (json.dumps(public_identity, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        0o600,
    )
    return bundle, False


def wait_until_active(
    manager: ManagerConfig,
    ca_file: Path,
    state_dir: Path,
) -> dict[str, Any]:
    bundle = load_identity(state_dir, manager)
    if bundle is None:
        raise BootstrapError("relay is not enrolled")
    token_path = _identity_paths(state_dir)[1]
    token = token_path.read_text(encoding="ascii").strip()
    status_url = bundle["bootstrap_status_url"]
    opener = _opener(_ssl_context(ca_file))
    _force_ipv4_networking()
    deadline = time.monotonic() + manager.activation_timeout_seconds
    last_category = "manager has not reported a healthy relay yet"
    while time.monotonic() < deadline:
        request = urllib.request.Request(
            status_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Cache-Control": "no-store",
                "User-Agent": "relay-node-installer/status",
            },
            method="GET",
        )
        try:
            value = _request_json(opener, request, expected_statuses={200})
            expected = {
                "relay_id",
                "lifecycle_status",
                "online",
                "mediamtx_healthy",
                "desired_generation",
                "reported_generation",
                "healthy_streak",
                "healthy_streak_required",
                "pool_eligible",
            }
            if set(value) != expected or value.get("relay_id") != bundle["relay_id"]:
                raise BootstrapError("manager returned an invalid bootstrap status")
            if (
                value.get("lifecycle_status") == "active"
                and value.get("online") is True
                and value.get("mediamtx_healthy") is True
                and value.get("pool_eligible") is True
            ):
                return value
            last_category = f"manager lifecycle is {value.get('lifecycle_status', 'unknown')}"
        except BootstrapError as exc:
            last_category = str(exc)
        time.sleep(5)
    raise BootstrapError(f"relay did not enter the pool before timeout: {last_category}")


def _read_version(path: Path) -> str:
    try:
        version = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise BootstrapError("release VERSION is unreadable") from exc
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise BootstrapError("release VERSION is invalid")
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("enroll", "status"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--ca-file", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/relay-bootstrap"))
    parser.add_argument("--version-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise BootstrapError("run bootstrap client as root")
        manager = ManagerConfig.load(args.config)
        version = _read_version(args.version_file)
        if args.command == "enroll":
            bundle, reused = enroll(manager, args.ca_file, args.state_dir, version)
            print(f"ENROLLED relay_id={bundle['relay_id']} reused={'yes' if reused else 'no'}")
        else:
            status = wait_until_active(manager, args.ca_file, args.state_dir)
            print(
                "POOL_ACTIVE "
                f"relay_id={status['relay_id']} healthy_streak={status['healthy_streak']}"
            )
    except BootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
