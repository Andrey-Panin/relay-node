#!/usr/bin/env python3
"""Render the non-secret relay-agent environment from enrolled identity."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from bootstrap_client import BootstrapError, ManagerConfig, _atomic_write, load_identity


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise BootstrapError("run config renderer as root")
        if args.output != Path("/etc/relay-agent/relay-agent.env"):
            raise BootstrapError("refusing an unexpected relay-agent environment path")
        manager = ManagerConfig.load(args.config)
        bundle = load_identity(args.state_dir, manager)
        if bundle is None:
            raise BootstrapError("relay must be enrolled before rendering configuration")
        values = {
            "RELAY_ID": bundle["relay_id"],
            "MANAGER_URL": manager.manager_url,
            "MANAGER_CA_FILE": "/etc/relay-agent/manager-ca.pem",
            "ALLOW_INSECURE_MANAGER_HTTP": "false",
            "MEDIAMTX_API_URL": "http://127.0.0.1:9997",
            "MEDIAMTX_INPUT_URL": "rtsp://127.0.0.1:8554",
            "AGENT_LISTEN_HOST": "127.0.0.1",
            "AGENT_LISTEN_PORT": "8091",
            "STATE_POLL_SECONDS": "5",
            "PATH_POLL_SECONDS": "2",
            "TELEMETRY_SECONDS": "15",
            "MAX_ACTIVE_MODELS": str(bundle["max_active_models"]),
            "TRAFFIC_QUOTA_BYTES": str(bundle["quota_bytes"]),
            "FFMPEG_PATH": "/usr/bin/ffmpeg",
        }
        data = "".join(f"{key}={value}\n" for key, value in values.items()).encode("ascii")
        _atomic_write(args.output, data, 0o640, parent_mode=0o750)
        print(f"CONFIG_RENDERED relay_id={bundle['relay_id']}")
    except BootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
