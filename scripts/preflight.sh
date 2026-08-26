#!/usr/bin/env bash
set -euo pipefail

[[ ${EUID} -eq 0 ]] || { echo "ERROR: run as root" >&2; exit 1; }

# shellcheck source=/dev/null
source /etc/os-release
if [[ ${ID:-} != ubuntu || ${VERSION_ID:-} != 24.04 ]]; then
  echo "ERROR: this release supports exactly Ubuntu 24.04 LTS; found ${PRETTY_NAME:-unknown}" >&2
  exit 1
fi
if [[ $(dpkg --print-architecture) != amd64 ]]; then
  echo "ERROR: this release supports exactly the amd64 architecture" >&2
  exit 1
fi

required=(apt-get apt-cache dpkg-query systemctl systemd-run ss tar sha256sum python3 realpath flock)
for command_name in "${required[@]}"; do
  command -v "${command_name}" >/dev/null || {
    echo "ERROR: required command is missing: ${command_name}" >&2
    exit 1
  }
done

memory_kib=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
if [[ -d /opt ]]; then
  disk_kib=$(df -Pk /opt | awk 'NR==2 {print $4}')
else
  disk_kib=$(df -Pk / | awk 'NR==2 {print $4}')
fi
if ((memory_kib < 3000000)); then
  echo "WARNING: less than 3 GiB RAM; reduce relay capacity in the admin panel" >&2
fi
if ((disk_kib < 5000000)); then
  echo "ERROR: less than 5 GiB free disk" >&2
  exit 1
fi

echo "OS=${PRETTY_NAME}"
echo "ARCH=$(dpkg --print-architecture)"
echo "RAM_KIB=${memory_kib}"
echo "DISK_FREE_KIB=${disk_kib}"
echo "EXISTING_LISTENERS_BEGIN"
ss -H -lntup 2>/dev/null || true
echo "EXISTING_LISTENERS_END"
echo "PREFLIGHT_OK"
