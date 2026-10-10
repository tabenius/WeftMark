#!/usr/bin/env python3
"""Verify a workspace bundle against out-of-band signer and root fingerprints."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile


PUBLIC_KEY_FILES = ("ragbaz-root.pub.asc", "box0-genesis.pub.asc")


def gpg(argv, env, want_output=False):
    result = subprocess.run(
        ["gpg", "--batch", "--no-auto-key-retrieve", *argv],
        capture_output=True, text=True, check=True, env=env,
    )
    return result.stdout if want_output else None


def actual_signer(stdout):
    """The fingerprint GPG says signed, never the one the JSON claims."""
    status_lines = [line.split() for line in stdout.splitlines()
                    if line.startswith("[GNUPG:] VALIDSIG ")]
    if len(status_lines) != 1:
        raise ValueError("expected exactly one valid GPG signature")
    status = status_lines[0]
    candidates = {status[2].upper()}
    # VALIDSIG names the signing key and, when present, its primary key.
    if len(status) > 11 and status[11]:
        candidates.add(status[11].upper())
    return candidates


def issuers_of_checked_certifications(stdout):
    """Fingerprints that cryptographically certified a key, local-only excluded."""
    issuers = set()
    for line in stdout.splitlines():
        fields = line.split(":")
        if fields[0] != "sig" or len(fields) < 13:
            continue
        # Field 1 is "!" only when GPG cryptographically checked the
        # certification. "L" is a local-only mark that would not travel.
        if fields[1] != "!" or not fields[12]:
            continue
        issuers.add(fields[12].upper())
    return issuers


def verify_bundle(bundle, expected_fingerprint, nostoi, expected_root_fingerprint):
    bundle = Path(bundle)
    expected = expected_fingerprint.upper()
    expected_root = expected_root_fingerprint.upper()
    for name in PUBLIC_KEY_FILES:
        if not (bundle / name).is_file():
            raise ValueError(f"public key file {name} is missing from the bundle")

    with tempfile.TemporaryDirectory(prefix="verify-workspace-") as gnupghome:
        env = {**os.environ, "GNUPGHOME": gnupghome}
        # Verify against an isolated keyring holding only what the delivery
        # claims, so the caller's trust database and network access are irrelevant.
        gpg(["--import", *[str(bundle / name) for name in PUBLIC_KEY_FILES]], env)

        # 1. The signature verifies, and it is made by the pinned signer.
        stdout = gpg(
            ["--status-fd=1", "--verify",
             str(bundle / "workspace.jsonl.attestation.sig"),
             str(bundle / "workspace.jsonl.attestation.json")],
            env, want_output=True,
        )
        if expected not in actual_signer(stdout):
            raise ValueError("actual GPG signer does not match the pinned fingerprint")

        # 2. The root the delivery supplies is the root that was pinned.
        try:
            listing = gpg(["--with-colons", "--fingerprint", expected_root],
                          env, want_output=True)
        except subprocess.CalledProcessError:
            raise ValueError("supplied organisation root key is not the pinned root") from None
        if not any(fields[9].upper() == expected_root
                  for line in listing.splitlines() if line.startswith("fpr:")
                  for fields in [line.split(":")] if len(fields) > 9):
            raise ValueError("supplied organisation root key is not the pinned root")

        # 3. That root certified the signing key cryptographically.
        certifications = gpg(
            ["--check-sigs", "--with-colons", "--keyid-format", "long", expected],
            env, want_output=True,
        )
        if expected_root not in issuers_of_checked_certifications(certifications):
            raise ValueError("pinned organisation root did not certify the signing key")

    att = json.loads((bundle / "workspace.jsonl.attestation.json").read_text())
    if att["fingerprint"].upper() != expected:
        raise ValueError("attestation fingerprint differs from the pinned identity")
    if att["format"] != "nostoi-v1":
        raise ValueError("unsupported chain format")
    chain = bundle / "workspace.jsonl"
    if not nostoi.verify(str(chain))["ok"]:
        raise ValueError("Nostoi chain verification failed")
    records = [json.loads(line) for line in chain.read_text().splitlines() if line]
    # The attestation names a sequence; compare against that record, not the first.
    matches = [record for record in records if record["seq"] == att["seq"]]
    if len(matches) != 1 or nostoi.digest(matches[0]) != att["digest"]:
        raise ValueError("signed sequence/digest is not bound to the chain record")
    manifest = json.loads((bundle / "manifest.json").read_text())
    if nostoi.canonical(manifest) != nostoi.canonical(matches[0]["body"]):
        raise ValueError("readable manifest differs from the signed chain body")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--expected-fingerprint", required=True,
                        help="Trusted box-signer fingerprint obtained independently")
    parser.add_argument("--expected-root-fingerprint", required=True,
                        help="Trusted organisation root fingerprint obtained independently")
    parser.add_argument("--nostoi-python", required=True, type=Path,
                        help="Locally available trusted Nostoi reference implementation")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("nostoi_ref", args.nostoi_python)
    nostoi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nostoi)
    try:
        verify_bundle(args.bundle, args.expected_fingerprint, nostoi,
                      args.expected_root_fingerprint)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"verification failed: {exc}\n")
    print("GPG signer, organisation delegation, chain, digest and manifest verified")


if __name__ == "__main__":
    main()
