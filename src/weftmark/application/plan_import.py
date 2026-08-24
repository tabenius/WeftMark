"""Idempotent promotion of reviewed source-plan intent into native tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from weftmark.adapters.weft_plan import WeftPlanSnapshot, WeftPlanTask
from weftmark.application.ledger import LedgerService, LedgerServiceError
from weftmark.application.ports.ledger import LEDGER_GENESIS_DIGEST, LedgerHeadChanged
from weftmark.application.tasks import TaskService, TaskServiceError
from weftmark.domain.scope import Scope
from weftmark.domain.task import TaskError, TaskIntent, TaskState


class PlanImportError(ValueError):
    """Raised when source intent cannot be imported safely."""


@dataclass(frozen=True, slots=True)
class PlanDrift:
    source_label: str
    previous_digest: str
    current_digest: str
    added_tasks: tuple[str, ...]
    removed_tasks: tuple[str, ...]
    changed_tasks: tuple[str, ...]
    added_files: tuple[str, ...]
    removed_files: tuple[str, ...]
    changed_files: tuple[str, ...]


class PlanImportDriftError(PlanImportError):
    def __init__(self, drift: PlanDrift) -> None:
        self.drift = drift
        super().__init__(
            f"source plan {drift.source_label!r} changed from "
            f"{drift.previous_digest} to {drift.current_digest}; "
            "explicit drift reconciliation is required"
        )


@dataclass(frozen=True, slots=True)
class PlanImportResult:
    source_label: str
    source_digest: str
    imported: bool
    created_tasks: tuple[str, ...]
    existing_tasks: tuple[str, ...]
    created_dependencies: tuple[tuple[str, str], ...]
    existing_dependencies: tuple[tuple[str, str], ...]
    created_conflicts: tuple[tuple[str, str], ...]
    existing_conflicts: tuple[tuple[str, str], ...]
    skipped_terminal_tasks: tuple[str, ...]
    satisfied_source_dependencies: tuple[tuple[str, str], ...]


_RECEIPT_KIND = "source_plan_import"
_CONFLICT_REASON = "reviewed source-plan semantic conflict"
_SOURCE_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,255})$")


class PlanImportService:
    """Create actionable native intent without importing source lifecycle authority."""

    def __init__(self, tasks: TaskService, ledger: LedgerService) -> None:
        self._tasks = tasks
        self._ledger = ledger

    def import_snapshot(
        self,
        snapshot: WeftPlanSnapshot,
        *,
        source_label: str,
        imported_at: datetime,
    ) -> PlanImportResult:
        label = _source_label(source_label)
        _require_aware(imported_at)
        previous = self._receipt(label)
        if previous is not None:
            if previous["source_digest"] != snapshot.digest:
                raise PlanImportDriftError(_drift(label, previous, snapshot))
            return _repeated_result(previous)

        actionable = {
            task.slug: task for task in snapshot.tasks if task.status != "done"
        }
        skipped = tuple(
            sorted(task.slug for task in snapshot.tasks if task.status == "done")
        )
        expected = {
            slug: _task_intent(task, imported_at=imported_at)
            for slug, task in actionable.items()
        }
        current = {task.id: task for task in self._tasks.list()}
        mismatches = tuple(
            sorted(
                slug
                for slug, intent in expected.items()
                if slug in current and not _same_intent(current[slug], intent)
            )
        )
        if mismatches:
            raise PlanImportError(
                "native tasks already exist with different immutable intent: "
                + ", ".join(mismatches)
            )

        source_dependencies = tuple(
            sorted(
                (task.slug, dependency)
                for task in snapshot.tasks
                if task.slug in actionable
                for dependency in task.dependencies
            )
        )
        dependencies = tuple(
            pair for pair in source_dependencies if pair[1] in actionable
        )
        satisfied = tuple(
            pair for pair in source_dependencies if pair[1] not in actionable
        )
        conflicts = tuple(
            sorted(
                {
                    tuple(sorted((task.slug, conflict)))
                    for task in snapshot.tasks
                    if task.slug in actionable
                    for conflict in task.conflicts
                    if conflict in actionable
                }
            )
        )
        current_dependencies = {
            (value.task_id, value.depends_on_task_id)
            for value in self._tasks.dependencies()
        }
        current_conflicts = {
            (value.first_task_id, value.second_task_id): value.reason
            for value in self._tasks.conflicts()
        }
        conflicting_reasons = tuple(
            pair
            for pair in conflicts
            if pair in current_conflicts and current_conflicts[pair] != _CONFLICT_REASON
        )
        if conflicting_reasons:
            raise PlanImportError(
                "native conflicts already exist with different reasons: "
                + ", ".join(f"{left}/{right}" for left, right in conflicting_reasons)
            )

        created_tasks: list[str] = []
        existing_tasks: list[str] = []
        for slug in sorted(expected):
            if slug in current:
                existing_tasks.append(slug)
                continue
            try:
                self._tasks.create(expected[slug])
            except TaskServiceError as error:
                concurrent = self._tasks.get(slug)
                if concurrent is not None and _same_intent(concurrent, expected[slug]):
                    existing_tasks.append(slug)
                    continue
                raise PlanImportError(str(error)) from error
            except (LedgerServiceError, TaskError) as error:
                raise PlanImportError(str(error)) from error
            created_tasks.append(slug)

        created_dependencies: list[tuple[str, str]] = []
        existing_dependencies: list[tuple[str, str]] = []
        for task_id, dependency_id in dependencies:
            if (task_id, dependency_id) in current_dependencies:
                existing_dependencies.append((task_id, dependency_id))
                continue
            try:
                result = self._tasks.add_dependency(
                    task_id,
                    dependency_id,
                    created_at=imported_at,
                )
            except (LedgerServiceError, TaskServiceError) as error:
                raise PlanImportError(str(error)) from error
            target = created_dependencies if result.created else existing_dependencies
            target.append((task_id, dependency_id))

        created_conflicts: list[tuple[str, str]] = []
        existing_conflicts: list[tuple[str, str]] = []
        for left, right in conflicts:
            if (left, right) in current_conflicts:
                existing_conflicts.append((left, right))
                continue
            try:
                result = self._tasks.add_conflict(
                    left,
                    right,
                    reason=_CONFLICT_REASON,
                    created_at=imported_at,
                )
            except (LedgerServiceError, TaskServiceError) as error:
                raise PlanImportError(str(error)) from error
            target = created_conflicts if result.created else existing_conflicts
            target.append((left, right))

        result = PlanImportResult(
            source_label=label,
            source_digest=snapshot.digest,
            imported=True,
            created_tasks=tuple(created_tasks),
            existing_tasks=tuple(existing_tasks),
            created_dependencies=tuple(created_dependencies),
            existing_dependencies=tuple(existing_dependencies),
            created_conflicts=tuple(created_conflicts),
            existing_conflicts=tuple(existing_conflicts),
            skipped_terminal_tasks=skipped,
            satisfied_source_dependencies=satisfied,
        )
        return self._record_receipt(
            result,
            snapshot=snapshot,
            imported_at=imported_at,
        )

    def _receipt(self, source_label: str) -> Mapping[str, Any] | None:
        entry = self._ledger.latest(kind=_RECEIPT_KIND, entity_id=source_label)
        if entry is None:
            return None
        return _validate_receipt(entry.payload, source_label)

    def _record_receipt(
        self,
        result: PlanImportResult,
        *,
        snapshot: WeftPlanSnapshot,
        imported_at: datetime,
    ) -> PlanImportResult:
        payload = _receipt_payload(result, snapshot=snapshot, imported_at=imported_at)
        for _ in range(8):
            entries = self._ledger.snapshot()
            previous = next(
                (
                    entry
                    for entry in reversed(entries)
                    if entry.kind == _RECEIPT_KIND
                    and entry.entity_id == result.source_label
                ),
                None,
            )
            if previous is not None:
                stored = _validate_receipt(previous.payload, result.source_label)
                if stored["source_digest"] != snapshot.digest:
                    raise PlanImportDriftError(
                        _drift(result.source_label, stored, snapshot)
                    )
                return _repeated_result(stored)
            expected_head = entries[-1].digest if entries else LEDGER_GENESIS_DIGEST
            try:
                self._ledger.record_if_head(
                    kind=_RECEIPT_KIND,
                    entity_id=result.source_label,
                    payload=payload,
                    recorded_at=imported_at,
                    expected_digest=expected_head,
                )
                return result
            except LedgerHeadChanged:
                continue
            except LedgerServiceError as error:
                raise PlanImportError(str(error)) from error
        raise PlanImportError("ledger remained busy while recording source-plan import")


def _task_intent(task: WeftPlanTask, *, imported_at: datetime) -> TaskIntent:
    initial_state = TaskState.IDEA if task.status == "idea" else TaskState.TODO
    try:
        return TaskIntent.create(
            id=task.slug,
            title=task.title,
            why=task.purpose,
            what="\n".join(task.deliverables),
            roi_note=None,
            priority=task.priority,
            state=initial_state,
            scopes=tuple(Scope.parse(value) for value in task.scopes),
            created_at=imported_at,
        )
    except (TaskError, ValueError) as error:
        raise PlanImportError(f"source task {task.slug!r} is not native-compatible") from error


def _same_intent(current: TaskIntent, expected: TaskIntent) -> bool:
    return (
        current.id == expected.id
        and current.title == expected.title
        and current.why == expected.why
        and current.what == expected.what
        and current.roi_note == expected.roi_note
        and current.priority is expected.priority
        and tuple(sorted(current.scopes)) == tuple(sorted(expected.scopes))
    )


def _receipt_payload(
    result: PlanImportResult,
    *,
    snapshot: WeftPlanSnapshot,
    imported_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_label": result.source_label,
        "source_digest": snapshot.digest,
        "imported_at": imported_at.isoformat(),
        "source_files": [
            {"path": value.path, "digest": value.digest, "size": value.size}
            for value in snapshot.files
        ],
        "task_fingerprints": {
            task.slug: task.fingerprint for task in snapshot.tasks
        },
        "native_task_ids": sorted(
            set(result.created_tasks) | set(result.existing_tasks)
        ),
        "skipped_terminal_tasks": list(result.skipped_terminal_tasks),
        "satisfied_source_dependencies": [
            list(value) for value in result.satisfied_source_dependencies
        ],
        "created": {
            "tasks": list(result.created_tasks),
            "dependencies": [list(value) for value in result.created_dependencies],
            "conflicts": [list(value) for value in result.created_conflicts],
        },
        "existing": {
            "tasks": list(result.existing_tasks),
            "dependencies": [list(value) for value in result.existing_dependencies],
            "conflicts": [list(value) for value in result.existing_conflicts],
        },
    }


def _validate_receipt(payload: Mapping[str, Any], source_label: str) -> Mapping[str, Any]:
    try:
        expected_fields = {
            "schema_version",
            "source_label",
            "source_digest",
            "imported_at",
            "source_files",
            "task_fingerprints",
            "native_task_ids",
            "skipped_terminal_tasks",
            "satisfied_source_dependencies",
            "created",
            "existing",
        }
        if set(payload) != expected_fields:
            raise ValueError("unexpected receipt fields")
        if payload["schema_version"] != 1 or payload["source_label"] != source_label:
            raise ValueError("identity mismatch")
        digest = str(payload["source_digest"])
        if not _is_digest(digest):
            raise ValueError("invalid digest")
        imported_at = datetime.fromisoformat(str(payload["imported_at"]))
        _require_aware(imported_at)
        source_files = payload["source_files"]
        if not isinstance(source_files, list):
            raise ValueError("invalid files")
        for value in source_files:
            if (
                not isinstance(value, Mapping)
                or set(value) != {"path", "digest", "size"}
                or not isinstance(value.get("path"), str)
                or Scope.file(value["path"]).key != value["path"]
                or not _is_digest(value.get("digest"))
                or not isinstance(value.get("size"), int)
                or value["size"] < 0
            ):
                raise ValueError("invalid file record")
        fingerprints = payload["task_fingerprints"]
        if not isinstance(fingerprints, Mapping) or any(
            not isinstance(key, str) or not _is_digest(value)
            for key, value in fingerprints.items()
        ):
            raise ValueError("invalid task fingerprints")
        if not _text_sequence(payload["native_task_ids"]):
            raise ValueError("invalid native task ids")
        if not _text_sequence(payload["skipped_terminal_tasks"]):
            raise ValueError("invalid skipped tasks")
        if not _pair_sequence(payload["satisfied_source_dependencies"]):
            raise ValueError("invalid source dependencies")
        for group_name in ("created", "existing"):
            group = payload[group_name]
            if not isinstance(group, Mapping) or set(group) != {
                "tasks",
                "dependencies",
                "conflicts",
            }:
                raise ValueError(f"invalid {group_name} result")
            if (
                not _text_sequence(group["tasks"])
                or not _pair_sequence(group["dependencies"])
                or not _pair_sequence(group["conflicts"])
            ):
                raise ValueError(f"invalid {group_name} result")
    except (KeyError, PlanImportError, TypeError, ValueError) as error:
        raise PlanImportError("stored source-plan import receipt is malformed") from error
    return payload


def _repeated_result(receipt: Mapping[str, Any]) -> PlanImportResult:
    return PlanImportResult(
        source_label=str(receipt["source_label"]),
        source_digest=str(receipt["source_digest"]),
        imported=False,
        created_tasks=(),
        existing_tasks=tuple(str(value) for value in receipt["native_task_ids"]),
        created_dependencies=(),
        existing_dependencies=(),
        created_conflicts=(),
        existing_conflicts=(),
        skipped_terminal_tasks=tuple(
            str(value) for value in receipt["skipped_terminal_tasks"]
        ),
        satisfied_source_dependencies=tuple(
            (str(value[0]), str(value[1]))
            for value in receipt["satisfied_source_dependencies"]
        ),
    )


def _drift(
    source_label: str,
    previous: Mapping[str, Any],
    current: WeftPlanSnapshot,
) -> PlanDrift:
    previous_tasks = {
        str(key): str(value)
        for key, value in previous["task_fingerprints"].items()
    }
    current_tasks = {task.slug: task.fingerprint for task in current.tasks}
    previous_files = {
        str(value["path"]): str(value["digest"])
        for value in previous["source_files"]
    }
    current_files = {value.path: value.digest for value in current.files}
    return PlanDrift(
        source_label=source_label,
        previous_digest=str(previous["source_digest"]),
        current_digest=current.digest,
        added_tasks=tuple(sorted(set(current_tasks) - set(previous_tasks))),
        removed_tasks=tuple(sorted(set(previous_tasks) - set(current_tasks))),
        changed_tasks=tuple(
            sorted(
                key
                for key in set(previous_tasks) & set(current_tasks)
                if previous_tasks[key] != current_tasks[key]
            )
        ),
        added_files=tuple(sorted(set(current_files) - set(previous_files))),
        removed_files=tuple(sorted(set(previous_files) - set(current_files))),
        changed_files=tuple(
            sorted(
                key
                for key in set(previous_files) & set(current_files)
                if previous_files[key] != current_files[key]
            )
        ),
    )


def _source_label(value: str) -> str:
    label = value.strip()
    if (
        not _SOURCE_LABEL.fullmatch(label)
        or any(part in {"", ".", ".."} for part in label.split("/"))
    ):
        raise PlanImportError("source_label must be a normalized portable label")
    return label


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PlanImportError("imported_at must include a timezone")


def _is_digest(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )


def _text_sequence(value: object) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and bool(item) for item in value)
        and len(set(value)) == len(value)
    )


def _pair_sequence(value: object) -> bool:
    if not isinstance(value, list):
        return False
    pairs: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) and bool(part) for part in item)
        ):
            return False
        pairs.append((item[0], item[1]))
    return len(set(pairs)) == len(pairs)
