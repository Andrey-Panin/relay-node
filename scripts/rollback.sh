#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() { echo "Usage: $0 /var/backups/relay-node/YYYYMMDDTHHMMSSZ" >&2; }
[[ ${EUID} -eq 0 ]] || { echo "ERROR: run as root" >&2; exit 1; }
backup=${1:-}
[[ -n ${backup} ]] || { usage; exit 2; }
backup_real=$(realpath -e -- "${backup}")
case "${backup_real}" in
  /var/backups/relay-node/[0-9]*) ;;
  *) echo "ERROR: refusing unsafe backup path" >&2; exit 1 ;;
esac
[[ -f ${backup_real}/SHA256SUMS ]] || { echo "ERROR: backup is incomplete" >&2; exit 1; }
(cd "${backup_real}" && sha256sum -c SHA256SUMS >/dev/null)

systemctl stop relay-agent.service mediamtx.service 2>/dev/null || true
systemctl disable relay-agent.service mediamtx.service 2>/dev/null || true

# Remove only the exact paths captured by backup.sh, then restore the verified
# originals. Enrollment identity under /var/lib/relay-bootstrap is deliberately
# retained so the same one-time claim can be retried safely.
while IFS=$'\t' read -r path _state; do
  case "${path}" in
    /etc/mediamtx|/etc/relay-agent|/etc/ufw) rm -rf -- "${path}" ;;
    /etc/systemd/system/mediamtx.service|\
    /etc/systemd/system/relay-agent.service|\
    /etc/sysctl.d/90-media-relay.conf|\
    /etc/apt/preferences.d/relay-node-ffmpeg) rm -f -- "${path}" ;;
    *) echo "ERROR: unsafe path in backup inventory: ${path}" >&2; exit 1 ;;
  esac
done < "${backup_real}/inventory/paths.tsv"
if [[ -f ${backup_real}/filesystem.tar.gz ]]; then
  tar --absolute-names --acls --xattrs -xzpf "${backup_real}/filesystem.tar.gz" -C /
fi

while IFS=$'\t' read -r link target; do
  case "${link}" in
    /opt/mediamtx/current|/opt/relay-node/current) ;;
    *) echo "ERROR: unsafe symlink in backup inventory" >&2; exit 1 ;;
  esac
  [[ ! -L ${link} ]] || unlink "${link}"
  if [[ ${target} != ABSENT ]]; then
    rm -f -- "${link}.rollback-new"
    ln -s "${target}" "${link}.rollback-new"
    mv -Tf "${link}.rollback-new" "${link}"
  fi
done < "${backup_real}/inventory/symlinks.tsv"

systemctl daemon-reload
while IFS=$'\t' read -r unit enabled active; do
  case "${unit}" in mediamtx.service|relay-agent.service|ufw.service) ;; *) continue ;; esac
  case "${enabled}" in
    enabled) systemctl enable "${unit}" >/dev/null 2>&1 || true ;;
    disabled) systemctl disable "${unit}" >/dev/null 2>&1 || true ;;
  esac
  if [[ ${active} == active ]]; then
    systemctl start "${unit}" || true
  else
    systemctl stop "${unit}" 2>/dev/null || true
  fi
done < "${backup_real}/inventory/systemd.tsv"
while IFS=$'\t' read -r key value; do
  case "${key}" in
    net.core.rmem_default|net.core.rmem_max|net.core.wmem_default|net.core.wmem_max|net.core.netdev_max_backlog)
      sysctl -q -w "${key}=${value}" || true
      ;;
  esac
done < "${backup_real}/inventory/sysctl-media.tsv"
systemctl restart ufw.service 2>/dev/null || true
echo "ROLLBACK_COMPLETE backup=${backup_real}"
echo "Enrollment identity and inert versioned release directories were retained for retry/forensics."
