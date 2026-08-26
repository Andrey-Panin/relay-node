#!/usr/bin/env python3
"""Verify that a checkout contains only the reviewed public release files."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import sys
from pathlib import Path, PurePosixPath


class TreeError(RuntimeError):
    pass


def _load_allowlist(root: Path) -> list[str]:
    try:
        lines = (root / "PUBLIC_FILES.txt").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TreeError("PUBLIC_FILES.txt is missing or unreadable") from exc
    if not lines or lines != sorted(set(lines)):
        raise TreeError("PUBLIC_FILES.txt must be non-empty, unique, and sorted")
    for value in lines:
        path = PurePosixPath(value)
        if (
            not value
            or value.startswith("/")
            or "\\" in value
            or path.is_absolute()
            or ".." in path.parts
            or ".git" in path.parts
            or ".deployment-backups" in path.parts
        ):
            raise TreeError(f"unsafe public allowlist entry: {value!r}")
    return lines


def _actual_files(root: Path) -> list[str]:
    result: list[str] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if path.is_symlink():
            raise TreeError(f"public checkout contains a symlink: {relative.as_posix()}")
        if path.is_file():
            if path.stat().st_size > 2 * 1024 * 1024:
                raise TreeError(f"public release file exceeds 2 MiB: {relative.as_posix()}")
            result.append(relative.as_posix())
    return sorted(result)


def _load_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = (root / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise TreeError("SHA256SUMS is missing or unreadable") from exc
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)", line)
        if not match or match.group(2) in result:
            raise TreeError("SHA256SUMS contains an invalid or duplicate entry")
        result[match.group(2)] = match.group(1)
    return result


def check(root: Path) -> None:
    root = root.resolve(strict=True)
    allowed = _load_allowlist(root)
    actual = _actual_files(root)
    if allowed != actual:
        missing = sorted(set(allowed) - set(actual))
        unexpected = sorted(set(actual) - set(allowed))
        raise TreeError(f"public tree mismatch: missing={missing}, unexpected={unexpected}")
    expected_hashed = set(allowed) - {"SHA256SUMS"}
    hashes = _load_hashes(root)
    if set(hashes) != expected_hashed:
        raise TreeError("SHA256SUMS file set does not match PUBLIC_FILES.txt")
    for relative, expected in hashes.items():
        digest = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        if not hmac.compare_digest(digest, expected):
            raise TreeError(f"checksum mismatch: {relative}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    try:
        check(args.root)
    except (OSError, TreeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("PUBLIC_TREE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
