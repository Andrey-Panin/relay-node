#!/usr/bin/env bash
set -euo pipefail
umask 077

MEDIAMTX_VERSION=1.20.1
MEDIAMTX_SHA256=81b143f55a5d23d4a8c028d52869c14ea4a59919900528698fcc97a747fd69c6
FFMPEG_APT_BASE='7:6.1.1-3ubuntu5'
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SOURCE_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd -P)
RELEASE_VERSION=$(tr -d '\r\n' < "${SOURCE_ROOT}/VERSION")
RELEASE_STAMP=$(date -u +%Y%m%dT%H%M%SZ)

usage() { echo "Usage: $0 --backup /var/backups/relay-node/YYYYMMDDTHHMMSSZ" >&2; }
backup_dir=""
while (($#)); do
  case "$1" in
    --backup) backup_dir=${2:-}; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
[[ ${EUID} -eq 0 ]] || { echo "ERROR: run as root" >&2; exit 1; }
[[ -n ${backup_dir} ]] || { usage; exit 2; }
backup_real=$(realpath -e -- "${backup_dir}")
case "${backup_real}" in /var/backups/relay-node/[0-9]*) ;; *) echo "ERROR: unsafe backup path" >&2; exit 1 ;; esac
[[ -f ${backup_real}/SHA256SUMS ]] || { echo "ERROR: backup is incomplete" >&2; exit 1; }
(cd "${backup_real}" && sha256sum -c SHA256SUMS >/dev/null)

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl tar ufw python3
install -D -m 0644 /dev/stdin /etc/apt/preferences.d/relay-node-ffmpeg <<EOF
Package: ffmpeg
Pin: version ${FFMPEG_APT_BASE}*
Pin-Priority: 1001
EOF

# Noble ships 7:6.1.1-3ubuntu5. Ubuntu Pro can supply a signed +esmN
# successor. Accept only that official family, select the repository candidate,
# and never force a downgrade from a newer ESM build.
if ! apt_policy=$(apt-cache policy ffmpeg); then
  echo "ERROR: apt-cache could not inspect the ffmpeg candidate" >&2
  exit 1
fi
candidate_ffmpeg=$(awk '/Candidate:/ && candidate == "" {candidate=$2} END {print candidate}' <<<"${apt_policy}")
installed_ffmpeg=$(dpkg-query -W -f='${Version}' ffmpeg 2>/dev/null || true)
ffmpeg_family_re='^7:6\.1\.1-3ubuntu5(\+esm[0-9]+)?$'
if [[ -n ${installed_ffmpeg} && ! ${installed_ffmpeg} =~ ${ffmpeg_family_re} ]]; then
  echo "ERROR: installed ffmpeg ${installed_ffmpeg} is outside the validated Ubuntu Noble family" >&2
  exit 1
fi
if [[ ${candidate_ffmpeg} == '(none)' || ! ${candidate_ffmpeg} =~ ${ffmpeg_family_re} ]]; then
  if [[ -z ${installed_ffmpeg} ]]; then
    echo "ERROR: a validated Noble ffmpeg candidate is unavailable; verify signed noble/universe or Ubuntu Pro sources" >&2
    exit 1
  fi
  selected_ffmpeg=${installed_ffmpeg}
elif [[ -n ${installed_ffmpeg} ]] && dpkg --compare-versions "${installed_ffmpeg}" gt "${candidate_ffmpeg}"; then
  selected_ffmpeg=${installed_ffmpeg}
else
  selected_ffmpeg=${candidate_ffmpeg}
  apt-get install -y --no-install-recommends "ffmpeg=${selected_ffmpeg}"
fi
installed_ffmpeg=$(dpkg-query -W -f='${Version}' ffmpeg)
[[ ${installed_ffmpeg} == "${selected_ffmpeg}" && ${installed_ffmpeg} =~ ${ffmpeg_family_re} ]] || {
  echo "ERROR: installed ffmpeg failed the Noble package-family validation" >&2
  exit 1
}
if ! ffmpeg_protocols=$(/usr/bin/ffmpeg -hide_banner -protocols 2>/dev/null); then
  echo "ERROR: ffmpeg protocol capability probe failed" >&2
  exit 1
fi
grep -Eq '^[[:space:]]+rtmp$' <<<"${ffmpeg_protocols}" || {
  echo "ERROR: ffmpeg lacks RTMP protocol support" >&2
  exit 1
}
grep -Eq '^[[:space:]]+rtmps$' <<<"${ffmpeg_protocols}" || {
  echo "ERROR: ffmpeg lacks RTMPS protocol support" >&2
  exit 1
}
if ! ffmpeg_rtsp_help=$(/usr/bin/ffmpeg -hide_banner -h demuxer=rtsp 2>/dev/null); then
  echo "ERROR: ffmpeg RTSP demuxer capability probe failed" >&2
  exit 1
