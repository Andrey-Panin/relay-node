from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from scripts import check_public_tree


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


def test_no_pipefail_sigpipe_prone_runtime_probes():
    runtime = read("scripts/install_runtime.sh")
    verify = read("scripts/verify.sh")
    forbidden = (
        "apt-cache policy ffmpeg | awk",
        "/usr/bin/ffmpeg -hide_banner -protocols 2>/dev/null | grep",
        "/usr/bin/ffmpeg -hide_banner -h demuxer=rtsp 2>/dev/null | grep",
        "/usr/bin/ffmpeg -version | head",
        "ufw status | grep",
    )
    assert all(shape not in runtime + verify for shape in forbidden)
    assert "apt_policy=$(apt-cache policy ffmpeg)" in runtime
    assert "ERROR: ffmpeg lacks RTMP protocol support" in runtime
    assert "ERROR: ffmpeg lacks RTMPS protocol support" in runtime
    assert "ERROR: ffmpeg RTSP demuxer lacks timeout support" in runtime
    assert "ERROR: UFW lacks RTMP/1935 allow rule" in verify
    assert "ERROR: UFW lacks SRT/8890 allow rule" in verify


def test_installer_cleans_bytecode_before_strict_validation():
    installer = read("install.sh")
    assert "export PYTHONDONTWRITEBYTECODE=1" in installer
    assert "check_public_tree.py\" --clean-bytecode" in installer
    assert installer.index("--clean-bytecode") < installer.index("secret_scan.py")


def test_clean_bytecode_removes_only_cache_artifacts(tmp_path):
    cache = tmp_path / "scripts" / "__pycache__"
    cache.mkdir(parents=True)
    stale = cache / "module.cpython-312.pyc"
    stale.write_bytes(b"stale")
    git_stale = tmp_path / ".git" / "__pycache__" / "git.cpython-312.pyc"
    git_stale.parent.mkdir(parents=True)
    git_stale.write_bytes(b"keep")
    unexpected = tmp_path / "unexpected.txt"
    unexpected.write_text("user/source file", encoding="utf-8")

    assert check_public_tree.clean_bytecode(tmp_path) == 1
    assert not stale.exists()
    assert git_stale.exists()
    assert unexpected.exists()

    reviewed = tmp_path / "README.md"
    reviewed.write_text("reviewed\n", encoding="utf-8")
    digest = hashlib.sha256(reviewed.read_bytes()).hexdigest()
    (tmp_path / "PUBLIC_FILES.txt").write_text("README.md\nSHA256SUMS\n", encoding="utf-8")
    (tmp_path / "SHA256SUMS").write_text(f"{digest}  README.md\n", encoding="ascii")
    with pytest.raises(check_public_tree.TreeError, match="public tree mismatch"):
        check_public_tree.check(tmp_path)


def test_clean_bytecode_does_not_follow_symlink(tmp_path):
    if os.name != "posix":
        pytest.skip("symlink semantics are POSIX-specific")
    outside = tmp_path.parent / "relay-node-outside-bytecode"
    outside.mkdir()
    try:
        target = outside / "external.pyc"
        target.write_bytes(b"keep")
        cache = tmp_path / "scripts" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "external.pyc").symlink_to(target)
        assert check_public_tree.clean_bytecode(tmp_path) == 0
        assert target.exists()
    finally:
        target.unlink(missing_ok=True)
        outside.rmdir()
