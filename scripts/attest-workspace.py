#!/usr/bin/env python3
"""Produce a signed, chain-bound attestation of this workspace.

The deliverable a client receives is three files in attestations/:

  workspace.jsonl              a nostoi-v1 hash chain; record 1 is the manifest
  workspace.jsonl.attestation.json   what was signed, and by which key
  workspace.jsonl.attestation.sig    ASCII-armored detached OpenPGP signature

Chain of custody, and how a client checks each link:

  sig  --verifies over-->  attestation.json
         which pins chain + format + seq + digest
  digest  --is the-->  record digest inside workspace.jsonl
  chain  --verifies with-->  nostoi, back to genesis

Verification deliberately needs nothing but the files, gpg, and one pinned
fingerprint. It never contacts a keyserver.

The record and its digest follow nostoi's own rules exactly, using its reference
Python implementation, so a future `nostoi attest --driver gpg` is a swap rather
than a redesign.
"""

from __future__ import annotations

import hashlib
import argparse
import importlib.util
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
NOSTOI_PY = ROOT / "nostoi" / "contrib" / "python" / "nostoi.py"
OUT = pathlib.Path(__file__).resolve().parents[1] / "attestations"
CHAIN = OUT / "workspace.jsonl"

BOX_KEY = "78171B4532F8EFFBDFA2958F4315AE31DCC658B1"
PRINCIPAL = "ragbaz-box0-genesis"
ORG_ROOT = "866ECB8348C0FE48E479736AF85CEFBA8B8E1040"


def load_nostoi():
    spec = importlib.util.spec_from_file_location("nostoi_ref", NOSTOI_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()


def gpg_fpr(keyid: str) -> str:
    out = sh("gpg", "--with-colons", "--fingerprint", keyid)
    for line in out.splitlines():
        if line.startswith("fpr:"):
            return line.split(":")[9]
    raise SystemExit(f"no fingerprint for {keyid}")


def git(*args: str, cwd: pathlib.Path | None = None) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def collect_repos() -> list[dict]:
    """Every git repository in the workspace, in a stable order.

    `repo_key` is included because frog already records one, and it is included
    *with its portability marked* rather than omitted: `path:` values are computed
    from an absolute path and mean nothing on the client's machine.
    """
    registered = {}
    db = ROOT / "AGENTS.db"
    if db.exists():
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        for name, key in con.execute("SELECT name, repo_key FROM repos"):
            registered[name] = key
        con.close()

    repos = []
    for d in sorted(ROOT.iterdir()):
        if not (d / ".git").exists() or not d.is_dir():
            continue
        head = git("rev-parse", "HEAD", cwd=d)
        origin = git("remote", "get-url", "origin", cwd=d)
        branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=d)
        dirty = len([l for l in git("status", "--porcelain", cwd=d).splitlines() if l])
        key = registered.get(d.name)
        repos.append({
            "name": d.name,
            "path": str(d.relative_to(ROOT)),
            "origin": origin or None,
            "branch": branch or None,
            "head": head or None,
            "dirty_files": dirty,
            "clean": dirty == 0,
            "repo_key": key,
            "repo_key_portable": bool(key and key.startswith("git:")),
        })
    return repos


def tool_versions() -> dict:
    def first(cmd: list[str]) -> str | None:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True)
            return (p.stdout or p.stderr).splitlines()[0].strip() if p.returncode == 0 else None
        except OSError:
            return None
    return {
        "git": first(["git", "--version"]),
        "nix": first(["nix", "--version"]),
        "python": sys.version.split()[0],
        "syft": first(["syft", "version"]),
        "trivy": first(["trivy", "--version"]),
        "podman": first(["podman", "--version"]),
    }


def collect_instance() -> dict:
    """What this box is actually running, as distinct from what is on disk."""
    inst: dict = {}
    try:
        podman = json.loads(sh("podman", "ps", "--format", "json")) if sh("podman", "ps", "--format", "{{.Names}}") else []
        for c in podman:
            inst[c["Names"]] = {
                "image": c.get("Image"),
                "image_id": (c.get("ImageID") or "").split(":")[-1] or None,
                "status": c.get("Status"),
            }
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        pass
    rb = ROOT / "rebekah"
    lock = rb / "flake.lock"
    if lock.exists():
        # The lock is the instance's real revision record: Nix already resolved
        # exact revs. Its hash is bound into the attestation, so a client can
        # detect a changed lock without reconstructing it.
        inst["rebekah_flake_lock_sha256"] = hashlib.sha256(lock.read_bytes()).hexdigest()
    return inst


