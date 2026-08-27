#!/usr/bin/env bash
set -euo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1

if (($#)); then
  echo "ERROR: install.sh takes no arguments" >&2
  exit 2
fi
[[ ${EUID} -eq 0 ]] || { echo "ERROR: run with sudo bash install.sh" >&2; exit 1; }

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec 9>/run/lock/relay-node-install.lock
flock -n 9 || { echo "ERROR: another relay-node installation is running" >&2; exit 1; }

backup_dir=""
firewall_timestamp=""
finished=false
on_exit() {
  status=$?
  if [[ ${finished} == true || ${status} -eq 0 ]]; then
    return
  fi
  trap - EXIT
  echo "ERROR: installation failed; starting containment rollback" >&2
  if [[ -n ${firewall_timestamp} ]]; then
    firewall_backup="/var/backups/relay-node/firewall-${firewall_timestamp}"
    if [[ -d ${firewall_backup} && ! -f ${firewall_backup}/rollback.executed ]]; then
      bash "${SCRIPT_DIR}/scripts/firewall_rollback.sh" "${firewall_backup}" || true
    fi
    systemctl stop "relay-firewall-rollback-${firewall_timestamp}.timer" 2>/dev/null || true
  fi
  if [[ -n ${backup_dir} ]]; then
    bash "${SCRIPT_DIR}/scripts/rollback.sh" "${backup_dir}" || true
  fi
  echo "Recovery backup: ${backup_dir:-not-created}" >&2
  echo "Enrollment identity is retained in /var/lib/relay-bootstrap for retry." >&2
  exit "${status}"
}
trap on_exit EXIT
trap 'exit 130' INT TERM

echo "[1/8] Validating host and public source tree"
bash "${SCRIPT_DIR}/scripts/preflight.sh"
python3 "${SCRIPT_DIR}/scripts/check_public_tree.py" --clean-bytecode "${SCRIPT_DIR}"
python3 "${SCRIPT_DIR}/scripts/secret_scan.py" "${SCRIPT_DIR}"

echo "[2/8] Creating verified rollback backup"
backup_output=$(bash "${SCRIPT_DIR}/scripts/backup.sh")
echo "${backup_output}"
backup_dir=$(awk -F= '/^BACKUP_DIR=/{print $2}' <<<"${backup_output}")
[[ -n ${backup_dir} && -d ${backup_dir} ]] || { echo "ERROR: backup path was not returned" >&2; exit 1; }

echo "[3/8] Installing pinned runtime into immutable releases"
bash "${SCRIPT_DIR}/scripts/install_runtime.sh" --backup "${backup_dir}"

echo "[4/8] Pairing with the relay manager"
python3 "${SCRIPT_DIR}/scripts/bootstrap_client.py" enroll \
  --config "${SCRIPT_DIR}/bootstrap/manager.json" \
  --ca-file "${SCRIPT_DIR}/bootstrap/manager-ca.pem" \
  --state-dir /var/lib/relay-bootstrap \
  --version-file "${SCRIPT_DIR}/VERSION"
install -m 0644 -o root -g root "${SCRIPT_DIR}/bootstrap/manager-ca.pem" /etc/relay-agent/manager-ca.pem
python3 "${SCRIPT_DIR}/scripts/render_config.py" \
  --config "${SCRIPT_DIR}/bootstrap/manager.json" \
  --state-dir /var/lib/relay-bootstrap \
  --version-file "${SCRIPT_DIR}/VERSION" \
  --output /etc/relay-agent/relay-agent.env
install -d -m 0750 -o root -g media-relay /etc/relay-agent
chown root:media-relay /etc/relay-agent/relay-agent.env
chmod 0640 /etc/relay-agent/relay-agent.env

echo "[5/8] Applying firewall with a ten-minute automatic rollback"
firewall_output=$(bash "${SCRIPT_DIR}/scripts/firewall_apply.sh")
echo "${firewall_output}"
firewall_timestamp=$(awk -F= '/^FIREWALL_TIMESTAMP=/{print $2}' <<<"${firewall_output}")
[[ ${firewall_timestamp} =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || { echo "ERROR: firewall timestamp missing" >&2; exit 1; }

echo "[6/8] Starting and verifying the local relay"
bash "${SCRIPT_DIR}/scripts/start.sh"
bash "${SCRIPT_DIR}/scripts/verify.sh"

echo "[7/8] Waiting for three healthy manager heartbeats and pool admission"
python3 "${SCRIPT_DIR}/scripts/bootstrap_client.py" status \
  --config "${SCRIPT_DIR}/bootstrap/manager.json" \
  --ca-file "${SCRIPT_DIR}/bootstrap/manager-ca.pem" \
  --state-dir /var/lib/relay-bootstrap \
  --version-file "${SCRIPT_DIR}/VERSION"

echo "[8/8] Confirming firewall and installation"
bash "${SCRIPT_DIR}/scripts/firewall_confirm.sh" "${firewall_timestamp}"
finished=true
trap - EXIT INT TERM
echo "INSTALL_COMPLETE backup=${backup_dir}"
echo "Relay is healthy and active in the manager pool."
