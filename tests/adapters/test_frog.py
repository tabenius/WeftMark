from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from weftmark.adapters import frog as frog_adapter
from weftmark.adapters.frog import FrogImportError, read_frog_snapshot


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


_INTEGER_COLUMNS = {
    "id",
    "third_party",
    "active",
    "pid",
    "lease_seconds",
}


def insert(
    connection: sqlite3.Connection,
    table: str,
    **values: object,
) -> None:
    columns = frog_adapter._TABLES[table][1]
    placeholders = ", ".join("?" for _ in columns)
    names = ", ".join(f'"{column}"' for column in columns)
    connection.execute(
        f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
        tuple(values.get(column) for column in columns),
    )


def database(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT)"
    )
    connection.executemany(
        "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
        ((name, NOW.isoformat()) for name in frog_adapter.SUPPORTED_MIGRATIONS),
    )
    for table, (_, columns) in frog_adapter._TABLES.items():
        declarations = ", ".join(
            f'"{column}" {"INTEGER" if column in _INTEGER_COLUMNS else "TEXT"}'
            for column in columns
        )
        connection.execute(f'CREATE TABLE "{table}" ({declarations})')

    insert(
        connection,
        "repos",
        repo_path="/workspace/project",
        name="project",
        kind="prototype",
        status="active",
        third_party=0,
        repo_key="origin:project",
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
    )
    insert(
        connection,
        "agents",
        name="worker-1",
        kind="agent",
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
    )
    insert(
        connection,
        "tasks",
        slug="task-1",
        repo_path="/workspace/project",
        title="Do one thing",
        why="Observed need",
        what_text="Small slice",
        priority="p1",
        workflow_status="in_progress",
        git_status="not_started",
        assigned_agent="worker-1",
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
        status_confidence_at=NOW.isoformat(),
    )
    insert(
        connection,
        "files",
        file_path="/workspace/project/src/app.py",
        repo_path="/workspace/project",
        file_type="source",
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
    )
    insert(
        connection,
        "task_files",
        task_slug="task-1",
        file_path="/workspace/project/src/app.py",
        role="edit",
        created_at=NOW.isoformat(),
    )
    insert(
        connection,
        "task_assignments",
        id=1,
        task_slug="task-1",
        agent_name="worker-1",
        assigned_at=NOW.isoformat(),
        active=1,
    )
    insert(
        connection,
        "locks",
        id=1,
        scope_key="task:task-1",
        repo_path="/workspace/project",
        repo_key="origin:project",
        lock_kind="edit",
        file_paths_json='["/workspace/project/src/app.py"]',
        rel_files_json='["src/app.py"]',
        agent_name="worker-1",
        host="box-1",
        box_id="box-1",
        reason="claim task-1",
        status="active",
        lease_seconds=1800,
        started_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
    )
    connection.commit()
    connection.close()
    return path


def test_snapshot_is_deterministic_typed_and_read_only(tmp_path: Path) -> None:
    source = database(tmp_path / "AGENTS.db")
    before = source.read_bytes()

    first = read_frog_snapshot(
        source,
        source_label="workspace-main",
        captured_at=NOW,
    )
    second = read_frog_snapshot(
        source,
        source_label="workspace-main",
        captured_at=NOW + timedelta(minutes=1),
    )

    assert source.read_bytes() == before
    assert first.digest == second.digest
    assert first.source_schema_migrations == frog_adapter.SUPPORTED_MIGRATIONS
    assert first.records["tasks"][0]["slug"] == "task-1"
    assert first.records["locks"][0]["file_paths"] == [
        "/workspace/project/src/app.py"
    ]
    assert first.records["locks"][0]["relative_files"] == ["src/app.py"]
    assert "file_paths_json" not in first.records["locks"][0]

    payload = first.to_payload()
    assert payload["source_kind"] == "frog-agents-db"
    assert payload["source_label"] == "workspace-main"
    assert payload["digest"].startswith("sha256:")
    assert payload["captured_at"] == NOW.isoformat()


def test_snapshot_rejects_schema_skew_and_malformed_json(tmp_path: Path) -> None:
    source = database(tmp_path / "AGENTS.db")
    connection = sqlite3.connect(source)
    connection.execute(
        "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
        ("014_future.sql", NOW.isoformat()),
    )
    connection.commit()
    connection.close()
    with pytest.raises(FrogImportError, match="unsupported Frog schema"):
        read_frog_snapshot(source, source_label="main", captured_at=NOW)

    source = database(tmp_path / "malformed.db")
    connection = sqlite3.connect(source)
    connection.execute("UPDATE locks SET file_paths_json = '{bad'")
    connection.commit()
    connection.close()
    with pytest.raises(FrogImportError, match="malformed or unreadable"):
        read_frog_snapshot(source, source_label="main", captured_at=NOW)


def test_snapshot_rejects_unresolved_relations_and_symlinks(tmp_path: Path) -> None:
    source = database(tmp_path / "AGENTS.db")
    connection = sqlite3.connect(source)
    insert(
        connection,
        "task_tags",
        task_slug="missing-task",
        tag="unsafe",
        created_at=NOW.isoformat(),
    )
    connection.commit()
    connection.close()
    with pytest.raises(FrogImportError, match="unresolved Frog relation"):
        read_frog_snapshot(source, source_label="main", captured_at=NOW)

    link = tmp_path / "linked.db"
    link.symlink_to(source)
    with pytest.raises(FrogImportError, match="symlink"):
        read_frog_snapshot(link, source_label="main", captured_at=NOW)


def test_snapshot_requires_identity_and_timezone(tmp_path: Path) -> None:
    source = database(tmp_path / "AGENTS.db")
    with pytest.raises(FrogImportError, match="source_label"):
        read_frog_snapshot(source, source_label=" ", captured_at=NOW)
    with pytest.raises(FrogImportError, match="timezone"):
        read_frog_snapshot(
            source,
            source_label="main",
            captured_at=NOW.replace(tzinfo=None),
        )
