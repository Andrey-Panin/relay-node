#!/usr/bin/env bash
set -euo pipefail

[[ ${EUID} -eq 0 ]] || { echo "ERROR: run as root" >&2; exit 1; }
for file in \
  /var/lib/relay-bootstrap/identity.json \
  /var/lib/relay-bootstrap/credentials/manager_token \
  /var/lib/relay-bootstrap/credentials/state_signing_key \
  /etc/relay-agent/relay-agent.env \
  /etc/relay-agent/manager-ca.pem; do
  [[ -s ${file} ]] || { echo "ERROR: missing required bootstrap file ${file}" >&2; exit 1; }
done
grep -Eq '^RELAY_ID=[0-9a-f]{8}-[0-9a-f-]{27}$' /etc/relay-agent/relay-agent.env || {
  echo "ERROR: relay-agent.env is not enrolled" >&2
  exit 1
}
systemctl enable relay-agent.service mediamtx.service >/dev/null
systemctl restart relay-agent.service mediamtx.service
systemctl --no-pager --full status relay-agent.service mediamtx.service || true
