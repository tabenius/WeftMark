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


def verifier_fixture(monkeypatch, tmp_path, signer):
    spec = importlib.util.spec_from_file_location("verify_workspace", ROOT / "scripts/verify-workspace.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("manifest.json", "workspace.jsonl", "workspace.jsonl.attestation.json"):
        (tmp_path / name).write_bytes((ROOT / "attestations" / name).read_bytes())
    status = f"[GNUPG:] VALIDSIG {signer} 2026-10-02 1790979519 0 4 0 22 8 00 {signer}\n"
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(stdout=status))
    att = json.loads((tmp_path / "workspace.jsonl.attestation.json").read_text())
    nostoi = SimpleNamespace(verify=lambda path: {"ok": True},
                            digest=lambda record: att["digest"],
                            canonical=lambda value: json.dumps(value, sort_keys=True))
    return module, nostoi, att["fingerprint"]


def test_substituted_signer_cannot_hide_behind_claimed_fingerprint(monkeypatch, tmp_path):
    module, nostoi, expected = verifier_fixture(monkeypatch, tmp_path, "A" * 40)
    with pytest.raises(ValueError, match="actual GPG signer"):
        module.verify_bundle(tmp_path, expected, nostoi)


def test_modified_readable_manifest_is_rejected(monkeypatch, tmp_path):
    expected = "78171B4532F8EFFBDFA2958F4315AE31DCC658B1"
    module, nostoi, _ = verifier_fixture(monkeypatch, tmp_path, expected)
    module.verify_bundle(tmp_path, expected, nostoi)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["box"]["host"] = "substituted"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="readable manifest"):
        module.verify_bundle(tmp_path, expected, nostoi)
