#!/usr/bin/env python3
"""Transport a signed minimal native review; no private ledger or signing key in CI."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 'weftmark.release-review.v1'
REPOSITORY = 'tabenius/WeftMark'
NAMESPACE = 'weftmark-release-review-v1'
MAX_RECEIPT = 128 * 1024


def bounded(path, maximum):
    if path.is_symlink() or not path.is_file():
        raise ValueError('review material must be a regular file, not a symlink')
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('review material exceeds size limit')
    return raw


def load_policy(path):
    policy = json.loads(bounded(path, 8192))
    if policy.get('schema') != 'weftmark.release-review-policy.v1' or policy.get('namespace') != NAMESPACE:
        raise ValueError('unsupported review signing policy')
    principal = policy.get('principal')
    if not isinstance(principal, str) or not re.fullmatch(r'[a-zA-Z0-9_.@-]{1,128}', principal):
        raise ValueError('invalid pinned review principal')
    public = policy.get('public_key')
    if not isinstance(public, str) or not isinstance(policy.get('fingerprint'), str):
        raise ValueError('dedicated review key has not been enrolled')
    parts = public.split()
    if len(parts) != 2 or parts[0] != 'ssh-ed25519' or '\n' in public:
        raise ValueError('expected one Ed25519 public key')
    blob = base64.b64decode(parts[1], validate=True)
    if not blob.startswith(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20') or len(blob) != 51:
        raise ValueError('invalid Ed25519 public key encoding')
    fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')
    if fingerprint != policy['fingerprint']:
        raise ValueError('public key does not match the independently enrolled fingerprint')
    return policy


def validate(receipt, head, tag, now=None):
    now = now or datetime.now(timezone.utc)
    if not re.fullmatch(r'[0-9a-f]{40}', head) or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag):
        raise ValueError('review needs an exact source head and semantic-version tag')
    if not isinstance(receipt, dict) or set(receipt) != {'schema','repository','head_sha','tag','created_at','expires_at','review'}:
        raise ValueError('unsupported review receipt fields')
    if receipt['schema'] != SCHEMA or receipt['repository'] != REPOSITORY or receipt['head_sha'] != head or receipt['tag'] != tag:
        raise ValueError('review receipt repository, schema, source head or tag mismatch')
    created = datetime.fromisoformat(receipt['created_at'].replace('Z', '+00:00'))
    expires = datetime.fromisoformat(receipt['expires_at'].replace('Z', '+00:00'))
    if created.tzinfo is None or expires.tzinfo is None or created > now + timedelta(minutes=5) or not created < expires or expires <= now or expires - created > timedelta(days=7):
        raise ValueError('review receipt time bounds are invalid or expired')
    review = receipt['review']
    expected = {'change_set_id','review_id','outcome','head_sha','is_releasable'}
    if not isinstance(review, dict) or set(review) != expected:
        raise ValueError('native release review is missing or malformed')
    if review['change_set_id'] != 'alpha-release-evidence-cs' or review['head_sha'] != head or review['outcome'] != 'ready' or review['is_releasable'] is not True or not isinstance(review['review_id'], str) or not review['review_id'].strip():
        raise ValueError('native release review is not ready at this exact head')
    return review


def verify(receipt_path, signature_path, policy_path, head, tag, now=None):
    raw = bounded(receipt_path, MAX_RECEIPT)
    signature = bounded(signature_path, 16384)
    policy = load_policy(policy_path)  # Never take trust policy from an artifact.
    with tempfile.TemporaryDirectory(prefix='weftmark-review-verify-') as directory:
        allowed = Path(directory) / 'allowed_signers'
        detached = Path(directory) / 'review.sig'
        detached.write_bytes(signature)
        allowed.write_text(f'{policy["principal"]} namespaces="{NAMESPACE}" {policy["public_key"]}\n')
        result = subprocess.run(['ssh-keygen','-Y','verify','-f',str(allowed),'-I',policy['principal'],
                                 '-n',NAMESPACE,'-s',str(detached)], input=raw, capture_output=True, timeout=10)
    if result.returncode:
        raise ValueError('review signature is invalid for the enrolled key, principal or namespace')
    receipt = json.loads(raw)
    validate(receipt, head, tag, now)
    return receipt, {'schema':'weftmark.release-review-provisioning.v1','sha256':hashlib.sha256(raw).hexdigest(),
                     'fingerprint':policy['fingerprint'],'principal':policy['principal'],'tag':tag}


def export(repo, output, tag):
    # Existing native read model is authority; do not manufacture review outcomes.
    from build_release_evidence import _release_review
    head = subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'], text=True).strip()
    tagged = subprocess.check_output(['git','-C',str(repo),'rev-parse',f'refs/tags/{tag}^{{commit}}'], text=True).strip()
    if tagged != head or subprocess.check_output(['git','-C',str(repo),'status','--porcelain'],text=True).strip():
        raise ValueError('export requires clean source at the exact existing version tag')
    now = datetime.now(timezone.utc)
    receipt = {'schema':SCHEMA,'repository':REPOSITORY,'head_sha':head,'tag':tag,
               'created_at':now.isoformat(),'expires_at':(now+timedelta(days=7)).isoformat(),'review':_release_review(str(repo))}
    validate(receipt, head, tag, now)
    with output.open('x') as stream:
        stream.write(json.dumps(receipt,sort_keys=True,indent=2)+'\n')
    print(f'Exported native review only; explicitly sign {output} locally using namespace {NAMESPACE}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command',required=True)
    exporter = commands.add_parser('export')
    exporter.add_argument('--repo',type=Path,default=ROOT)
    exporter.add_argument('--output',type=Path,required=True)
    exporter.add_argument('--tag',required=True)
    for name in ('verify','apply'):
        command = commands.add_parser(name)
        command.add_argument('--receipt',type=Path,required=True)
        command.add_argument('--signature',type=Path,required=True)
        command.add_argument('--policy',type=Path,default=ROOT/'scripts/release_review_policy.json')
        command.add_argument('--head',required=True)
        command.add_argument('--tag',required=True)
        if name == 'apply': command.add_argument('--bundle',type=Path,required=True)
    args = parser.parse_args()
    try:
        if args.command == 'export':
            export(args.repo,args.output,args.tag)
            return
        receipt, provenance = verify(args.receipt,args.signature,args.policy,args.head,args.tag)
        if args.command == 'apply':
            bundle = json.loads(bounded(args.bundle,16*1024*1024))
            if bundle.get('schema') != 'weftmark.release-evidence.v0' or bundle.get('source_sha') != args.head:
                raise ValueError('fresh release evidence bundle source mismatch')
            if bundle.get('review') not in (None, receipt['review']):
                raise ValueError('existing native review differs; refusing to replace it')
            bundle.update(review=receipt['review'], review_provisioning=provenance)
            args.bundle.write_text(json.dumps(bundle,indent=2)+'\n')
        print(json.dumps({'ok':True,'provisioning':provenance}))
    except (ValueError,KeyError,TypeError,AttributeError,OSError,subprocess.SubprocessError) as error:
        parser.exit(1,f'Release review refused: {error}\n')


if __name__ == '__main__':
    main()
