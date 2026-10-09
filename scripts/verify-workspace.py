#!/usr/bin/env python3
"""Verify a workspace bundle against an out-of-band signer fingerprint."""

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess


def verify_bundle(bundle, expected_fingerprint, nostoi):
    bundle = Path(bundle)
    expected = expected_fingerprint.upper()
    result = subprocess.run(
        ["gpg", "--batch", "--no-auto-key-retrieve", "--status-fd=1", "--verify",
         str(bundle / "workspace.jsonl.attestation.sig"),
         str(bundle / "workspace.jsonl.attestation.json")],
        capture_output=True, text=True, check=True,
    )
    signatures = [line.split() for line in result.stdout.splitlines()
                  if line.startswith("[GNUPG:] VALIDSIG ")]
    # VALIDSIG identifies the signing key and (when present) its primary key.
    # Never take the claimed fingerprint in the JSON as proof of signer identity.
    if len(signatures) != 1:
        raise ValueError("expected exactly one valid GPG signature")
    status = signatures[0]
    actual = {status[2].upper()}
    if len(status) > 11:
        actual.add(status[11].upper())
    if expected not in actual:
        raise ValueError("actual GPG signer does not match the pinned fingerprint")
    att = json.loads((bundle / "workspace.jsonl.attestation.json").read_text())
    if att["fingerprint"].upper() != expected:
        raise ValueError("attestation fingerprint differs from the pinned identity")
    if att["format"] != "nostoi-v1":
        raise ValueError("unsupported chain format")
    chain = bundle / "workspace.jsonl"
    if not nostoi.verify(str(chain))["ok"]:
        raise ValueError("Nostoi chain verification failed")
    records = [json.loads(line) for line in chain.read_text().splitlines() if line]
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
                        help="Trusted fingerprint obtained independently of this bundle")
    parser.add_argument("--nostoi-python", required=True, type=Path,
                        help="Locally available trusted Nostoi reference implementation")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("nostoi_ref", args.nostoi_python)
    nostoi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nostoi)
    try:
        verify_bundle(args.bundle, args.expected_fingerprint, nostoi)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"verification failed: {exc}\n")
    print("GPG signer, signature, chain, signed digest and readable manifest verified")


if __name__ == "__main__":
    main()
