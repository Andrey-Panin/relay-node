#!/usr/bin/env bash
set -euo pipefail

[[ ${EUID} -eq 0 ]] || { echo "ERROR: run as root" >&2; exit 1; }
timestamp=${1:-}
[[ ${timestamp} =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || {
  echo "ERROR: pass the timestamp printed by firewall_apply.sh" >&2
  exit 2
}
backup="/var/backups/relay-node/firewall-${timestamp}"
[[ -d ${backup} ]] || { echo "ERROR: firewall backup not found" >&2; exit 1; }
[[ ! -f ${backup}/rollback.executed ]] || {
  echo "ERROR: automatic firewall rollback already executed; refusing false confirmation" >&2
  exit 1
}
unit="relay-firewall-rollback-${timestamp}"
systemctl stop "${unit}.timer"
systemctl reset-failed "${unit}.service" 2>/dev/null || true
echo "FIREWALL_CONFIRMED timer=${unit}.timer cancelled backup=${backup}"