fi
grep -q 'timeout' <<<"${ffmpeg_rtsp_help}" || {
  echo "ERROR: ffmpeg RTSP demuxer lacks timeout support" >&2
  exit 1
}

getent group media-relay >/dev/null || groupadd --system media-relay
id mediamtx >/dev/null 2>&1 || useradd --system --gid media-relay --home-dir /var/lib/mediamtx --shell /usr/sbin/nologin mediamtx
id relay-agent >/dev/null 2>&1 || useradd --system --gid media-relay --home-dir /var/lib/relay-agent --shell /usr/sbin/nologin relay-agent

install -d -m 0755 -o root -g root /opt/mediamtx /opt/mediamtx/releases
install -d -m 0755 -o root -g root /opt/relay-node /opt/relay-node/releases
install -d -m 0700 -o root -g root /var/lib/relay-bootstrap /var/lib/relay-bootstrap/credentials

tmp_dir=$(mktemp -d /var/tmp/relay-node-install.XXXXXX)
cleanup() {
  case "${tmp_dir}" in
    /var/tmp/relay-node-install.*) rm -rf -- "${tmp_dir}" ;;
    *) echo "WARNING: refusing unsafe temporary cleanup path" >&2 ;;
  esac
}
trap cleanup EXIT
archive="${tmp_dir}/mediamtx.tar.gz"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  --output "${archive}" \
  "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_amd64.tar.gz"
printf '%s  %s\n' "${MEDIAMTX_SHA256}" "${archive}" | sha256sum -c - >/dev/null
tar -xzf "${archive}" -C "${tmp_dir}" mediamtx
media_release="/opt/mediamtx/releases/v${MEDIAMTX_VERSION}"
if [[ -e ${media_release}/mediamtx ]]; then
  cmp -s "${tmp_dir}/mediamtx" "${media_release}/mediamtx" || {
    echo "ERROR: immutable MediaMTX release differs from the verified artifact" >&2
    exit 1
  }
else
  install -d -m 0755 "${media_release}"
  install -m 0755 "${tmp_dir}/mediamtx" "${media_release}/mediamtx"
fi
ln -sfn "${media_release}" /opt/mediamtx/.current.new
mv -Tf /opt/mediamtx/.current.new /opt/mediamtx/current

release_dir="/opt/relay-node/releases/${RELEASE_VERSION}-${RELEASE_STAMP}"
[[ ! -e ${release_dir} ]] || { echo "ERROR: immutable release already exists" >&2; exit 1; }
install -d -m 0755 "${release_dir}/relay_agent" "${release_dir}/scripts"
for source_file in "${SOURCE_ROOT}/relay_agent/"*.py; do
  install -m 0644 "${source_file}" "${release_dir}/relay_agent/$(basename "${source_file}")"
done
for source_file in "${SOURCE_ROOT}/scripts/"*.sh "${SOURCE_ROOT}/scripts/"*.py; do
  install -m 0755 "${source_file}" "${release_dir}/scripts/$(basename "${source_file}")"
done
install -m 0644 "${SOURCE_ROOT}/VERSION" "${release_dir}/VERSION"
python3 -m compileall -q "${release_dir}/relay_agent" "${release_dir}/scripts"
ln -sfn "${release_dir}" /opt/relay-node/.current.new
mv -Tf /opt/relay-node/.current.new /opt/relay-node/current

install -d -m 0750 -o mediamtx -g media-relay /etc/mediamtx
install -d -m 0750 -o root -g media-relay /etc/relay-agent
install -m 0640 -o mediamtx -g media-relay "${SOURCE_ROOT}/config/mediamtx.yml" /etc/mediamtx/mediamtx.yml
install -m 0644 -o root -g root "${SOURCE_ROOT}/config/90-media-relay.conf" /etc/sysctl.d/90-media-relay.conf
install -m 0644 -o root -g root "${SOURCE_ROOT}/systemd/mediamtx.service" /etc/systemd/system/mediamtx.service
install -m 0644 -o root -g root "${SOURCE_ROOT}/systemd/relay-agent.service" /etc/systemd/system/relay-agent.service
sysctl --system >/dev/null
systemctl daemon-reload

"/opt/mediamtx/current/mediamtx" --version
if ! ffmpeg_version=$(/usr/bin/ffmpeg -version); then
  echo "ERROR: ffmpeg version probe failed" >&2
  exit 1
fi
printf '%s\n' "${ffmpeg_version%%$'\n'*}"
echo "RUNTIME_STAGED release=${RELEASE_VERSION}-${RELEASE_STAMP} backup=${backup_real}"
