"""Relocation must preserve historical evidence and collect the selected workspace."""

import hashlib
import importlib.util
import subprocess
from pathlib import Path


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
    monkeypatch.setattr(module, "ROOT", tmp_path)
    repos = module.collect_repos()
    assert [item["name"] for item in repos] == ["selected-repo"]
    assert repos[0]["path"] == "selected-repo"
