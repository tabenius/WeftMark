#!/usr/bin/env python3
"""Install only the signed Nostoi CLI artifact; no Rust build or system mutation.

Requires curl and cosign on PATH. All downloads are temporary, the output must
not already exist, and the release selection comes from this checkout's config.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


def release_config(root: Path = ROOT) -> dict:
    with (root / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)["tool"]["weftmark"]["nostoi-release"]
    if config.get("repository") != "tabenius/nostoi":
        raise ValueError("Unexpected Nostoi repository")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", config.get("version", "")):
        raise ValueError("Invalid Nostoi release version")
    if not re.fullmatch(r"[0-9a-f]{40}", config.get("source-commit", "")):
        raise ValueError("Invalid Nostoi source commit")
    if config.get("signing-workflow") != "registry-candidate.yml":
        raise ValueError("Unexpected Nostoi signing workflow")
    return config


def artifact_entry(manifest: dict, config: dict, name: str) -> dict:
    expected = {
        "schema": "ragbaz.release/1", "repository": config["repository"],
        "product": "nostoi", "version": config["version"],
        "sourceCommit": config["source-commit"], "platform": "linux-amd64",
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("Nostoi manifest identity/source mismatch")
    files = manifest.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 32:
        raise ValueError("Invalid Nostoi artifact inventory")
    names = [entry.get("name") for entry in files if isinstance(entry, dict)]
    if len(names) != len(files) or any(not isinstance(n, str) for n in names) or len(names) != len(set(names)):
        raise ValueError("Invalid or duplicate Nostoi artifact names")
    matches = [entry for entry in files if entry["name"] == name]
    if len(matches) != 1:
        raise ValueError("Selected Nostoi artifact is missing")
    entry = matches[0]
    if not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", "")):
        raise ValueError("Invalid Nostoi artifact digest")
    if type(entry.get("bytes")) is not int or not 0 < entry["bytes"] <= MAX_ARTIFACT_BYTES:
        raise ValueError("Invalid Nostoi artifact size")
    return entry


def verify_artifact(path: Path, entry: dict) -> None:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            if size > entry["bytes"]:
                raise ValueError("Nostoi artifact size mismatch")
            digest.update(chunk)
    if size != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
        raise ValueError("Nostoi artifact hash/size mismatch")


def download(url: str, path: Path, max_bytes: int) -> None:
    subprocess.run([
        "curl", "--fail", "--silent", "--show-error", "--location",
        "--proto", "=https", "--proto-redir", "=https", "--retry", "2",
        "--max-time", "90", "--max-filesize", str(max_bytes),
        url, "--output", str(path),
    ], check=True, timeout=300)
    if path.stat().st_size > max_bytes:
        raise ValueError("Oversized Nostoi download")


def install(output: Path) -> None:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("The released CLI artifact is Linux amd64 only")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Refusing to replace existing path: {output}")
    for tool in ("curl", "cosign"):
        if not shutil.which(tool):
            raise ValueError(f"{tool} is required for verified release installation")
    config = release_config()
    tag = "v" + config["version"]
    base = f"https://dl.ragbaz.cc/v1/nostoi/{tag}/"
    name = f"nostoi-{config['version']}-linux-amd64"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nostoi-verified-", dir=output.parent) as temporary:
        root = Path(temporary)
        manifest = root / "release.json"
        bundle = root / "release.sigstore.json"
        download(base + manifest.name, manifest, 256 * 1024)
        download(base + bundle.name, bundle, 256 * 1024)
        identity = f"https://github.com/{config['repository']}/.github/workflows/{config['signing-workflow']}@refs/tags/{tag}"
        subprocess.run([
            "cosign", "verify-blob", "--bundle", str(bundle),
            "--certificate-identity", identity,
            "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com",
            str(manifest),
        ], check=True, timeout=120)
        entry = artifact_entry(json.loads(manifest.read_text()), config, name)
        binary = root / name
        download(base + name, binary, entry["bytes"])
        verify_artifact(binary, entry)
        binary.chmod(0o755)
        # Atomic create-only publication; never replace an existing path.
        os.link(binary, output)
    print(f"Installed verified Nostoi {config['version']} to {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    install(args.output.absolute())


if __name__ == "__main__":
    main()
