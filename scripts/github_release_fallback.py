#!/usr/bin/env python3
"""Build a curl config for GitHub's official release-asset storage backend."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit


SOURCE_HOST = "release-assets.githubusercontent.com"
FALLBACK_HOST = "releaseassetproduction.blob.core.windows.net"
SOURCE_PATH_PREFIX = "/github-production-release-asset/"
MAX_HEADER_BYTES = 64 * 1024


class RedirectError(RuntimeError):
    pass


def _location_from_headers(headers: bytes) -> str:
    if len(headers) > MAX_HEADER_BYTES:
        raise RedirectError("GitHub response headers exceed the safety limit")
    if b"\x00" in headers:
        raise RedirectError("GitHub response headers contain a NUL byte")
    text = headers.decode("iso-8859-1")
    locations = [
        line.split(":", 1)[1].strip()
        for line in text.splitlines()
        if line.lower().startswith("location:")
    ]
    if len(locations) != 1:
        raise RedirectError("expected exactly one GitHub Location header")
    return locations[0]


def fallback_url(location: str) -> str:
    if any(value in location for value in ('"', "\\", "\r", "\n", "\x00")):
        raise RedirectError("unsafe character in GitHub redirect")
    parts = urlsplit(location)
    try:
        port = parts.port
    except ValueError as exc:
        raise RedirectError("invalid port in GitHub redirect") from exc
    if (
        parts.scheme != "https"
        or parts.hostname != SOURCE_HOST
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.fragment
    ):
        raise RedirectError("unexpected GitHub release redirect origin")
    if not parts.path.startswith(SOURCE_PATH_PREFIX):
        raise RedirectError("unexpected GitHub release redirect path")
    query = parse_qs(parts.query, keep_blank_values=True)
    if not query.get("sig") or not query.get("se"):
        raise RedirectError("GitHub release redirect is not a signed storage URL")
    return urlunsplit(("https", FALLBACK_HOST, parts.path, parts.query, ""))


def write_curl_config(headers_path: Path, output_path: Path) -> None:
    location = _location_from_headers(headers_path.read_bytes())
    target = fallback_url(location)
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
        handle.write(f'url = "{target}"\n')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("headers", type=Path)
    parser.add_argument("curl_config", type=Path)
    args = parser.parse_args()
    try:
        write_curl_config(args.headers, args.curl_config)
    except (OSError, RedirectError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
