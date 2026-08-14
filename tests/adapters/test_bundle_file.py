from __future__ import annotations

import os
from pathlib import Path

import pytest

from weftmark.adapters.bundle_file import BundleFileError, read_bundle, write_bundle


BUNDLE = {"schema_version": 1, "digest": "sha256:test", "contents": {}}


def test_bundle_file_round_trip_is_private_and_refuses_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "exports" / "change.weftmark.json"
    assert write_bundle(path, BUNDLE) == path
    assert read_bundle(path) == BUNDLE
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(BundleFileError, match="overwrite"):
        write_bundle(path, BUNDLE)


def test_bundle_file_refuses_symlinks_and_invalid_json(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    os.symlink(target, link)
    with pytest.raises(BundleFileError, match="symlink"):
        read_bundle(link)
    with pytest.raises(BundleFileError, match="symlink"):
        write_bundle(link, BUNDLE)

    invalid = tmp_path / "invalid.json"
    invalid.write_text("not-json", encoding="utf-8")
    with pytest.raises(BundleFileError, match="cannot read"):
        read_bundle(invalid)
