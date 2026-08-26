# Security policy

Do not open a public issue containing pairing codes, agent tokens, signing
keys, destination stream keys, server addresses that are not already public,
logs with credentials, or private certificates.

Report vulnerabilities through GitHub Private Vulnerability Reporting for the
repository. If that channel is unavailable, contact the repository owner
privately before sharing technical details.

Supported deployment: the latest tagged relay-node release on Ubuntu 24.04 LTS
amd64. The installer refuses other operating-system versions and architectures.

The public repository must never contain:

- private keys or pairing/agent tokens;
- production database dumps, environment files, or deployment backups;
- model destination keys;
- SSH credentials or provider snapshots.

The manager CA certificate is public trust material. Its corresponding private
key must remain only on the certificate authority.
