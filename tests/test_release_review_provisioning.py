from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


transport = load('provision_release_review')
ceremony = load('setup_release_review_key')
HEAD = 'a' * 40
TAG = 'v0.0.1'
NOW = datetime.now(timezone.utc)


def receipt():
    return {'schema':transport.SCHEMA,'repository':transport.REPOSITORY,'head_sha':HEAD,'tag':TAG,
            'created_at':NOW.isoformat(),'expires_at':(NOW+timedelta(days=1)).isoformat(),
            'review':{'change_set_id':'alpha-release-evidence-cs','review_id':'native-review-1',
                      'head_sha':HEAD,'outcome':'ready','is_releasable':True}}


@pytest.fixture
def signer(tmp_path):
    if not shutil.which('ssh-keygen'):
        pytest.skip('OpenSSH tools unavailable')
    key = tmp_path / 'disposable-test-key'
    subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)],check=True,capture_output=True)
    public = ' '.join(key.with_suffix('.pub').read_text().split()[:2])
    fingerprint = subprocess.check_output(['ssh-keygen','-lf',str(key)+'.pub','-E','sha256'],text=True).split()[1]
    policy = {'schema':'weftmark.release-review-policy.v1','principal':'weftmark-release-review',
              'namespace':transport.NAMESPACE,'fingerprint':fingerprint,'public_key':public}
    path = tmp_path / 'policy.json'; path.write_text(json.dumps(policy))
    return key, path


def signed_files(tmp_path, key, namespace=transport.NAMESPACE):
    path = tmp_path / 'review.json'; path.write_text(json.dumps(receipt()))
    signature = tmp_path / 'review.json.sig'
    result = subprocess.run(['ssh-keygen','-Y','sign','-f',str(key),'-n',namespace,'-'],
                            input=path.read_bytes(),capture_output=True,check=True)
    signature.write_bytes(result.stdout)
    return path, signature


def test_real_detached_signature_authenticates_only_the_exact_receipt(tmp_path, signer):
    key, policy = signer
    path, signature = signed_files(tmp_path,key)
    value, provenance = transport.verify(path,signature,policy,HEAD,TAG,NOW)
    assert value['review']['review_id'] == 'native-review-1'
    assert provenance['fingerprint'].startswith('SHA256:')
    path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ValueError,match='signature'):
        transport.verify(path,signature,policy,HEAD,TAG,NOW)


def test_wrong_namespace_key_pin_and_unsigned_receipts_are_refused(tmp_path, signer):
    key, policy = signer
    path, signature = signed_files(tmp_path,key,'another-protocol')
    with pytest.raises(ValueError,match='signature'):
        transport.verify(path,signature,policy,HEAD,TAG,NOW)
    value = json.loads(policy.read_text());value['fingerprint']='SHA256:wrong';policy.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='fingerprint'):
        transport.verify(path,signature,policy,HEAD,TAG,NOW)
    signature.unlink()
    with pytest.raises(ValueError,match='regular file'):
        transport.verify(path,signature,policy,HEAD,TAG,NOW)


@pytest.mark.parametrize('change', [
    {'head_sha':'b'*40}, {'tag':'v0.0.2'}, {'repository':'attacker/project'}, {'schema':'future'},
    {'created_at':(NOW+timedelta(hours=1)).isoformat()},
    {'expires_at':(NOW-timedelta(seconds=1)).isoformat()},
    {'expires_at':(NOW+timedelta(days=8)).isoformat()},
    {'created_at':'2026-11-25T01:00:00'}, {'extra':'untrusted'},
])
def test_stale_cross_source_and_malformed_receipts_fail_closed(change):
    data=receipt();data.update(change)
    with pytest.raises(ValueError):transport.validate(data,HEAD,TAG,NOW)


def test_review_is_never_implicitly_upgraded_or_self_approved():
    for field,value in [('is_releasable',1),('outcome','ready_with_follow_up'),('head_sha','b'*40)]:
        data=receipt();data['review'][field]=value
        with pytest.raises(ValueError):transport.validate(data,HEAD,TAG,NOW)
    data=receipt();data['review']=None
    with pytest.raises(ValueError):transport.validate(data,HEAD,TAG,NOW)


def test_unenrolled_policy_and_unencrypted_production_key_are_refused(tmp_path, signer):
    unconfigured = tmp_path/'unconfigured-policy.json'
    policy = json.loads(signer[1].read_text())
    policy.update(public_key=None,fingerprint=None)
    unconfigured.write_text(json.dumps(policy))
    with pytest.raises(ValueError,match='not been enrolled'):
        transport.load_policy(unconfigured)
    with pytest.raises(ValueError,match='passphrase'):
        ceremony.require_encrypted_key(signer[0])


def test_public_key_comments_from_openssh_do_not_break_ceremony(signer):
    public = signer[0].with_suffix('.pub').read_text().strip()
    assert ceremony.normalize_public_key(public+' owner label') == ' '.join(public.split()[:2])
    assert transport.load_policy(ROOT/'scripts/release_review_policy.json')['fingerprint'] == 'SHA256:OyhH3b8uVIdi6uoBnefjxhznTJOTjLi6RFSjp4faRw0'


def test_hosted_workflows_do_not_receive_private_keys_or_auto_approve():
    intake=(ROOT/'.github/workflows/release-review-intake.yml').read_text()
    release=(ROOT/'.github/workflows/release.yml').read_text()
    assert 'secrets.' not in intake
    assert 'workflow_dispatch' in intake and "github.ref_type == 'tag'" in intake
    assert "'path':'.github/workflows/release-review-intake.yml'" in release
    assert 'review_run_id:' in release
    assert 'provision_release_review.py apply' in release
    assert 'ssh-keygen -Y sign' not in release
    assert 'review create' not in intake
    # Explicit job permissions disable unspecified scopes; gh run/download
    # needs Actions read, not signing keys or Actions write authority.
    assert yaml.safe_load(release)['jobs']['release']['permissions']['actions'] == 'read'
