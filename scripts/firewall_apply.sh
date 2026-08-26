#!/usr/bin/env bash
set -euo pipefail
umask 077

[[ ${EUID} -eq 0 ]] || { echo "ERROR: run as root" >&2; exit 1; }

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="/var/backups/relay-node/firewall-${timestamp}"
[[ ! -e ${backup_dir} ]] || { echo "ERROR: firewall backup already exists" >&2; exit 1; }
install -d -m 0700 "${backup_dir}"
[[ -d /etc/ufw ]] || { echo "ERROR: /etc/ufw is missing" >&2; exit 1; }
cp -a /etc/ufw "${backup_dir}/ufw"
systemctl is-enabled ufw.service > "${backup_dir}/enabled" 2>/dev/null || true
systemctl is-active ufw.service > "${backup_dir}/active" 2>/dev/null || true
ufw status verbose > "${backup_dir}/status.txt" 2>&1 || true

rollback_script=/opt/relay-node/current/scripts/firewall_rollback.sh
[[ -x ${rollback_script} ]] || rollback_script=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/firewall_rollback.sh
install -m 0700 "${rollback_script}" "${backup_dir}/firewall_rollback.sh"
unit="relay-firewall-rollback-${timestamp}"
systemd-run --quiet --unit="${unit}" --on-active=10m --timer-property=AccuracySec=1s \
  "${backup_dir}/firewall_rollback.sh" "${backup_dir}"

declare -A ssh_ports=()
if command -v sshd >/dev/null 2>&1; then
  while read -r port; do
    [[ ${port} =~ ^[0-9]+$ ]] && ((port >= 1 && port <= 65535)) && ssh_ports["${port}"]=1
  done < <(sshd -T 2>/dev/null | awk '$1 == "port" {print $2}')
fi
if [[ -n ${SSH_CONNECTION:-} ]]; then
  current_port=${SSH_CONNECTION##* }
  [[ ${current_port} =~ ^[0-9]+$ ]] && ((current_port >= 1 && current_port <= 65535)) && ssh_ports["${current_port}"]=1
fi
if ((${#ssh_ports[@]} == 0)); then
  ssh_ports[22]=1
  echo "WARNING: could not detect sshd port; preserving TCP/22" >&2
fi

# The rollback timer is live before the first rule mutation. Existing provider
# rules are preserved; this installer adds only the required ingress rules.
for port in "${!ssh_ports[@]}"; do
  ufw allow "${port}/tcp" comment 'SSH management'
done
ufw allow 1935/tcp comment 'Media relay RTMP fallback'
ufw allow 8890/udp comment 'Media relay encrypted SRT'
ufw default deny incoming
ufw default allow outgoing
ufw --force enable

echo "FIREWALL_TIMESTAMP=${timestamp}"
echo "FIREWALL_BACKUP=${backup_dir}"
echo "AUTOMATIC_ROLLBACK_TIMER=${unit}.timer (10 minutes)"
echo "Verify a second SSH session if possible. The installer confirms this timer only after manager activation."
