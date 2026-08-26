from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from scripts import bootstrap_client


MANAGER_URL = "https://198.51.100.10"
RELAY_ID = "11111111-2222-4333-8444-555555555555"


def manager_file(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "manager_url": MANAGER_URL,
                "enrollment_path": "/api/v1/relay-enroll",
                "activation_timeout_seconds": 30,
            }
        ),
        encoding="utf-8",
    )
    return path


def bundle() -> dict:
    return {
        "schema_version": 1,
        "relay_id": RELAY_ID,
        "agent_token": "A" * 32,
        "state_signing_key": base64.urlsafe_b64encode(b"S" * 32).decode().rstrip("="),
        "manager_url": MANAGER_URL,
        "relay_state_url": f"{MANAGER_URL}/api/v1/relay-state/{RELAY_ID}",
        "telemetry_url": f"{MANAGER_URL}/api/v1/relays/{RELAY_ID}/telemetry",
        "publish_authorization_url": f"{MANAGER_URL}/api/v1/publish-authorization",
        "bootstrap_status_url": f"{MANAGER_URL}/api/v1/relays/{RELAY_ID}/bootstrap-status",
        "public_host": "198.51.100.20",
        "srt_port": 8890,
        "rtmp_port": 1935,
        "max_active_models": 15,
        "quota_bytes": 32_000_000_000_000,
        "desired_state_schema": 1,
    }


def test_manager_config_rejects_release_placeholder(tmp_path):
    config = tmp_path / "manager.json"
    config.write_text(
        '{"schema_version":1,"manager_url":"https://relay.example.invalid",'
        '"enrollment_path":"/api/v1/relay-enroll","activation_timeout_seconds":300}',
        encoding="utf-8",
    )
    with pytest.raises(bootstrap_client.BootstrapError, match="placeholder"):
        bootstrap_client.ManagerConfig.load(config)


def test_bundle_requires_fixed_ports_and_same_origin(tmp_path):
    manager = bootstrap_client.ManagerConfig.load(manager_file(tmp_path / "manager.json"))
    assert bootstrap_client._validate_bundle(bundle(), manager)["quota_bytes"] == 32_000_000_000_000
    wrong_port = bundle()
    wrong_port["srt_port"] = 9000
    with pytest.raises(bootstrap_client.BootstrapError, match="fixed relay"):
        bootstrap_client._validate_bundle(wrong_port, manager)
    wrong_origin = bundle()
    wrong_origin["telemetry_url"] = "https://203.0.113.10/api/v1/telemetry"
    with pytest.raises(bootstrap_client.BootstrapError, match="pinned manager origin"):
        bootstrap_client._validate_bundle(wrong_origin, manager)


def test_claim_id_is_durable_and_retry_safe(tmp_path):
    first = bootstrap_client._load_or_create_claim(tmp_path)
    second = bootstrap_client._load_or_create_claim(tmp_path)
    assert first == second
    assert bootstrap_client.CLAIM_RE.fullmatch(first)


def test_enrollment_writes_identity_last_and_reuses_it(tmp_path, monkeypatch):
    manager = bootstrap_client.ManagerConfig.load(manager_file(tmp_path / "manager.json"))
    state_dir = tmp_path / "state"
    monkeypatch.setattr(bootstrap_client.getpass, "getpass", lambda _prompt: "P" * 24)
    monkeypatch.setattr(bootstrap_client, "_ssl_context", lambda _path: object())
    monkeypatch.setattr(bootstrap_client, "_opener", lambda _context: object())
    monkeypatch.setattr(bootstrap_client, "_force_ipv4_networking", lambda: None)
    monkeypatch.setattr(bootstrap_client, "_request_json", lambda *_args, **_kwargs: bundle())
    enrolled, reused = bootstrap_client.enroll(manager, tmp_path / "ca.pem", state_dir, "0.2.0")
    assert reused is False
    assert enrolled["relay_id"] == RELAY_ID
    identity = json.loads((state_dir / "identity.json").read_text(encoding="utf-8"))
    assert "agent_token" not in identity
    assert "state_signing_key" not in identity
    monkeypatch.setattr(
        bootstrap_client.getpass,
        "getpass",
        lambda _prompt: pytest.fail("retry must not prompt for a pairing code"),
    )
    replayed, reused = bootstrap_client.enroll(manager, tmp_path / "ca.pem", state_dir, "0.2.0")
    assert reused is True
    assert replayed["agent_token"] == "A" * 32


@pytest.mark.skipif(os.name != "posix", reason="directory mode semantics are POSIX-specific")
def test_atomic_write_parent_mode_preserves_secret_and_service_directory_modes(tmp_path):
    service_dir = tmp_path / "relay-agent"
    service_dir.mkdir(mode=0o750)
    bootstrap_client._atomic_write(
        service_dir / "relay-agent.env",
        b"CONFIG=1\n",
        0o640,
        parent_mode=0o750,
    )
    assert service_dir.stat().st_mode & 0o777 == 0o750

    secret_dir = tmp_path / "credentials"
    secret_dir.mkdir(mode=0o750)
    bootstrap_client._atomic_write(secret_dir / "token", b"secret\n", 0o400)
    assert secret_dir.stat().st_mode & 0o777 == 0o700


def test_status_requires_full_pool_eligibility(tmp_path, monkeypatch):
    manager = bootstrap_client.ManagerConfig.load(manager_file(tmp_path / "manager.json"))
    state_dir = tmp_path / "state"
    value = bundle()
    identity = {key: item for key, item in value.items() if key not in {"agent_token", "state_signing_key"}}
    bootstrap_client._atomic_write(
        state_dir / "identity.json",
        (json.dumps(identity) + "\n").encode(),
        0o600,
    )
    bootstrap_client._atomic_write(
        state_dir / "credentials" / "manager_token",
        (value["agent_token"] + "\n").encode(),
        0o400,
    )
    bootstrap_client._atomic_write(
        state_dir / "credentials" / "state_signing_key",
        (value["state_signing_key"] + "\n").encode(),
        0o400,
    )
    monkeypatch.setattr(bootstrap_client, "_ssl_context", lambda _path: object())
    monkeypatch.setattr(bootstrap_client, "_opener", lambda _context: object())
    monkeypatch.setattr(bootstrap_client, "_force_ipv4_networking", lambda: None)
    monkeypatch.setattr(
        bootstrap_client,
        "_request_json",
        lambda *_args, **_kwargs: {
            "relay_id": RELAY_ID,
            "lifecycle_status": "active",
            "online": True,
            "mediamtx_healthy": True,
            "desired_generation": 0,
            "reported_generation": 0,
            "healthy_streak": 3,
            "healthy_streak_required": 3,
            "pool_eligible": True,
        },
    )
    status = bootstrap_client.wait_until_active(manager, tmp_path / "ca.pem", state_dir)
    assert status["pool_eligible"] is True
