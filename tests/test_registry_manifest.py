import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/create_registry_manifest.py"
spec = importlib.util.spec_from_file_location("registry_manifest", SCRIPT)
assert spec is not None and spec.loader is not None
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)


@pytest.fixture
def release_source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / "source.txt").write_text("Synthetic source\n")
    subprocess.run(["git", "add", "source.txt"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "Release source fixture"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "tag", "v1.2.3"], cwd=root, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in ["weftmark.whl", "weftmark.tar.gz", "SHA256SUMS"]:
        (dist / name).write_bytes(("Synthetic " + name).encode())
    env = {"RELEASE_TAG": "v1.2.3", "GITHUB_REPOSITORY": "tabenius/WeftMark",
        "GITHUB_REF_NAME": "v1.2.3", "GITHUB_REF": "refs/tags/v1.2.3", "GITHUB_SHA": commit}
    return root, dist, env


def test_manifest_binds_exact_checked_out_tag_and_actual_artifact_bytes(release_source):
    root, dist, env = release_source
    manifest = registry.create_manifest(dist, repository_root=root, environ=env)
    assert manifest["sourceCommit"] == env["GITHUB_SHA"]
    assert manifest["version"] == "1.2.3"
    assert manifest["channel"] == "candidate"
    assert json.loads((dist / "release.json").read_text()) == manifest
    for item in manifest["files"]:
        data = (dist / item["name"]).read_bytes()
        assert item["sha256"] == hashlib.sha256(data).hexdigest()
        assert item["bytes"] == len(data)
    with pytest.raises(FileExistsError):
        registry.create_manifest(dist, repository_root=root, environ=env)


@pytest.mark.parametrize("change", [
    {"GITHUB_REF": "refs/heads/main", "GITHUB_REF_NAME": "main"},
    {"GITHUB_SHA": "b" * 40},
    {"RELEASE_TAG": "v1.2.4", "GITHUB_REF": "refs/tags/v1.2.4"},
    {"GITHUB_REPOSITORY": "tabenius/unrelated"},
    {"RELEASE_TAG": "v١.٢.٣"},
])
def test_event_context_or_source_substitution_is_refused_before_output(release_source, change):
    root, dist, env = release_source
    with pytest.raises(ValueError):
        registry.create_manifest(dist, repository_root=root, environ={**env, **change})
    assert not (dist / "release.json").exists()


def test_existing_tag_does_not_authorize_a_different_checkout(release_source):
    root, dist, env = release_source
    (root / "new.txt").write_text("Different source\n")
    subprocess.run(["git", "add", "new.txt"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "Different source fixture"], cwd=root, check=True, capture_output=True)
    with pytest.raises(ValueError, match="checked-out commit"):
        registry.create_manifest(dist, repository_root=root, environ=env)
    assert not (dist / "release.json").exists()


def test_linked_artifacts_are_refused_before_manifest_publication(release_source):
    root, dist, env = release_source
    (dist / "linked.whl").symlink_to(dist / "weftmark.whl")
    with pytest.raises(ValueError, match="artifact"):
        registry.create_manifest(dist, repository_root=root, environ=env)
    assert not (dist / "release.json").exists()
