"""Relocation must preserve historical evidence and collect the selected workspace."""

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "attest-workspace.py"


def test_historical_signed_bytes_survive_relocation():
    expected = {
        "manifest.json": "3c0ae0967dcb28b48ad9fda73c3a38662f3326669f0bcf72fd3e3b5b9c03ad4e",
        "workspace.jsonl": "8ea2f20a6743f4149fcbfd4819db8431d188549cc62bca0f83a2ca1bdd9a1d3e",
        "workspace.jsonl.attestation.json": "bde1285fc0b7f718cc256a60640f009680011e9e3ba0b90b89c7a6308928fb0b",
        "workspace.jsonl.attestation.sig": "2713eb5710480d7280ab7eddaee9cefa4e9ad064495f3715d43fa9624bda59bf",
        "box0-genesis.pub.asc": "dd7954789727259abb0f8e298cff22f580d50ba13caad4eeeaf682c646f329cd",
        "ragbaz-root.pub.asc": "d9ed0513548f9046dc35dd9d1e466c332dcc7a5d8fbdd46d49cacc1fe90f1936",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((ROOT / "attestations" / name).read_bytes()).hexdigest() == digest


def test_existing_bundle_is_refused_without_modifying_it():
    before = (ROOT / "attestations" / "workspace.jsonl").read_bytes()
    result = subprocess.run(
        ["python3", str(SCRIPT), "--workspace", str(ROOT),
         "--nostoi-python", str(SCRIPT), "--output", str(ROOT / "attestations")],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "output already contains a bundle" in result.stderr
    assert (ROOT / "attestations" / "workspace.jsonl").read_bytes() == before


def test_collector_uses_selected_workspace(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("workspace_attestation", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / "selected-repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "fixture"],
                   check=True, capture_output=True)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    repos = module.collect_repos()
    assert [item["name"] for item in repos] == ["selected-repo"]
    assert repos[0]["path"] == "selected-repo"


def collector():
    spec = importlib.util.spec_from_file_location("workspace_attestation", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_uninspectable_git_repository_cannot_be_attested_clean(monkeypatch, tmp_path):
    module = collector()
    repo = tmp_path / "broken"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /nonexistent/workspace-attestation-test\n")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        module.collect_repos()


def test_missing_podman_does_not_abort_collection(monkeypatch, tmp_path):
    module = collector()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    def missing(*args):
        raise FileNotFoundError("podman")
    monkeypatch.setattr(module, "sh", missing)
    assert module.collect_instance() == {}


def test_failed_podman_inspection_is_not_an_empty_inventory(monkeypatch, tmp_path):
    module = collector()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    def failed(*args):
        raise subprocess.CalledProcessError(125, args, stderr="permission denied")
    monkeypatch.setattr(module, "sh", failed)
    with pytest.raises(subprocess.CalledProcessError):
        module.collect_instance()


def test_malformed_podman_inventory_is_not_an_empty_inventory(monkeypatch, tmp_path):
    module = collector()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "sh", lambda *args: "not JSON")
    with pytest.raises(json.JSONDecodeError):
        module.collect_instance()


def test_podman_name_lists_are_recorded(monkeypatch, tmp_path):
    module = collector()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    payload = [{"Names": ["baz0"], "Image": "image", "ImageID": "sha256:abc", "Status": "Up"}]
    monkeypatch.setattr(module, "sh", lambda *args: json.dumps(payload))
    assert module.collect_instance() == {
        "baz0": {"image": "image", "image_id": "abc", "status": "Up"},
    }


SIGNER_FINGERPRINT = "78171B4532F8EFFBDFA2958F4315AE31DCC658B1"
ROOT_FINGERPRINT = "866ECB8348C0FE48E479736AF85CEFBA8B8E1040"


def stub_gpg(signer, root=ROOT_FINGERPRINT, root_present=True, root_certifies=True,
             checked=True):
    """A GPG that answers only what the verifier asks, in GPG's own shapes."""

    def run(args, **kwargs):
        argv = args[0] if isinstance(args[0], (list, tuple)) else args
        argv = [str(a) for a in argv]
        if "--import" in argv:
            return SimpleNamespace(stdout="")
        if "--check-sigs" in argv:
            lines = [
                "pub:-:255:22:%s:1790971148:::-:::scESC:::::ed25519:::0:"
                % signer[-16:],
                "fpr:::::::::%s:" % signer,
                "sig:!::22:%s:1790971148::::self uid:13x::%s:::10:"
                % (signer[-16:], signer),
            ]
            if root_certifies:
                lines.append(
                    "sig:%s::22:%s:1790971171::::RAGBAZ root <ragbaz@proton.me>:10x::%s:::10:"
                    % ("!" if checked else "L", root[-16:], root))
            return SimpleNamespace(stdout="\n".join(lines) + "\n")
        if "--fingerprint" in argv:
            if not root_present:
                return SimpleNamespace(stdout="")
            return SimpleNamespace(stdout=(
                "pub:-:255:22:%s:1790971137:::-:::scESC:::::ed25519:::0:\n"
                "fpr:::::::::%s:\n" % (root[-16:], root)))
        status = ("[GNUPG:] VALIDSIG %s 2026-10-02 1790979519 0 4 0 22 8 00 %s\n"
                  % (signer, signer))
        return SimpleNamespace(stdout=status)

    return run


def verifier_fixture(monkeypatch, tmp_path, signer=SIGNER_FINGERPRINT, **gpg_options):
    spec = importlib.util.spec_from_file_location("verify_workspace",
                                                 ROOT / "scripts" / "verify-workspace.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = ["manifest.json", "workspace.jsonl", "workspace.jsonl.attestation.json",
             "ragbaz-root.pub.asc", "box0-genesis.pub.asc"]
    for name in names:
        (tmp_path / name).write_bytes((ROOT / "attestations" / name).read_bytes())
    monkeypatch.setattr(module.subprocess, "run",
                        stub_gpg(signer, **gpg_options))
    att = json.loads((tmp_path / "workspace.jsonl.attestation.json").read_text())
    nostoi = SimpleNamespace(verify=lambda path: {"ok": True},
                            digest=lambda record: att["digest"],
                            canonical=lambda value: json.dumps(value, sort_keys=True))
    return module, nostoi, att["fingerprint"]


def verify(module, tmp_path, nostoi, root=ROOT_FINGERPRINT):
    module.verify_bundle(tmp_path, SIGNER_FINGERPRINT, nostoi, root)


def test_substituted_signer_cannot_hide_behind_claimed_fingerprint(monkeypatch, tmp_path):
    module, nostoi, expected = verifier_fixture(monkeypatch, tmp_path, "A" * 40)
    with pytest.raises(ValueError, match="actual GPG signer"):
        verify(module, tmp_path, nostoi)


def test_substituted_root_pubkey_cannot_vouch_for_the_signer(monkeypatch, tmp_path):
    module, nostoi, _ = verifier_fixture(monkeypatch, tmp_path, root_present=False)
    with pytest.raises(ValueError, match="supplied organisation root"):
        verify(module, tmp_path, nostoi)


def test_local_only_root_certification_is_not_delegation(monkeypatch, tmp_path):
    module, nostoi, _ = verifier_fixture(monkeypatch, tmp_path, checked=False)
    with pytest.raises(ValueError, match="did not certify"):
        verify(module, tmp_path, nostoi)


def test_absent_root_certification_is_not_delegation(monkeypatch, tmp_path):
    module, nostoi, _ = verifier_fixture(monkeypatch, tmp_path, root_certifies=False)
    with pytest.raises(ValueError, match="did not certify"):
        verify(module, tmp_path, nostoi)


def test_missing_public_key_file_is_rejected(monkeypatch, tmp_path):
    module, nostoi, _ = verifier_fixture(monkeypatch, tmp_path)
    (tmp_path / "ragbaz-root.pub.asc").unlink()
    with pytest.raises(ValueError, match="public key file"):
        verify(module, tmp_path, nostoi)


def test_signed_sequence_is_selected_not_the_first_record(monkeypatch, tmp_path):
    module, nostoi, _ = verifier_fixture(monkeypatch, tmp_path)
    att = json.loads((tmp_path / "workspace.jsonl.attestation.json").read_text())
    att["seq"] = 99
    (tmp_path / "workspace.jsonl.attestation.json").write_text(json.dumps(att))
    with pytest.raises(ValueError, match="signed sequence"):
        verify(module, tmp_path, nostoi)


def test_modified_readable_manifest_is_rejected(monkeypatch, tmp_path):
    module, nostoi, _ = verifier_fixture(monkeypatch, tmp_path)
    verify(module, tmp_path, nostoi)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["box"]["host"] = "substituted"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="readable manifest"):
        verify(module, tmp_path, nostoi)


def test_later_appended_record_is_verified_against_the_signed_sequence(monkeypatch, tmp_path):
    """A valid attestation over record 2 must not be checked against record 1."""
    module, nostoi, _ = verifier_fixture(monkeypatch, tmp_path)
    import hashlib
    body = json.loads((tmp_path / "manifest.json").read_text())
    first = {"seq": 1, "body": body, "prev": None, "digest": "0" * 64}
    second = {"seq": 2, "body": body, "prev": first["digest"], "digest": "1" * 64}
    (tmp_path / "workspace.jsonl").write_text(
        json.dumps(first) + "\n" + json.dumps(second) + "\n")
    att = json.loads((tmp_path / "workspace.jsonl.attestation.json").read_text())
    att["seq"] = 2
    att["digest"] = second["digest"]
    (tmp_path / "workspace.jsonl.attestation.json").write_text(json.dumps(att))
    nostoi.digest = lambda record: {1: first["digest"], 2: second["digest"]}[record["seq"]]
    # The manifest matches the signed record body, so only sequence selection
    # distinguishes passing from failing here.
    verify(module, tmp_path, nostoi)
