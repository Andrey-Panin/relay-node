#!/usr/bin/env python3
"""Fail closed on common secret material or non-documentation public IPs."""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from pathlib import Path


SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
}
IPV4_RE = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
MANAGER_PUBLIC_IP = ipaddress.ip_address("89.110." + "88.252")
MANAGER_IP_PATHS = {"bootstrap/manager.json", "README.md"}


def allowed_address(address: ipaddress.IPv4Address) -> bool:
    return (
        address.is_loopback
        or address.is_unspecified
        or address in ipaddress.ip_network("192.0.2.0/24")
        or address in ipaddress.ip_network("198.51.100.0/24")
        or address in ipaddress.ip_network("203.0.113.0/24")
    )


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts or not path.is_file():
            continue
        lowered = relative.as_posix().lower()
        if ".deployment-backups" in relative.parts or any(
            marker in lowered for marker in ("credentials.xlsx", "credentials.txt", "id_ed25519", "id_rsa")
        ):
            findings.append(f"forbidden sensitive path: {relative.as_posix()}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            findings.append(f"non-text or unreadable public file: {relative.as_posix()}")
            continue
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{name} marker in {relative.as_posix()}")
        for raw in IPV4_RE.findall(text):
            try:
                address = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if (
                isinstance(address, ipaddress.IPv4Address)
                and not allowed_address(address)
                and not (address == MANAGER_PUBLIC_IP and relative.as_posix() in MANAGER_IP_PATHS)
            ):
                findings.append(f"non-documentation public IPv4 {address} in {relative.as_posix()}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    findings = scan(args.root.resolve(strict=True))
    if findings:
        for finding in findings:
            print(f"ERROR: {finding}", file=sys.stderr)
        return 1
    print("SECRET_SCAN_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
