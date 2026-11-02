#!/usr/bin/env python3
"""Build a signed-release-ready manifest for existing CI-verified WeftMark assets."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys

root = Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve()
tag = os.environ.get("GITHUB_REF_NAME", "")
repo = os.environ.get("GITHUB_REPOSITORY", "")
commit = os.environ.get("GITHUB_SHA", "")
if not re.fullmatch(r"v\d+\.\d+\.\d+", tag) or repo.lower() != "tabenius/weftmark" or not re.fullmatch("[0-9a-f]{40}", commit):
    raise SystemExit("Manifest creation requires a tagged WeftMark GitHub Actions build")
files = []
for path in sorted(root.iterdir()):
    if path.name in {"release.json", "release.sigstore.json"}:
        continue
    if not path.is_file() or path.is_symlink() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", path.name):
        raise SystemExit("Invalid release artifact")
    size = path.stat().st_size
    if size < 1 or size > 4 * 1024 ** 3:
        raise SystemExit("Invalid artifact size")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    files.append({"name": path.name, "path": f"releases/weftmark/{tag[1:]}/multi/{path.name}", "sha256": digest.hexdigest(), "bytes": size})
if len(files) < 3 or len(files) > 32:
    raise SystemExit("Incomplete or excessive distribution artifacts")
manifest = {"schema": "ragbaz.release/1", "repository": repo, "product": "weftmark", "version": tag[1:], "platform": "multi", "channel": "candidate", "sourceCommit": commit, "files": files}
with (root / "release.json").open("x", encoding="utf8") as handle:
    json.dump(manifest, handle, indent=2)
    handle.write("\n")
print("Prepared signed-release manifest with", len(files), "verified assets")
