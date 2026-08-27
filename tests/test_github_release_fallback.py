from __future__ import annotations

import os
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.github_release_fallback import (
    MAX_HEADER_BYTES,
    RedirectError,
    _location_from_headers,
    fallback_url,
    write_curl_config,
)


SIGNED_LOCATION = (
    "https://release-assets.githubusercontent.com/github-production-release-asset/"
    "123/example.tar.gz?sp=r&se=2026-08-28T12%3A00%3A00Z&sig=fake%2Bsignature"
)


def test_fallback_rewrites_only_the_official_storage_hostname(tmp_path: Path):
    headers = tmp_path / "headers"
    config = tmp_path / "curl.conf"
    headers.write_bytes(f"HTTP/2 302\r\nLocation: {SIGNED_LOCATION}\r\n\r\n".encode("ascii"))

    write_curl_config(headers, config)

    line = config.read_text(encoding="ascii").strip()
    assert line.startswith('url = "https://releaseassetproduction.blob.core.windows.net/')
    target = line.removeprefix('url = "').removesuffix('"')
    source_parts = urlsplit(SIGNED_LOCATION)
    target_parts = urlsplit(target)
    assert target_parts.path == source_parts.path
    assert parse_qs(target_parts.query) == parse_qs(source_parts.query)
    if os.name == "posix":
        assert stat.S_IMODE(config.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "location",
    (
        "https://example.com/github-production-release-asset/123/a?se=x&sig=y",
        "http://release-assets.githubusercontent.com/github-production-release-asset/123/a?se=x&sig=y",
        "https://user@release-assets.githubusercontent.com/github-production-release-asset/123/a?se=x&sig=y",
        "https://release-assets.githubusercontent.com/other/123/a?se=x&sig=y",
        "https://release-assets.githubusercontent.com/github-production-release-asset/123/a?se=x",
        'https://release-assets.githubusercontent.com/github-production-release-asset/123/a?se=x&sig="bad"',
    ),
)
def test_fallback_rejects_unexpected_or_unsigned_redirects(location: str):
    with pytest.raises(RedirectError):
        fallback_url(location)


def test_fallback_rejects_ambiguous_location_headers(tmp_path: Path):
    headers = tmp_path / "headers"
    config = tmp_path / "curl.conf"
    headers.write_text(
        f"HTTP/2 302\nLocation: {SIGNED_LOCATION}\nLocation: {SIGNED_LOCATION}\n",
        encoding="ascii",
    )
    with pytest.raises(RedirectError, match="exactly one"):
        write_curl_config(headers, config)
    assert not config.exists()


def test_fallback_rejects_oversized_or_nul_headers():
    with pytest.raises(RedirectError, match="safety limit"):
        _location_from_headers(b"x" * (MAX_HEADER_BYTES + 1))
    with pytest.raises(RedirectError, match="NUL"):
        _location_from_headers(b"HTTP/2 302\r\nLocation: https://example.test/\x00\r\n")


def test_fallback_rejects_an_invalid_port():
    location = (
        "https://release-assets.githubusercontent.com:not-a-port/"
        "github-production-release-asset/123/a?se=x&sig=y"
    )
    with pytest.raises(RedirectError, match="invalid port"):
        fallback_url(location)


def test_fallback_refuses_to_overwrite_an_existing_curl_config(tmp_path: Path):
    headers = tmp_path / "headers"
    config = tmp_path / "curl.conf"
    headers.write_bytes(f"HTTP/2 302\r\nLocation: {SIGNED_LOCATION}\r\n\r\n".encode("ascii"))
    config.write_text("do not replace\n", encoding="ascii")

    with pytest.raises(FileExistsError):
        write_curl_config(headers, config)
    assert config.read_text(encoding="ascii") == "do not replace\n"
