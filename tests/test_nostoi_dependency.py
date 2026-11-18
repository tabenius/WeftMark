"""Dependency/source contracts and refusal tests; no network in unit tests."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tomllib

from packaging.markers import default_environment, Marker
from packaging.requirements import Requirement
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_nostoi", ROOT / "scripts/install_nostoi_release.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
CONFIG = installer.release_config()
NAME = f"nostoi-{CONFIG['version']}-linux-amd64"
PAYLOAD = b"synthetic CLI bytes\n"


def manifest():
    return {
        "schema": "ragbaz.release/1", "repository": CONFIG["repository"],
        "product": "nostoi", "version": CONFIG["version"],
        "sourceCommit": CONFIG["source-commit"], "platform": "linux-amd64",
        "files": [{"name": NAME, "bytes": len(PAYLOAD), "sha256": hashlib.sha256(PAYLOAD).hexdigest()}],
    }


def test_optional_dependency_floor_and_compatible_newer_versions():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert all(Requirement(dep).name != "nostoi" for dep in config["project"]["dependencies"])
    for extra in ("sqlite", "test"):
        req = next(Requirement(d) for d in config["project"]["optional-dependencies"][extra] if Requirement(d).name == "nostoi")
        assert req.url is None
        assert "0.1.0" not in req.specifier
        assert CONFIG["version"] in req.specifier
        assert "0.1.2" in req.specifier
        assert "0.2.0" not in req.specifier


@pytest.mark.parametrize("system,machine,implementation,source", [
    ("linux", "x86_64", "cpython", "url"),
    ("linux", "aarch64", "cpython", "git"),
    ("darwin", "arm64", "cpython", "git"),
    ("win32", "AMD64", "cpython", "git"),
    ("linux", "x86_64", "pypy", "git"),
])
def test_sources_cover_platforms_without_falling_back_to_public_index(system, machine, implementation, source):
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    environment = default_environment() | {"sys_platform": system, "platform_machine": machine, "implementation_name": implementation}
    matches = [s for s in config["tool"]["uv"]["sources"]["nostoi"] if Marker(s["marker"]).evaluate(environment)]
    assert len(matches) == 1
    assert source in matches[0]
    if source == "git":
        assert matches[0]["rev"] == CONFIG["source-commit"]
    else:
        assert f"/v{CONFIG['version']}/nostoi-{CONFIG['version']}-" in matches[0]["url"]


def test_locked_wheel_matches_verified_release_digest():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = [p for p in lock["package"] if p["name"] == "nostoi"]
    assert len(packages) == 2
    assert all(p["version"] == CONFIG["version"] for p in packages)
    wheel = next(p for p in packages if "url" in p["source"])
    assert wheel["wheels"][0]["hash"] == "sha256:e8ab2a8662bb214ca01454fac013dc504203dd92fa8e51a7386db9a15b4122aa"
    assert "b043bf953eeda3ea720adfa91f1b2177c0ab1143" not in (ROOT / "uv.lock").read_text()


@pytest.mark.parametrize("key,value", [
    ("repository", "tabenius/unrelated"), ("version", "0.1.0"),
    ("product", "other"), ("sourceCommit", "0" * 40), ("platform", "other"),
])
def test_wrong_manifest_identity_is_refused(key, value):
    wrong = manifest() | {key: value}
    with pytest.raises(ValueError, match="identity/source"):
        installer.artifact_entry(wrong, CONFIG, NAME)


def test_duplicate_missing_and_oversized_artifacts_are_refused():
    data = manifest()
    data["files"].append(data["files"][0].copy())
    with pytest.raises(ValueError, match="duplicate"):
        installer.artifact_entry(data, CONFIG, NAME)
    with pytest.raises(ValueError, match="missing"):
        installer.artifact_entry(manifest(), CONFIG, "missing")
    data = manifest()
    data["files"][0]["bytes"] = installer.MAX_ARTIFACT_BYTES + 1
    with pytest.raises(ValueError, match="size"):
        installer.artifact_entry(data, CONFIG, NAME)


@pytest.fixture
def fake_network(monkeypatch):
    calls = []
    def download(url, path, max_bytes):
        calls.append(path.name)
        content = json.dumps(manifest()).encode() if path.name == "release.json" else PAYLOAD
        path.write_bytes(content)
    monkeypatch.setattr(installer, "download", download)
    monkeypatch.setattr(installer.shutil, "which", lambda name: f"/synthetic/{name}")
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    return calls


def test_installs_only_after_signature_and_hash_checks(tmp_path, monkeypatch, fake_network):
    verified = []
    monkeypatch.setattr(installer.subprocess, "run", lambda argv, **kwargs: verified.append(argv))
    output = tmp_path / "nostoi"
    installer.install(output)
    assert output.read_bytes() == PAYLOAD
    assert output.stat().st_mode & 0o111
    assert verified[0][:2] == ["cosign", "verify-blob"]
    assert f"@refs/tags/v{CONFIG['version']}" in verified[0][verified[0].index("--certificate-identity") + 1]
    assert list(tmp_path.iterdir()) == [output]
    with pytest.raises(FileExistsError):
        installer.install(output)
    assert output.read_bytes() == PAYLOAD


def test_bad_signature_never_downloads_or_installs_binary(tmp_path, monkeypatch, fake_network):
    def fail(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv)
    monkeypatch.setattr(installer.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(tmp_path / "nostoi")
    assert NAME not in fake_network
    assert list(tmp_path.iterdir()) == []


def test_tampered_binary_is_refused(tmp_path):
    path = tmp_path / "tampered"
    path.write_bytes(PAYLOAD.replace(b"CLI", b"bad"))
    entry = installer.artifact_entry(manifest(), CONFIG, NAME)
    with pytest.raises(ValueError, match="hash/size"):
        installer.verify_artifact(path, entry)
