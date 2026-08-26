#!/usr/bin/env bash
set -euo pipefail
umask 077

[[ ${EUID} -eq 0 ]] || { echo "ERROR: run as root" >&2; exit 1; }
backup=${1:-}
backup_real=$(realpath -e -- "${backup}")
case "${backup_real}" in
  /var/backups/relay-node/firewall-[0-9]*) ;;
  *) echo "ERROR: refusing unsafe firewall backup path" >&2; exit 1 ;;
esac
[[ -d ${backup_real}/ufw ]] || { echo "ERROR: firewall backup is incomplete" >&2; exit 1; }

ufw --force disable >/dev/null 2>&1 || true
rm -rf -- /etc/ufw
cp -a "${backup_real}/ufw" /etc/ufw
if grep -Eq '^ENABLED=yes' "${backup_real}/ufw/ufw.conf" 2>/dev/null; then
  ufw --force enable >/dev/null
else
  ufw --force disable >/dev/null 2>&1 || true
fi
systemctl restart ufw.service 2>/dev/null || true
date -u +%Y%m%dT%H%M%SZ > "${backup_real}/rollback.executed"
echo "FIREWALL_ROLLED_BACK backup=${backup_real}"
