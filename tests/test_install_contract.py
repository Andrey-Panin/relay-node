from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_top_level_three_command_installer_contract():
    installer = read("install.sh")
    assert "Ubuntu 24.04" not in installer  # delegated to preflight, no duplicated policy
    assert "bootstrap_client.py\" enroll" in installer
    assert "bootstrap_client.py\" status" in installer
    assert "scripts/backup.sh" in installer
    assert "scripts/rollback.sh" in installer
    assert "firewall_confirm.sh" in installer


def test_ubuntu_noble_and_media_versions_are_pinned_safely():
    preflight = read("scripts/preflight.sh")
    runtime = read("scripts/install_runtime.sh")
    assert "VERSION_ID:-} != 24.04" in preflight
    assert "dpkg --print-architecture) != amd64" in preflight
    assert "MEDIAMTX_VERSION=1.20.1" in runtime
    assert re.search(r"MEDIAMTX_SHA256=[0-9a-f]{64}", runtime)
    assert "7:6.1.1-3ubuntu5" in runtime
    assert "+esm" in runtime
    assert "apt-mark hold ffmpeg" not in runtime


def test_fixed_public_ports_and_loopback_controls():
    mediamtx = read("config/mediamtx.yml")
    firewall = read("scripts/firewall_apply.sh")
    assert "rtmpAddress: :1935" in mediamtx
    assert "srtAddress: :8890" in mediamtx
    for value in ("127.0.0.1:8554", "127.0.0.1:9997", "127.0.0.1:9998"):
        assert value in mediamtx
    assert "1935/tcp" in firewall
    assert "8890/udp" in firewall
    assert "sshd -T" in firewall


def test_public_bootstrap_is_bound_to_the_production_manager():
    manager = json.loads(read("bootstrap/manager.json"))
    ca = read("bootstrap/manager-ca.pem")
    manager_host = "89.110." "88.252"
    assert manager == {
        "schema_version": 1,
        "manager_url": f"https://{manager_host}",
        "enrollment_path": "/api/v1/relay-enroll",
        "activation_timeout_seconds": 300,
    }
    assert ca.startswith("-----BEGIN CERTIFICATE-----\n")
    assert ca.rstrip().endswith("-----END CERTIFICATE-----")
    assert "PRIVATE KEY" not in ca
    assert "RELEASE PLACEHOLDER" not in ca


def test_no_old_deployment_backup_or_hotfix_is_packaged():
    names = {path.relative_to(ROOT).as_posix() for path in ROOT.rglob("*")}
    assert not any(".deployment-backups" in value for value in names)
    assert "deploy_agent_hotfix.py" not in names
