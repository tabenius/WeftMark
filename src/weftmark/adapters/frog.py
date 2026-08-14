"""Read-only snapshots of supported Frog coordination databases."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


class FrogImportError(ValueError):
    """Raised when Frog state cannot be read safely and deterministically."""


SUPPORTED_MIGRATIONS = tuple(f"{index:03d}_{name}.sql" for index, name in (
    (1, "initial"),
    (2, "repo_targets"),
    (3, "units"),
    (4, "target_runs"),
    (5, "repo_deps"),
    (6, "event_mirror"),
    (7, "repo_keys"),
    (8, "cross_box_locks"),
    (9, "task_source"),
    (10, "event_hooks"),
    (11, "box_identity"),
    (12, "peers"),
    (13, "event_origin_box"),
))


_TABLES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "repos": (
        ("repo_path",),
        (
            "repo_path", "name", "kind", "status", "third_party", "notes",
            "created_at", "updated_at", "repo_key",
        ),
    ),
    "tasks": (
        ("slug",),
        (
            "slug", "repo_path", "title", "why", "what_text", "roi_note",
            "priority", "workflow_status", "git_status", "assigned_agent",
            "delegation_current", "delegation_other", "parent_task_slug",
            "created_at", "updated_at", "status_confidence_at", "source",
            "external_id",
        ),
    ),
    "task_dependencies": (
        ("task_slug", "depends_on_slug", "relation"),
        ("task_slug", "depends_on_slug", "relation", "created_at"),
    ),
    "task_conflicts": (
        ("task_slug", "conflicts_with_slug"),
        ("task_slug", "conflicts_with_slug", "reason", "created_at"),
    ),
    "task_tags": (
        ("task_slug", "tag"),
        ("task_slug", "tag", "created_at"),
    ),
    "task_assignments": (
        ("id",),
        ("id", "task_slug", "agent_name", "assigned_at", "active", "notes"),
    ),
    "agents": (
        ("name",),
        ("name", "kind", "notes", "created_at", "updated_at"),
    ),
    "files": (
        ("file_path",),
        (
            "file_path", "repo_path", "file_type", "source_of_truth", "notes",
            "created_at", "updated_at",
        ),
    ),
    "task_files": (
        ("task_slug", "file_path"),
        ("task_slug", "file_path", "role", "created_at"),
    ),
    "locks": (
        ("id",),
        (
            "id", "scope_key", "repo_path", "lock_kind", "file_paths_json",
            "agent_name", "pid", "host", "reason", "status", "lease_seconds",
            "started_at", "updated_at", "eta_finish_at", "released_at",
            "repo_key", "rel_files_json", "box_id",
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class FrogSnapshot:
    source_label: str
    source_schema_migrations: tuple[str, ...]
    captured_at: datetime
    digest: str
    records: Mapping[str, tuple[Mapping[str, Any], ...]]

    def to_payload(self) -> dict[str, Any]:
        return json.loads(json.dumps({
            "schema_version": 1,
            "source_kind": "frog-agents-db",
            "source_label": self.source_label,
            "source_schema": {
                "migrations": self.source_schema_migrations,
            },
            "captured_at": self.captured_at.isoformat(),
            "digest": self.digest,
            "records": self.records,
        }, sort_keys=True, allow_nan=False))


def read_frog_snapshot(
    path: str | Path,
    *,
    source_label: str,
    captured_at: datetime,
) -> FrogSnapshot:
    label = source_label.strip()
    if not label:
        raise FrogImportError("source_label must not be empty")
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise FrogImportError("captured_at must include a timezone")

    source = Path(path).expanduser()
    if source.is_symlink():
        raise FrogImportError("refusing to follow Frog database symlink")
    try:
        resolved = source.resolve(strict=True)
    except OSError as error:
        raise FrogImportError("Frog database is unavailable") from error
    if not resolved.is_file():
        raise FrogImportError("Frog database must be a regular file")

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            resolved.as_uri() + "?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        migrations = tuple(
            row["name"]
            for row in connection.execute(
                "SELECT name FROM schema_migrations ORDER BY name"
            )
        )
        if migrations != SUPPORTED_MIGRATIONS:
            raise FrogImportError(
                "unsupported Frog schema migrations: " + ", ".join(migrations)
            )

        records: dict[str, tuple[Mapping[str, Any], ...]] = {}
        for table, (order, expected_columns) in _TABLES.items():
            _require_columns(connection, table, expected_columns)
            ordering = ", ".join(f'"{name}"' for name in order)
            rows = connection.execute(
                f'SELECT * FROM "{table}" ORDER BY {ordering}'
            )
            records[table] = tuple(
                _normalise_row(table, dict(row)) for row in rows
            )
        _validate_relations(records)
    except FrogImportError:
        raise
    except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError) as error:
        raise FrogImportError("Frog database is malformed or unreadable") from error
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()

    digest_contents = {
        "source_kind": "frog-agents-db",
        "source_label": label,
        "source_schema": {"migrations": migrations},
        "records": records,
    }
    canonical = json.dumps(
        digest_contents,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
    return FrogSnapshot(label, migrations, captured_at, digest, records)


def _require_columns(
    connection: sqlite3.Connection,
    table: str,
    expected: tuple[str, ...],
) -> None:
    actual = tuple(
        row["name"]
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    )
    if set(actual) != set(expected) or len(actual) != len(expected):
        raise FrogImportError(f"unsupported Frog table schema: {table}")


def _normalise_row(table: str, row: dict[str, Any]) -> Mapping[str, Any]:
    if table == "locks":
        for source, target in (
            ("file_paths_json", "file_paths"),
            ("rel_files_json", "relative_files"),
        ):
            value = json.loads(row.pop(source))
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise FrogImportError(f"Frog lock {source} must be a string list")
            row[target] = value
    json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return row


def _validate_relations(
    records: Mapping[str, tuple[Mapping[str, Any], ...]],
) -> None:
    tasks = {str(row["slug"]) for row in records["tasks"]}
    agents = {str(row["name"]) for row in records["agents"]}
    # Frog uses files as an optional classification registry in practice, so a
    # task_files path may be meaningful even when no files row exists.
    checks = (
        ("task_dependencies", "task_slug", tasks),
        ("task_dependencies", "depends_on_slug", tasks),
        ("task_conflicts", "task_slug", tasks),
        ("task_conflicts", "conflicts_with_slug", tasks),
        ("task_tags", "task_slug", tasks),
        ("task_assignments", "task_slug", tasks),
        ("task_assignments", "agent_name", agents),
        ("task_files", "task_slug", tasks),
    )
    for table, field, identities in checks:
        for row in records[table]:
            if str(row[field]) not in identities:
                raise FrogImportError(
                    f"unresolved Frog relation: {table}.{field}"
                )
