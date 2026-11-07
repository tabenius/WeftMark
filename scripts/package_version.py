#!/usr/bin/env python3
"""Print the Debian-compatible package version for the installed WeftMark release.

PEP 440 prereleases such as ``0.1.0a1`` are valid Python versions but are not
accepted by the Linux packaging scripts. Debian sorts prereleases before the
final release with a ``~`` separator, so ``0.1.0a1`` becomes ``0.1.0~a1``.
"""
from __future__ import annotations

import importlib.metadata
import re
import sys

_PRERELEASE = re.compile(r"^(?P<release>\d+\.\d+\.\d+)(?P<stage>a|b|rc)(?P<number>\d+)$")
_FINAL = re.compile(r"^\d+\.\d+\.\d+$")


def debian_version(version: str) -> str:
    """Return a Debian package version for a supported PEP 440 release."""
    if _FINAL.fullmatch(version):
        return version
    match = _PRERELEASE.fullmatch(version)
    if match is None:
        raise ValueError(f"unsupported package version: {version}")
    return f"{match.group('release')}~{match.group('stage')}{match.group('number')}"


def main() -> int:
    try:
        print(debian_version(importlib.metadata.version("weftmark")))
    except (importlib.metadata.PackageNotFoundError, ValueError) as exc:
        print(f"Invalid version: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
