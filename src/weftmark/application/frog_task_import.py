"""Conservative promotion of immutable Frog task snapshots into native intent."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from weftmark.application.frog_receipts import FrogReceiptService
from weftmark.application.ledger import LedgerService, LedgerServiceError
from weftmark.application.ports.ledger import LEDGER_GENESIS_DIGEST, LedgerHeadChanged
from weftmark.application.tasks import TaskService, TaskServiceError
from weftmark.domain.scope import Scope, ScopeError
from weftmark.domain.task import TaskError, TaskIntent, TaskPriority, TaskState


class FrogTaskImportError(ValueError):
    """Raised when Frog intent cannot be promoted without widening authority."""


@dataclass(frozen=True, slots=True)
class FrogTaskImportResult:
    source_label: str
    source_snapshot_digest: str
    imported: bool
    created_tasks: tuple[str, ...]
    existing_tasks: tuple[str, ...]
    created_dependencies: tuple[tuple[str, str], ...]
    existing_dependencies: tuple[tuple[str, str], ...]
    created_conflicts: tuple[tuple[str, str], ...]
    existing_conflicts: tuple[tuple[str, str], ...]
    skipped_terminal_tasks: tuple[str, ...]
    satisfied_source_dependencies: tuple[tuple[str, str], ...]


_RECEIPT_KIND = "frog_native_task_import"
_CONFLICT_REASON = "imported Frog semantic conflict"
_TERMINAL = frozenset({"done", "cancelled", "abandoned", "archived"})
_SECRET = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|"
    r"(?:github_pat_|gh[oprsu]_|(?<![a-z0-9])sk-[a-z0-9])|"
    r"(?:password|secret|token|api[_-]?key|credential)"
    r"\s*[:=]\s*(?!<redacted>|redacted|\*\*\*)\S+",
    re.IGNORECASE,
)


class FrogTaskImportService:
    """Create native plan intent from one explicitly reviewed Frog snapshot."""

    def __init__(
        self,
        receipts: FrogReceiptService,
        tasks: TaskService,
        ledger: LedgerService,
    ) -> None:
        self._receipts = receipts
        self._tasks = tasks
        self._ledger = ledger

    def import_tasks(
        self,
        snapshot_digest: str,
        task_slugs: Sequence[str],
        *,
        scopes_by_task: Mapping[str, Sequence[Scope]],
        imported_at: datetime,
    ) -> FrogTaskImportResult:
        _require_aware(imported_at)
        selected = _selected_slugs(task_slugs)
        _validate_requested_scopes(scopes_by_task)
        receipt = self._receipts.get(snapshot_digest)
        if receipt is None:
            raise FrogTaskImportError(f"Frog snapshot not found: {snapshot_digest}")

        previous = self._receipt(receipt.source_label)
        if previous is not None:
            if previous["source_snapshot_digest"] != snapshot_digest:
                raise FrogTaskImportError(
                    "Frog source snapshot changed; explicit drift reconciliation is required"
                )
            _require_same_selection(previous, selected, scopes_by_task)
            return self._finish(previous, imported=False)

        records = receipt.snapshot["records"]
        by_slug = _task_index(records["tasks"])
        missing = tuple(slug for slug in selected if slug not in by_slug)
        if missing:
            raise FrogTaskImportError("Frog tasks not found: " + ", ".join(missing))
        chosen = {slug: by_slug[slug] for slug in selected}
        for slug, task in chosen.items():
            if _is_weftmark_origin(task):
                raise FrogTaskImportError(
                    f"refusing Frog task {slug!r} because it originated from WeftMark"
                )

        actionable = {
            slug: task
            for slug, task in chosen.items()
            if _status(task) not in _TERMINAL
        }
        skipped = tuple(sorted(set(selected) - set(actionable)))
        _require_scope_mapping(actionable, scopes_by_task)
        expected = {
            slug: _native_intent(
                task,
                scopes=tuple(scopes_by_task[slug]),
                imported_at=imported_at,
            )
            for slug, task in actionable.items()
        }
        dependencies, satisfied = _dependencies(records, actionable, by_slug)
        conflicts = _conflicts(records, actionable, by_slug)

        current = {task.id: task for task in self._tasks.list()}
        mismatches = tuple(
            sorted(
                slug
                for slug, intent in expected.items()
                if slug in current and not _same_intent(current[slug], intent)
            )
        )
        if mismatches:
            raise FrogTaskImportError(
                "native tasks already exist with different immutable intent: "
                + ", ".join(mismatches)
            )
        current_dependencies = {
            (value.task_id, value.depends_on_task_id)
            for value in self._tasks.dependencies()
        }
        current_conflicts = {
            (value.first_task_id, value.second_task_id): value.reason
            for value in self._tasks.conflicts()
        }
        conflicting = tuple(
            pair
            for pair in conflicts
            if pair in current_conflicts and current_conflicts[pair] != _CONFLICT_REASON
        )
        if conflicting:
            raise FrogTaskImportError(
                "native conflicts already exist with different reasons: "
                + ", ".join(f"{left}/{right}" for left, right in conflicting)
            )

        payload = {
            "schema_version": 1,
            "state": "reserved",
            "source_label": receipt.source_label,
            "source_snapshot_digest": snapshot_digest,
            "imported_at": imported_at.isoformat(),
            "selected_task_slugs": list(selected),
            "native_tasks": {
                slug: _intent_payload(intent) for slug, intent in sorted(expected.items())
            },
            "dependencies": [list(value) for value in dependencies],
            "conflicts": [list(value) for value in conflicts],
            "skipped_terminal_tasks": list(skipped),
            "satisfied_source_dependencies": [list(value) for value in satisfied],
        }
        stored, reserved = self._reserve(payload, imported_at=imported_at)
        return self._finish(stored, imported=reserved)

    def _receipt(self, source_label: str) -> Mapping[str, Any] | None:
        entry = self._ledger.latest(kind=_RECEIPT_KIND, entity_id=source_label)
        return None if entry is None else _validate_receipt(entry.payload, source_label)

    def _reserve(
        self, payload: Mapping[str, Any], *, imported_at: datetime
    ) -> tuple[Mapping[str, Any], bool]:
        source_label = str(payload["source_label"])
        for _ in range(8):
            entries = self._ledger.snapshot()
            existing = next(
                (
                    entry
                    for entry in reversed(entries)
                    if entry.kind == _RECEIPT_KIND and entry.entity_id == source_label
                ),
                None,
            )
            if existing is not None:
                stored = _validate_receipt(existing.payload, source_label)
                if stored["source_snapshot_digest"] != payload["source_snapshot_digest"]:
                    raise FrogTaskImportError(
                        "Frog source snapshot changed; explicit drift reconciliation is required"
                    )
                if stored["selected_task_slugs"] != payload["selected_task_slugs"] or stored[
                    "native_tasks"
                ] != payload["native_tasks"]:
                    raise FrogTaskImportError(
                        "Frog source already reserved with different native intent"
                    )
                return stored, False
            expected = entries[-1].digest if entries else LEDGER_GENESIS_DIGEST
            try:
                self._ledger.record_if_head(
                    kind=_RECEIPT_KIND,
                    entity_id=source_label,
                    payload=payload,
                    recorded_at=imported_at,
                    expected_digest=expected,
                )
                return _validate_receipt(payload, source_label), True
            except LedgerHeadChanged:
                continue
            except LedgerServiceError as error:
                raise FrogTaskImportError(str(error)) from error
        raise FrogTaskImportError("ledger remained busy while reserving Frog task import")

    def _finish(
        self, receipt: Mapping[str, Any], *, imported: bool
    ) -> FrogTaskImportResult:
        imported_at = datetime.fromisoformat(str(receipt["imported_at"]))
        expected = {
            slug: _intent_from_payload(value)
            for slug, value in receipt["native_tasks"].items()
        }
        created_tasks: list[str] = []
        existing_tasks: list[str] = []
        for slug, intent in sorted(expected.items()):
            current = self._tasks.get(slug)
            if current is not None:
                if not _same_intent(current, intent):
                    raise FrogTaskImportError(
                        f"reserved native task {slug!r} has different immutable intent"
                    )
                existing_tasks.append(slug)
                continue
            try:
                self._tasks.create(intent)
                created_tasks.append(slug)
            except (LedgerServiceError, TaskServiceError) as error:
                current = self._tasks.get(slug)
                if current is not None and _same_intent(current, intent):
                    existing_tasks.append(slug)
                    continue
                raise FrogTaskImportError(str(error)) from error

        current_dependencies = {
            (value.task_id, value.depends_on_task_id)
            for value in self._tasks.dependencies()
        }
        created_dependencies: list[tuple[str, str]] = []
        existing_dependencies: list[tuple[str, str]] = []
        for pair in _pairs(receipt["dependencies"]):
            if pair in current_dependencies:
                existing_dependencies.append(pair)
                continue
            try:
                relation = self._tasks.add_dependency(*pair, created_at=imported_at)
            except (LedgerServiceError, TaskServiceError) as error:
                raise FrogTaskImportError(str(error)) from error
            (created_dependencies if relation.created else existing_dependencies).append(pair)

        current_conflicts = {
            (value.first_task_id, value.second_task_id): value.reason
            for value in self._tasks.conflicts()
        }
        created_conflicts: list[tuple[str, str]] = []
        existing_conflicts: list[tuple[str, str]] = []
        for pair in _pairs(receipt["conflicts"]):
            reason = current_conflicts.get(pair)
            if reason is not None:
                if reason != _CONFLICT_REASON:
                    raise FrogTaskImportError(
                        "reserved native conflict has a different reason"
                    )
                existing_conflicts.append(pair)
                continue
            try:
                relation = self._tasks.add_conflict(
                    *pair, reason=_CONFLICT_REASON, created_at=imported_at
                )
            except (LedgerServiceError, TaskServiceError) as error:
                raise FrogTaskImportError(str(error)) from error
            (created_conflicts if relation.created else existing_conflicts).append(pair)

        if receipt["state"] == "reserved":
            self._complete(receipt, imported_at=imported_at)

        return FrogTaskImportResult(
            str(receipt["source_label"]),
            str(receipt["source_snapshot_digest"]),
            imported,
            tuple(created_tasks),
            tuple(existing_tasks),
            tuple(created_dependencies),
            tuple(existing_dependencies),
            tuple(created_conflicts),
            tuple(existing_conflicts),
            tuple(str(value) for value in receipt["skipped_terminal_tasks"]),
            _pairs(receipt["satisfied_source_dependencies"]),
        )

    def _complete(
        self, receipt: Mapping[str, Any], *, imported_at: datetime
    ) -> None:
        completed = {**receipt, "state": "completed"}
        source_label = str(receipt["source_label"])
        for _ in range(8):
            entries = self._ledger.snapshot()
            latest = next(
                (
                    entry
                    for entry in reversed(entries)
                    if entry.kind == _RECEIPT_KIND and entry.entity_id == source_label
                ),
                None,
            )
            if latest is None:
                raise FrogTaskImportError("reserved Frog task import disappeared")
            stored = _validate_receipt(latest.payload, source_label)
            if stored["state"] == "completed":
                if stored != completed:
                    raise FrogTaskImportError(
                        "completed Frog task import has different reserved intent"
                    )
                return
            if stored != receipt:
                raise FrogTaskImportError(
                    "reserved Frog task import changed before completion"
                )
            expected = entries[-1].digest if entries else LEDGER_GENESIS_DIGEST
            try:
                self._ledger.record_if_head(
                    kind=_RECEIPT_KIND,
                    entity_id=source_label,
                    payload=completed,
                    recorded_at=imported_at,
                    expected_digest=expected,
                )
                return
            except LedgerHeadChanged:
                continue
            except LedgerServiceError as error:
                raise FrogTaskImportError(str(error)) from error
        raise FrogTaskImportError("ledger remained busy while completing Frog task import")


def _selected_slugs(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise FrogTaskImportError("task_slugs must be a sequence of task identities")
    selected = tuple(sorted({str(value).strip() for value in values}))
    if not selected or any(not value for value in selected):
        raise FrogTaskImportError("at least one non-empty Frog task slug is required")
    return selected


def _validate_requested_scopes(scopes: Mapping[str, Sequence[Scope]]) -> None:
    if not isinstance(scopes, Mapping):
        raise FrogTaskImportError("scopes_by_task must be a task-to-scope mapping")
    for slug, values in scopes.items():
        if not isinstance(slug, str) or not slug.strip():
            raise FrogTaskImportError("scope mapping keys must be task identities")
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise FrogTaskImportError(f"Frog task {slug!r} scopes must be a sequence")
        if not all(isinstance(value, Scope) for value in values):
            raise FrogTaskImportError(f"Frog task {slug!r} scopes must be native Scope values")


def _task_index(values: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for value in values:
        slug = _required_text(value, "slug")
        if slug in result:
            raise FrogTaskImportError(f"duplicate Frog task: {slug}")
        result[slug] = value
    return result


def _status(task: Mapping[str, Any]) -> str:
    return _required_text(task, "workflow_status").casefold().replace("-", "_")


def _is_weftmark_origin(task: Mapping[str, Any]) -> bool:
    source = str(task.get("source") or "").strip().casefold()
    return source == "weftmark" or source.startswith(("weftmark:", "weftmark/", "weftmark-"))


def _require_scope_mapping(
    actionable: Mapping[str, Mapping[str, Any]],
    scopes: Mapping[str, Sequence[Scope]],
) -> None:
    expected = set(actionable)
    if set(scopes) != expected:
        missing = sorted(expected - set(scopes))
        extra = sorted(set(scopes) - expected)
        detail = []
        if missing:
            detail.append("missing: " + ", ".join(missing))
        if extra:
            detail.append("unexpected: " + ", ".join(extra))
        raise FrogTaskImportError("scope mapping must exactly match actionable tasks (" + "; ".join(detail) + ")")
    for slug, values in scopes.items():
        if not values:
            raise FrogTaskImportError(f"Frog task {slug!r} requires a native scope")
        canonical = tuple(value.canonical for value in values)
        if len(set(canonical)) != len(canonical):
            raise FrogTaskImportError(f"Frog task {slug!r} has duplicate native scopes")


def _dependencies(
    records: Mapping[str, Any],
    actionable: Mapping[str, Mapping[str, Any]],
    all_tasks: Mapping[str, Mapping[str, Any]],
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    relations: set[tuple[str, str]] = set()
    satisfied: set[tuple[str, str]] = set()
    for value in records["task_dependencies"]:
        if value.get("relation") != "depends_on":
            continue
        task_id = _required_text(value, "task_slug")
        dependency_id = _required_text(value, "depends_on_slug")
        if task_id not in actionable:
            continue
        if dependency_id not in all_tasks:
            raise FrogTaskImportError("Frog dependency references a missing task")
        if _status(all_tasks[dependency_id]) in _TERMINAL:
            satisfied.add((task_id, dependency_id))
        elif dependency_id not in actionable:
            raise FrogTaskImportError(
                f"selected Frog task {task_id!r} requires unselected dependency {dependency_id!r}"
            )
        else:
            relations.add((task_id, dependency_id))
    return tuple(sorted(relations)), tuple(sorted(satisfied))


def _conflicts(
    records: Mapping[str, Any],
    actionable: Mapping[str, Mapping[str, Any]],
    all_tasks: Mapping[str, Mapping[str, Any]],
) -> tuple[tuple[str, str], ...]:
    relations: set[tuple[str, str]] = set()
    for value in records["task_conflicts"]:
        left = _required_text(value, "task_slug")
        right = _required_text(value, "conflicts_with_slug")
        if left not in all_tasks or right not in all_tasks:
            raise FrogTaskImportError("Frog conflict references a missing task")
        selected_endpoint = left in actionable or right in actionable
        if not selected_endpoint:
            continue
        if left not in actionable or right not in actionable:
            other = right if left in actionable else left
            if _status(all_tasks[other]) not in _TERMINAL:
                raise FrogTaskImportError(
                    f"selected Frog task conflict requires unselected task {other!r}"
                )
            continue
        relations.add(tuple(sorted((left, right))))
    return tuple(sorted(relations))


def _native_intent(
    task: Mapping[str, Any], *, scopes: tuple[Scope, ...], imported_at: datetime
) -> TaskIntent:
    slug = _required_text(task, "slug")
    title = _required_text(task, "title")
    why = _optional_text(task.get("why")) or title
    what = _optional_text(task.get("what_text")) or title
    roi = _optional_text(task.get("roi_note"))
    for value in (title, why, what, roi):
        if value is not None and _SECRET.search(value):
            raise FrogTaskImportError(f"Frog task {slug!r} contains secret-shaped prose")
    raw_priority = str(task.get("priority") or "").strip().casefold()
    priority = TaskPriority(raw_priority) if raw_priority in {item.value for item in TaskPriority} else TaskPriority.P3
    state = TaskState.IDEA if _status(task) == "idea" else TaskState.TODO
    try:
        return TaskIntent.create(
            id=slug,
            title=title,
            why=why,
            what=what,
            roi_note=roi,
            priority=priority,
            state=state,
            scopes=tuple(sorted(scopes, key=lambda value: value.canonical)),
            created_at=imported_at,
        )
    except (ScopeError, TaskError, ValueError) as error:
        raise FrogTaskImportError(f"Frog task {slug!r} is not native-compatible") from error


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


def _intent_payload(value: TaskIntent) -> dict[str, Any]:
    return {
        "id": value.id,
        "title": value.title,
        "why": value.why,
        "what": value.what,
        "roi_note": value.roi_note,
        "priority": value.priority.value,
        "state": value.state.value,
        "scopes": list(value.scopes),
        "created_at": value.created_at.isoformat(),
    }


def _intent_from_payload(value: Mapping[str, Any]) -> TaskIntent:
    try:
        return TaskIntent.create(
            id=str(value["id"]),
            title=str(value["title"]),
            why=str(value["why"]),
            what=str(value["what"]),
            roi_note=None if value["roi_note"] is None else str(value["roi_note"]),
            priority=TaskPriority(str(value["priority"])),
            state=TaskState(str(value["state"])),
            scopes=tuple(Scope.parse(str(scope)) for scope in value["scopes"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
        )
    except (KeyError, ScopeError, TaskError, TypeError, ValueError) as error:
        raise FrogTaskImportError("stored Frog native task intent is malformed") from error


def _validate_receipt(payload: Mapping[str, Any], source_label: str) -> Mapping[str, Any]:
    try:
        if set(payload) != {
            "schema_version", "state", "source_label", "source_snapshot_digest",
            "imported_at", "selected_task_slugs", "native_tasks", "dependencies",
            "conflicts", "skipped_terminal_tasks", "satisfied_source_dependencies",
        }:
            raise ValueError("unexpected receipt fields")
        if payload["schema_version"] != 1 or payload["source_label"] != source_label:
            raise ValueError("receipt identity mismatch")
        if payload["state"] not in {"reserved", "completed"}:
            raise ValueError("invalid receipt state")
        digest = str(payload["source_snapshot_digest"])
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("invalid snapshot digest")
        imported_at = datetime.fromisoformat(str(payload["imported_at"]))
        _require_aware(imported_at)
        selected = payload["selected_task_slugs"]
        if not isinstance(selected, list) or selected != sorted(set(selected)) or not all(isinstance(value, str) and value for value in selected):
            raise ValueError("invalid selected tasks")
        native = payload["native_tasks"]
        if not isinstance(native, Mapping) or not set(native).issubset(set(selected)):
            raise ValueError("invalid native tasks")
        for slug, value in native.items():
            if not isinstance(slug, str) or not isinstance(value, Mapping) or _intent_from_payload(value).id != slug:
                raise ValueError("invalid native task intent")
        for name in ("dependencies", "conflicts", "satisfied_source_dependencies"):
            _pairs(payload[name])
        skipped = payload["skipped_terminal_tasks"]
        if not isinstance(skipped, list) or skipped != sorted(set(skipped)) or not set(skipped).issubset(set(selected)):
            raise ValueError("invalid skipped tasks")
        if set(native) | set(skipped) != set(selected):
            raise ValueError("selected tasks are not fully classified")
        native_ids = set(native)
        for left, right in _pairs(payload["dependencies"]):
            if left not in native_ids or right not in native_ids:
                raise ValueError("dependency leaves native task selection")
        for left, right in _pairs(payload["conflicts"]):
            if left not in native_ids or right not in native_ids:
                raise ValueError("conflict leaves native task selection")
        for left, right in _pairs(payload["satisfied_source_dependencies"]):
            if left not in native_ids or right not in set(skipped):
                raise ValueError("source-satisfied dependency is not classified")
    except (KeyError, FrogTaskImportError, TypeError, ValueError) as error:
        raise FrogTaskImportError("stored Frog native task import is malformed") from error
    return payload


def _require_same_selection(
    receipt: Mapping[str, Any],
    selected: tuple[str, ...],
    scopes_by_task: Mapping[str, Sequence[Scope]],
) -> None:
    if tuple(receipt["selected_task_slugs"]) != selected:
        raise FrogTaskImportError("Frog source already imported with a different task selection")
    stored = receipt["native_tasks"]
    requested = {
        slug: tuple(sorted(scope.canonical for scope in values))
        for slug, values in scopes_by_task.items()
    }
    expected = {
        slug: tuple(sorted(str(scope) for scope in value["scopes"]))
        for slug, value in stored.items()
    }
    if requested != expected:
        raise FrogTaskImportError("Frog source already imported with different native scopes")


def _pairs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise FrogTaskImportError("stored Frog relation list is malformed")
    result: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2 or not all(isinstance(part, str) and part for part in item):
            raise FrogTaskImportError("stored Frog relation is malformed")
        result.append((item[0], item[1]))
    if result != sorted(set(result)):
        raise FrogTaskImportError("stored Frog relations are not canonical")
    return tuple(result)


def _required_text(value: Mapping[str, Any], name: str) -> str:
    if name not in value:
        raise FrogTaskImportError(f"Frog record lacks {name}")
    text = str(value[name]).strip()
    if not text:
        raise FrogTaskImportError(f"Frog record has empty {name}")
    return text


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FrogTaskImportError("imported_at must include a timezone")
