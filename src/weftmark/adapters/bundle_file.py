"""Safe local file transport for portable WeftMark bundles."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


class BundleFileError(RuntimeError):
    """Raised when a portable bundle file cannot be read or written safely."""


def write_bundle(path: str | Path, bundle: Mapping[str, Any]) -> Path:
    target = Path(path).absolute()
    if target.is_symlink():
        raise BundleFileError("refusing to replace a bundle symlink")
    if target.exists():
        raise BundleFileError("refusing to overwrite an existing bundle")
    if target.parent.is_symlink():
        raise BundleFileError("refusing to use a symlinked bundle directory")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = json.dumps(bundle, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        os.chmod(target, 0o600)
    except (FileExistsError, OSError) as error:
        raise BundleFileError(f"cannot write bundle: {type(error).__name__}") from error
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return target


def read_bundle(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if source.is_symlink():
        raise BundleFileError("refusing to follow a bundle symlink")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BundleFileError(f"cannot read bundle: {type(error).__name__}") from error
    if not isinstance(value, dict):
        raise BundleFileError("bundle file must contain a JSON object")
    return value
