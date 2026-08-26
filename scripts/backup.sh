#!/usr/bin/env bash
set -euo pipefail
umask 077

[[ ${EUID} -eq 0 ]] || { echo "ERROR: run as root" >&2; exit 1; }

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="/var/backups/relay-node/${timestamp}"
[[ ! -e ${backup_dir} ]] || { echo "ERROR: backup already exists: ${backup_dir}" >&2; exit 1; }
install -d -m 0700 "${backup_dir}/inventory"

paths=(
  /etc/mediamtx
  /etc/relay-agent
  /etc/systemd/system/mediamtx.service
  /etc/systemd/system/relay-agent.service
  /etc/sysctl.d/90-media-relay.conf
  /etc/apt/preferences.d/relay-node-ffmpeg
  /etc/ufw
)
existing=()
for path in "${paths[@]}"; do
  if [[ -e ${path} || -L ${path} ]]; then
    printf '%s\tpresent\n' "${path}" >> "${backup_dir}/inventory/paths.tsv"
    existing+=("${path}")
  else
    printf '%s\tabsent\n' "${path}" >> "${backup_dir}/inventory/paths.tsv"
  fi
done
if ((${#existing[@]})); then
  tar --absolute-names --acls --xattrs -czpf "${backup_dir}/filesystem.tar.gz" "${existing[@]}"
else
  : > "${backup_dir}/filesystem.empty"
fi

for link in /opt/mediamtx/current /opt/relay-node/current; do
  if [[ -L ${link} ]]; then
    printf '%s\t%s\n' "${link}" "$(readlink "${link}")"
  else
    printf '%s\tABSENT\n' "${link}"
  fi
done > "${backup_dir}/inventory/symlinks.tsv"

for unit in mediamtx.service relay-agent.service ufw.service; do
  enabled=$(systemctl is-enabled "${unit}" 2>/dev/null || true)
  active=$(systemctl is-active "${unit}" 2>/dev/null || true)
  printf '%s\t%s\t%s\n' "${unit}" "${enabled:-unknown}" "${active:-unknown}"
done > "${backup_dir}/inventory/systemd.tsv"

dpkg-query -W -f='${binary:Package}\t${Version}\n' > "${backup_dir}/inventory/packages.tsv"
apt-mark showhold > "${backup_dir}/inventory/apt-holds.txt"
for key in net.core.rmem_default net.core.rmem_max net.core.wmem_default net.core.wmem_max net.core.netdev_max_backlog; do
  printf '%s\t%s\n' "${key}" "$(sysctl -n "${key}")"
done > "${backup_dir}/inventory/sysctl-media.tsv"
uname -a > "${backup_dir}/inventory/uname.txt"
ip -details address show > "${backup_dir}/inventory/ip-address.txt"
ip route show table all > "${backup_dir}/inventory/ip-routes.txt"
ss -lntup > "${backup_dir}/inventory/listeners.txt" 2>&1 || true
nft list ruleset > "${backup_dir}/inventory/nft-ruleset.txt" 2>&1 || true
ufw status verbose > "${backup_dir}/inventory/ufw-status.txt" 2>&1 || true
systemctl cat mediamtx.service relay-agent.service > "${backup_dir}/inventory/units.txt" 2>&1 || true

(
  cd "${backup_dir}"
  find . -type f ! -name 'SHA256SUMS*' -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS.tmp
  mv SHA256SUMS.tmp SHA256SUMS
  sha256sum -c SHA256SUMS >/dev/null
)
chmod -R go-rwx "${backup_dir}"
echo "BACKUP_DIR=${backup_dir}"
