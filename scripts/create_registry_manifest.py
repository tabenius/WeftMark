#!/usr/bin/env python3
"""Build a signed-release-ready manifest for existing CI-verified WeftMark assets."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def release_identity(repository_root, environ):
    tag = environ.get("RELEASE_TAG") or environ.get("GITHUB_REF_NAME", "")
    repo = environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag) or repo.lower() != "tabenius/weftmark":
        raise ValueError("Manifest creation requires an immutable WeftMark release tag")
    event_ref = environ.get("GITHUB_REF")
    if event_ref and event_ref != "refs/tags/" + tag:
        raise ValueError("Release signing must run on the selected tag ref, not a branch dispatch")
    try:
        def git(ref):
            return subprocess.check_output(["git", "rev-parse", "--verify", ref],
                cwd=repository_root, text=True, stderr=subprocess.DEVNULL).strip()
        commit = git("HEAD^{commit}")
        tagged = git("refs/tags/" + tag + "^{commit}")
    except subprocess.CalledProcessError as error:
        raise ValueError("Release tag must resolve to the checked-out commit") from error
    if not re.fullmatch("[0-9a-f]{40}", commit) or commit != tagged:
        raise ValueError("Release tag does not match the checked-out commit")
    if environ.get("GITHUB_SHA") and environ["GITHUB_SHA"] != commit:
        raise ValueError("Release event SHA differs from the checked-out tag commit")
    return tag, repo, commit


def create_manifest(root, *, repository_root, environ):
    tag, repo, commit = release_identity(repository_root, environ)
    files = []
    for path in sorted(root.iterdir()):
        if path.name in {"release.json", "release.sigstore.json"}:
            continue
        if not path.is_file() or path.is_symlink() or ".." in path.name or path.name.lower() in {"catalog.json", "release.json", "release.sigstore.json"} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", path.name):
            raise ValueError("Invalid release artifact")
        size = path.stat().st_size
        if size < 1 or size > 4 * 1024 ** 3:
            raise ValueError("Invalid artifact size")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        files.append({"name": path.name, "path": f"releases/weftmark/{tag[1:]}/multi/{path.name}",
            "sha256": digest.hexdigest(), "bytes": size})
    if len(files) < 3 or len(files) > 32:
        raise ValueError("Incomplete or excessive distribution artifacts")
    manifest = {"schema": "ragbaz.release/1", "repository": repo, "product": "weftmark",
        "version": tag[1:], "platform": "multi", "channel": "candidate", "sourceCommit": commit, "files": files}
    with (root / "release.json").open("x", encoding="utf8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    return manifest


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve()
    manifest = create_manifest(root, repository_root=Path(__file__).resolve().parents[1], environ=os.environ)
    print("Prepared signed-release manifest with", len(manifest["files"]), "verified assets")


if __name__ == "__main__":
    main()
