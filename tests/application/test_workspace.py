from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from weftmark.adapters.git_local import LocalGit
from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.ledger import LedgerService
from weftmark.application.workspace import WorkspaceService, binding_from_payload
from weftmark.domain.changeset import LineageEventKind
from weftmark.domain.scope import Scope


NOW = datetime(2026, 8, 14, 1, 20, tzinfo=timezone.utc)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "base")
    return tmp_path


def service(repo: Path, ledger_path: Path) -> WorkspaceService:
    return WorkspaceService(LocalGit(repo), LedgerService(JsonlLedger(ledger_path)))


def test_create_persist_reload_and_list_complete_binding(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    ledger_path = tmp_path / "state" / "ledger.jsonl"
    created = service(repo, ledger_path).create_change_set(
        id="chg-1",
        goal="Persist exact lineage",
        base_revision="HEAD",
        scopes=(Scope.file("src/**"), Scope.contract("api-v1")),
        created_at=NOW,
    )

    restored = service(repo, ledger_path).require_change_set("chg-1")
    assert restored == created
    assert service(repo, ledger_path).list_change_sets() == (created,)


def test_refresh_appends_observation_and_head_lineage(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    repository(repo)
    ledger_path = tmp_path / "state" / "ledger.jsonl"
    workspace = service(repo, ledger_path)
    first = workspace.create_change_set(
        id="chg-1",
        goal="Track movement",
        base_revision="HEAD",
        scopes=(Scope.file("**"),),
        created_at=NOW,
    )
    (repo / "next.txt").write_text("next\n", encoding="utf-8")
    git(repo, "add", "next.txt")
    git(repo, "commit", "-m", "next")
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    refreshed = workspace.refresh_change_set(
        "chg-1", observed_at=NOW + timedelta(minutes=1)
    )
    assert len(refreshed.observations) == 2
    assert refreshed.latest.head_sha != first.latest.head_sha
    assert refreshed.latest.dirty_paths == ("dirty.txt",)
    assert refreshed.change_set.lineage[-1].kind is LineageEventKind.HEAD_ADVANCED
    assert service(repo, ledger_path).require_change_set("chg-1") == refreshed


def test_initial_cli_flat_snapshot_is_migrated_on_read() -> None:
    payload = {
        "id": "chg-old",
        "goal": "Read old snapshot",
        "repository_id": "repo-1",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "base_revision": "main",
        "branch": "feature",
        "worktree": "/work/repo",
        "scopes": [{"kind": "file", "key": "src/**"}],
        "state": "active",
        "observation_id": "chg-old:git:1",
        "changed_paths": ["src/a.py"],
        "dirty_paths": [],
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }

    binding = binding_from_payload(payload)
    assert binding.change_set.lineage[0].kind is LineageEventKind.ACTIVATED
    assert binding.latest.changed_paths == ("src/a.py",)
