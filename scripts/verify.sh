#!/usr/bin/env bash
set -euo pipefail

[[ ${EUID} -eq 0 ]] || { echo "ERROR: run as root" >&2; exit 1; }
echo "VERSIONS"
/opt/mediamtx/current/mediamtx --version
/usr/bin/ffmpeg -version | head -n 1

healthy=false
required_ports=(1935 8554 8091 8890 9997 9998)
for _attempt in $(seq 1 90); do
  if ! systemctl is-active --quiet relay-agent.service mediamtx.service; then
    sleep 1
    continue
  fi
  listeners=$(ss -H -lntup)
  ports_ready=true
  for port in "${required_ports[@]}"; do
    if ! awk -v suffix=":${port}" '$5 ~ (suffix "$" ) {found=1} END {exit !found}' <<<"${listeners}"; then
      ports_ready=false
      break
    fi
  done
  if [[ ${ports_ready} == true ]] && \
     curl --fail --silent --show-error --noproxy '*' http://127.0.0.1:8091/healthz >/dev/null; then
    healthy=true
    break
  fi
  sleep 1
done
if [[ ${healthy} != true ]]; then
  echo "ERROR: relay services/listeners did not become healthy within 90 seconds" >&2
  systemctl --no-pager --full status relay-agent.service mediamtx.service >&2 || true
  ss -H -lntup >&2 || true
  journalctl -u relay-agent.service -u mediamtx.service -n 100 --no-pager >&2 || true
  exit 1
fi

echo "SERVICES"
systemctl is-active relay-agent.service mediamtx.service
echo "LISTENERS"
ss -H -lntup | awk '$5 ~ /:(1935|8554|8091|8890|9997|9998)$/ {print}'
echo "HEALTH"
curl --fail --silent --show-error --noproxy '*' http://127.0.0.1:8091/healthz
echo
echo "PUBLIC_FIREWALL"
ufw status verbose
ufw status | grep -Eq '^1935/tcp[[:space:]]+ALLOW' || { echo "ERROR: UFW lacks RTMP/1935 allow rule" >&2; exit 1; }
ufw status | grep -Eq '^8890/udp[[:space:]]+ALLOW' || { echo "ERROR: UFW lacks SRT/8890 allow rule" >&2; exit 1; }
echo "VERIFY_LOCAL_OK"