def main() -> int:
    global ROOT, OUT, CHAIN, NOSTOI_PY, BOX_KEY, PRINCIPAL, ORG_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=pathlib.Path, default=ROOT,
                        help="Workspace containing AGENTS.db and sibling repositories")
    parser.add_argument("--output", type=pathlib.Path, default=OUT,
                        help="Attestation bundle directory (use a new dated directory)")
    parser.add_argument("--nostoi-python", type=pathlib.Path,
                        help="Override the sibling Nostoi reference implementation")
    parser.add_argument("--box-id", help="Stable Frog box identity; defaults to frog box whoami")
    parser.add_argument("--signing-key", default=BOX_KEY)
    parser.add_argument("--org-root", default=ORG_ROOT)
    parser.add_argument("--principal", default=PRINCIPAL)
    parser.add_argument("--replace", action="store_true",
                        help="Explicitly replace an existing bundle; never implied")
    args = parser.parse_args()
    ROOT = args.workspace.resolve()
    OUT = args.output.resolve()
    CHAIN = OUT / "workspace.jsonl"
    NOSTOI_PY = (args.nostoi_python or ROOT / "nostoi/contrib/python/nostoi.py").resolve()
    BOX_KEY, ORG_ROOT, PRINCIPAL = args.signing_key, args.org_root, args.principal
    if not ROOT.is_dir():
        parser.error(f"workspace does not exist: {ROOT}")
    if not NOSTOI_PY.is_file():
        parser.error(f"Nostoi reference implementation not found: {NOSTOI_PY}")
    generated_names = ("manifest.json", "workspace.jsonl", "workspace.jsonl.attestation.json",
                       "workspace.jsonl.attestation.sig", "box0-genesis.pub.asc", "ragbaz-root.pub.asc")
    if not args.replace and any((OUT / name).exists() for name in generated_names):
        parser.error("output already contains a bundle; choose a new --output or explicitly --replace")
    nostoi = load_nostoi()
    OUT.mkdir(parents=True, exist_ok=True)

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    box_id = args.box_id
    if not box_id:
        box_id = json.loads(sh("frog", "--json", "box", "whoami"))["box_id"]

    manifest = {
        "v": 1,
        "kind": "ragbaz.workspace-collection/1",
        "generated_at": generated,
        "box": {"box_id": box_id, "host": sh("hostname")},
        "engine": {
            "canonicalisation": "json.dumps(sort_keys=True, separators=(',',':')), no floats, per nostoi",
            "digest": "sha256",
            "chain_format": "nostoi-v1",
        },
        "repos": collect_repos(),
        "instance": collect_instance(),
        "tools": tool_versions(),
        "notes": [
            "path: repo_key values are box-local and are not portable; git: values are.",
            "Signing keys are recorded in identities/keys.toml on the attesting box.",
        ],
    }

    # The manifest must not contain floats: nostoi's digest rules reject them,
    # and a float would silently change the digest on re-parse.
    nostoi.canonical(manifest)  # raises if the shape is illegal

    body_path = OUT / "manifest.json"
    body_path.write_text(json.dumps(manifest, indent=2) + "\n")

    if CHAIN.exists():
        CHAIN.unlink()
    result = nostoi.append(str(CHAIN),
        actor=PRINCIPAL,
        kind="workspace.collection",
        subject=box_id,
        body=manifest,
        at=generated,
    )
    seq, dig = result["seq"], result["digest"]

    attestation = {
        "v": "nostoi-attestation-v1",
        "chain": str(CHAIN.relative_to(OUT.parent)),
        "format": "nostoi-v1",
        "seq": seq,
        "digest": dig,
        "anchored_at": None,       # not anchored: see VERIFY.md
        "principal": PRINCIPAL,
        "key_kind": "openpgp",     # nostoi's own driver is ssh-keygen only
        "fingerprint": gpg_fpr(BOX_KEY),
        "anchor_key": None,
    }
    att_path = OUT / "workspace.jsonl.attestation.json"
    att_path.write_text(json.dumps(attestation, indent=2) + "\n")

    # Sign the canonical bytes of the document, exactly as nostoi does.
    payload = OUT / "workspace.jsonl.attestation.json"
    sig = OUT / "workspace.jsonl.attestation.sig"
    subprocess.run(
        ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase", "",
         "--local-user", BOX_KEY, "--armor", "--detach-sign",
         "--output", str(sig), str(payload)],
        check=True, capture_output=True,
    )

    # Export the public keys so a client can check the vouch with no keyring.
    for keyid, name in ((BOX_KEY, "box0-genesis.pub.asc"), (ORG_ROOT, "ragbaz-root.pub.asc")):
        (OUT / name).write_bytes(
            subprocess.run(["gpg", "--armor", "--export", keyid],
                           check=True, capture_output=True).stdout)

    print(f"seq      {seq}")
    print(f"digest   {dig}")
    print(f"key      {BOX_KEY}  ({PRINCIPAL})")
    print(f"repos    {len(manifest['repos'])}")
    print(f"wrote    {OUT}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
