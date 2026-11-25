#!/usr/bin/env python3
"""Interactive local encrypted review-key ceremony. Never run on a CI runner."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys


def require_encrypted_key(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('private key must be a mode-0600 regular file')
    raw = path.read_text()
    body = base64.b64decode(''.join(raw.splitlines()[1:-1]), validate=True)
    magic = b'openssh-key-v1\x00'
    if not body.startswith(magic) or len(body) < len(magic) + 4:
        raise ValueError('expected an encrypted OpenSSH private key')
    size = struct.unpack('>I', body[len(magic):len(magic)+4])[0]
    cipher = body[len(magic)+4:len(magic)+4+size]
    if not cipher or cipher == b'none':
        raise ValueError('private key must have a passphrase; protect it locally with ssh-keygen -p')


def normalize_public_key(output):
    lines = output.strip().splitlines()
    parts = lines[0].split() if len(lines) == 1 else []
    if len(parts) < 2 or parts[0] != 'ssh-ed25519':
        raise ValueError('dedicated review key must be Ed25519')
    # OpenSSH may retain a comment after the two public-key fields. It is not
    # part of the key blob or fingerprint and must not make enrollment fail.
    return ' '.join(parts[:2])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key', type=Path, required=True)
    parser.add_argument('--policy-output', type=Path, required=True)
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error('run from your own interactive terminal; never send a passphrase in chat or argv')
    proposed = args.key.expanduser().absolute()
    if proposed.is_symlink():
        parser.error('private key must not be a symlink')
    key = proposed.resolve()
    repo = Path(__file__).resolve().parents[1]
    if key.is_relative_to(repo):
        parser.error('private key must be outside the repository')
    try:
        if args.policy_output.exists():
            raise ValueError('policy output already exists; refusing to overwrite enrollment')
        key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if key.parent.stat().st_mode & 0o077:
            raise ValueError('key directory must be private (mode 0700)')
        if not key.exists():
            if key.with_suffix(key.suffix + '.pub').exists():
                raise ValueError('public key already exists without matching private key; recover custody first')
            subprocess.run(['ssh-keygen', '-t', 'ed25519', '-a', '100', '-C', 'WeftMark release review', '-f', str(key)], check=True)
        require_encrypted_key(key)
        # Prompt locally again if necessary: derive, do not trust a stale .pub.
        public = normalize_public_key(subprocess.check_output(['ssh-keygen', '-y', '-f', str(key)], text=True))
        parts = public.split()
        fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(parts[1], validate=True)).digest()).decode().rstrip('=')
        policy = {'schema': 'weftmark.release-review-policy.v1', 'principal': 'weftmark-release-review',
                  'namespace': 'weftmark-release-review-v1', 'fingerprint': fingerprint, 'public_key': public}
        fd = os.open(args.policy_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, 'w') as stream:
            stream.write(json.dumps(policy, indent=2) + '\n')
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Review-key ceremony refused: {error}\n')
    print(f'Proposed public policy written; review and commit its fingerprint: {fingerprint}')
    print('Private key remains local. Make and test an encrypted independent recovery copy before relying on it.')


if __name__ == '__main__':
    main()
