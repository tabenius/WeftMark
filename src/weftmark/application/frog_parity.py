"""Read-only behavioral parity report over Frog receipts and native state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
import posixpath
from typing import Any, Mapping

from weftmark.application.claims import ClaimService
from weftmark.application.frog_planning import FrogPlanningService
from weftmark.application.frog_receipts import FrogReceiptService
from weftmark.application.frog_task_import import (
    FrogTaskImportError,
    _validate_receipt as _validate_import_receipt,
)
from weftmark.application.ledger import LedgerService
from weftmark.application.ports.ledger import LEDGER_GENESIS_DIGEST
from weftmark.application.scope_audit import ScopeAuditService
from weftmark.application.task_claims import TaskClaimService
from weftmark.application.task_planning import TaskPlanningService
from weftmark.application.tasks import TaskService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.lock import LockState
from weftmark.domain.scope import Scope
from weftmark.domain.task import TaskState


FROG_PARITY_SCHEMA = "weftmark.frog-parity-report.v0"
_TERMINAL = frozenset({"done", "cancelled", "abandoned", "archived"})


class FrogParityError(ValueError):
    """Raised when a parity request or stored authority record is malformed."""


class ParityClassification(StrEnum):
    MATCH = "match"
    EXPLAINED_DIFFERENCE = "explained_difference"
    UNAVAILABLE = "unavailable"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class ParityCheck:
    id: str
    classification: ParityClassification
    required: bool
    summary: str
    frog: Mapping[str, Any]
    weftmark: Mapping[str, Any]
    detail: str


@dataclass(frozen=True, slots=True)
class FrogParityReport:
    snapshot_digest: str
    source_label: str
    captured_at: datetime
    observed_at: datetime
    repo_path: str | None
    native_ledger_digest: str
    native_ledger_sequence: int
    checks: tuple[ParityCheck, ...]

    @property
    def cutover_ready(self) -> bool:
        return all(
            not check.required
            or check.classification
            in {ParityClassification.MATCH, ParityClassification.EXPLAINED_DIFFERENCE}
            for check in self.checks
        )


class FrogParityService:
    """Compare observed outcomes without mutating Frog or native authority."""

    def __init__(
        self,
        receipts: FrogReceiptService,
        tasks: TaskService,
        workspace: WorkspaceService,
        claims: ClaimService,
        ledger: LedgerService,
    ) -> None:
        self._receipts = receipts
        self._tasks = tasks
        self._workspace = workspace
        self._claims = claims
        self._ledger = ledger
        self._frog_planning = FrogPlanningService(receipts)
        self._native_planning = TaskPlanningService(tasks)

    def compare(
        self,
        snapshot_digest: str,
        *,
        observed_at: datetime,
        repo_path: str | None = None,
        stale_after_seconds: int = 3600,
    ) -> FrogParityReport:
        _require_aware(observed_at, "observed_at")
        if stale_after_seconds < 1:
            raise FrogParityError("stale_after_seconds must be positive")
        if repo_path is not None:
            repo_path = _requested_repo_path(repo_path)
        for _ in range(8):
            before = self._ledger.snapshot()
            digest, sequence = _ledger_head(before)
            report = self._compare_once(
                snapshot_digest,
                observed_at=observed_at,
                repo_path=repo_path,
                stale_after_seconds=stale_after_seconds,
                native_ledger_digest=digest,
                native_ledger_sequence=sequence,
            )
            after = self._ledger.snapshot()
            if _ledger_head(after) == (digest, sequence):
                return report
        raise FrogParityError("native ledger changed throughout parity comparison")

    def _compare_once(
        self,
        snapshot_digest: str,
        *,
        observed_at: datetime,
        repo_path: str | None,
        stale_after_seconds: int,
        native_ledger_digest: str,
        native_ledger_sequence: int,
    ) -> FrogParityReport:
        receipt = self._receipts.get(snapshot_digest)
        if receipt is None:
            raise FrogParityError(f"Frog snapshot not found: {snapshot_digest}")
        if observed_at < receipt.captured_at:
            raise FrogParityError("observed_at predates the Frog snapshot")
        records = receipt.snapshot["records"]
        source_tasks = {
            _text(value, "slug"): value
            for value in records["tasks"]
            if repo_path is None or value.get("repo_path") == repo_path
        }
        import_receipt = self._import_receipt(
            receipt.source_label, snapshot_digest
        )
        imported_ids = (
            ()
            if import_receipt is None
            else tuple(
                sorted(set(import_receipt["native_tasks"]).intersection(source_tasks))
            )
        )
        checks = (
            _freshness_check(
                receipt.captured_at,
                observed_at,
                stale_after_seconds=stale_after_seconds,
            ),
            self._task_graph_check(source_tasks, import_receipt, imported_ids),
            self._eligibility_check(snapshot_digest, source_tasks, imported_ids),
            self._collision_check(records, observed_at),
            self._lease_check(
                records,
                imported_ids,
                observed_at,
                receipt.captured_at,
                repo_path,
            ),
            self._audit_check(
                records,
                imported_ids,
                observed_at,
                receipt.captured_at,
                repo_path,
            ),
            self._completion_check(source_tasks, imported_ids),
        )
        return FrogParityReport(
            snapshot_digest,
            receipt.source_label,
            receipt.captured_at,
            observed_at,
            repo_path,
            native_ledger_digest,
            native_ledger_sequence,
            checks,
        )

    def _import_receipt(
        self, source_label: str, snapshot_digest: str
    ) -> Mapping[str, Any] | None:
        matches = tuple(
            entry.payload
            for entry in self._ledger.snapshot()
            if entry.kind == "frog_native_task_import"
            and entry.entity_id == source_label
            and entry.payload.get("source_snapshot_digest") == snapshot_digest
        )
        if not matches:
            return None
        payload = matches[-1]
        try:
            payload = _validate_import_receipt(payload, source_label)
            if payload["source_snapshot_digest"] != snapshot_digest:
                raise ValueError("receipt targets another snapshot")
            if payload["state"] != "completed":
                raise ValueError("receipt mismatch")
        except (FrogTaskImportError, KeyError, TypeError, ValueError) as error:
            raise FrogParityError(
                "stored Frog native task import is malformed or targets another snapshot"
            ) from error
        return payload

    def _task_graph_check(
        self,
        source_tasks: Mapping[str, Mapping[str, Any]],
        receipt: Mapping[str, Any] | None,
        imported_ids: tuple[str, ...],
    ) -> ParityCheck:
        native = {value.id: value for value in self._tasks.list()}
        if receipt is None:
            return _check(
                "task_graph",
                ParityClassification.UNAVAILABLE,
                "No completed native import binds this Frog snapshot.",
                {"source_tasks": len(source_tasks)},
                {"provenance_bound_tasks": 0},
                "Slug equality alone is not accepted as import provenance.",
            )
        selected = set(imported_ids)
        expected_dependencies = tuple(
            value
            for value in _pairs(receipt["dependencies"])
            if value[0] in selected and value[1] in selected
        )
        expected_conflicts = {
            _ordered_pair(*value)
            for value in _pairs(receipt["conflicts"])
            if value[0] in selected and value[1] in selected
        }
        actual_dependencies = {
            (value.task_id, value.depends_on_task_id)
            for value in self._tasks.dependencies()
            if value.task_id in selected and value.depends_on_task_id in selected
        }
        actual_conflicts = {
            _ordered_pair(value.first_task_id, value.second_task_id)
            for value in self._tasks.conflicts()
            if value.first_task_id in selected and value.second_task_id in selected
        }
        missing = tuple(value for value in imported_ids if value not in native)
        wrong_priority = tuple(
            value
            for value in imported_ids
            if value in native
            and native[value].priority.value
            != str(receipt["native_tasks"][value]["priority"])
        )
        blockers = bool(
            missing
            or wrong_priority
            or actual_dependencies != set(expected_dependencies)
            or actual_conflicts != expected_conflicts
        )
        return _check(
            "task_graph",
            ParityClassification.BLOCKER if blockers else ParityClassification.MATCH,
            "Imported task identities, priorities and relations are compared exactly.",
            {
                "selected_tasks": len(imported_ids),
                "dependencies": len(expected_dependencies),
                "conflicts": len(expected_conflicts),
            },
            {
                "missing_tasks": list(missing),
                "priority_mismatches": list(wrong_priority),
                "dependencies_match": actual_dependencies == set(expected_dependencies),
                "conflicts_match": actual_conflicts == expected_conflicts,
            },
            "A mismatch is a cutover blocker; imported runtime status is intentionally excluded.",
        )

    def _eligibility_check(
        self,
        snapshot_digest: str,
        source_tasks: Mapping[str, Mapping[str, Any]],
        imported_ids: tuple[str, ...],
    ) -> ParityCheck:
        compared: list[dict[str, Any]] = []
        blockers: list[str] = []
        advanced: list[str] = []
        for task_id in imported_ids:
            if task_id not in source_tasks:
                continue
            frog = self._frog_planning.eligibility(snapshot_digest, task_id)
            native = self._native_planning.eligibility(task_id)
            same = frog.eligible == native.eligible
            if not same and native.task.state in {
                TaskState.IN_PROGRESS,
                TaskState.DONE,
                TaskState.ABANDONED,
            }:
                advanced.append(task_id)
            elif not same:
                blockers.append(task_id)
            compared.append(
                {
                    "task_id": task_id,
                    "frog_eligible": frog.eligible,
                    "weftmark_eligible": native.eligible,
                }
            )
        if not compared:
            classification = ParityClassification.UNAVAILABLE
        elif blockers:
            classification = ParityClassification.BLOCKER
        elif advanced:
            classification = ParityClassification.EXPLAINED_DIFFERENCE
        else:
            classification = ParityClassification.MATCH
        return _check(
            "eligibility",
            classification,
            "Dependency/conflict eligibility is compared for provenance-bound tasks.",
            {"compared": compared},
            {"locally_advanced": advanced, "mismatches": blockers},
            "A locally advanced lifecycle is explained; a selectable-state disagreement blocks cutover.",
        )

    def _collision_check(
        self, records: Mapping[str, Any], observed_at: datetime
    ) -> ParityCheck:
        source_active = sum(
            str(value.get("status") or "").casefold() == "active"
            for value in records["locks"]
        )
        native_active = sum(
            value.state_at(observed_at) is LockState.ACTIVE
            for value in self._claims.list()
        )
        return _check(
            "collision_refusal",
            ParityClassification.UNAVAILABLE,
            "Current ownership is visible, but refused acquisitions are absent.",
            {"active_lock_observations": source_active},
            {"active_claims": native_active},
            "Neither immutable snapshot nor native ledger records a paired refused attempt, so absence of overlap is not refusal proof.",
        )

    def _lease_check(
        self,
        records: Mapping[str, Any],
        imported_ids: tuple[str, ...],
        observed_at: datetime,
        captured_at: datetime,
        repo_path: str | None,
    ) -> ParityCheck:
        source_by_task: dict[str, str] = {}
        for value in records["locks"]:
            scope = str(value.get("scope_key") or "")
            if not scope.startswith("task:"):
                continue
            task_id = scope[5:]
            if task_id not in imported_ids:
                continue
            if not _lock_matches_repo(value, repo_path):
                continue
            source_by_task[task_id] = _frog_lock_state(
                value, observed_at, captured_at=captured_at
            )
        task_claims = TaskClaimService(
            self._native_planning,
            self._tasks,
            self._workspace,
            self._claims,
            self._ledger,
        )
        compared: list[dict[str, str]] = []
        differences: list[str] = []
        for task_id, frog_state in sorted(source_by_task.items()):
            binding = task_claims.get(task_id)
            if binding is None:
                continue
            claim = self._claims.get(binding.claim_id)
            if claim is None:
                raise FrogParityError("native task binding references a missing claim")
            native_state = claim.state_at(observed_at).value
            compared.append(
                {"task_id": task_id, "frog": frog_state, "weftmark": native_state}
            )
            if frog_state != native_state:
                differences.append(task_id)
        classification = (
            ParityClassification.UNAVAILABLE
            if not compared
            else ParityClassification.EXPLAINED_DIFFERENCE
            if differences
            else ParityClassification.MATCH
        )
        return _check(
            "stale_lease",
            classification,
            "Effective lease state is compared only for paired task ownership.",
            {"paired_source_locks": len(compared)},
            {"comparisons": compared, "differences": differences},
            "Differences remain visible because snapshot and native observations may occur at different lifecycle points.",
        )

    def _audit_check(
        self,
        records: Mapping[str, Any],
        imported_ids: tuple[str, ...],
        observed_at: datetime,
        captured_at: datetime,
        repo_path: str | None,
    ) -> ParityCheck:
        source_files: dict[str, set[str]] = {}
        for value in records["task_files"]:
            task_id = str(value.get("task_slug") or "")
            if task_id in imported_ids:
                source_files.setdefault(task_id, set()).add(
                    _frog_file_path(value.get("file_path"))
                )
        lock_files: dict[str, set[str]] = {}
        for value in records["locks"]:
            scope = str(value.get("scope_key") or "")
            if (
                scope.startswith("task:")
                and scope[5:] in imported_ids
                and _lock_matches_repo(value, repo_path)
                and _frog_lock_state(
                    value, observed_at, captured_at=captured_at
                )
                == "active"
            ):
                lock_files.setdefault(scope[5:], set()).update(
                    _frog_file_paths(value.get("file_paths"))
                )
        bindings = {value.change_set.id: value for value in self._workspace.list_change_sets()}
        task_claims = TaskClaimService(
            self._native_planning,
            self._tasks,
            self._workspace,
            self._claims,
            self._ledger,
        )
        compared: list[dict[str, Any]] = []
        blockers: list[str] = []
        incomplete: list[str] = []
        for task_id in imported_ids:
            binding = task_claims.get(task_id)
            if binding is None or binding.change_set_id not in bindings:
                continue
            source_known = bool(source_files.get(task_id))
            source_covered = source_known and source_files[task_id].issubset(
                lock_files.get(task_id, set())
            )
            change_set = bindings[binding.change_set_id]
            native_audit = ScopeAuditService().audit(
                change_set,
                declared_scopes=tuple(
                    Scope.parse(value) for value in change_set.change_set.scopes
                ),
                audited_at=observed_at,
            )
            compared.append(
                {
                    "task_id": task_id,
                    "frog_covered": source_covered if source_known else None,
                    "weftmark_within_scope": native_audit.is_within_scope,
                }
            )
            if (source_known and not source_covered) or not native_audit.is_within_scope:
                blockers.append(task_id)
            elif not source_known:
                incomplete.append(task_id)
        classification = (
            ParityClassification.UNAVAILABLE
            if not compared
            else ParityClassification.BLOCKER
            if blockers
            else ParityClassification.UNAVAILABLE
            if incomplete
            else ParityClassification.MATCH
        )
        return _check(
            "scope_audit",
            classification,
            "Declared-file coverage is compared for tasks with paired work bindings.",
            {"source_tasks_with_files": len(source_files)},
            {
                "comparisons": compared,
                "uncovered": blockers,
                "source_coverage_unavailable": incomplete,
            },
            "Missing source file declarations remain null rather than being treated as covered.",
        )

    def _completion_check(
        self,
        source_tasks: Mapping[str, Mapping[str, Any]],
        imported_ids: tuple[str, ...],
    ) -> ParityCheck:
        native = {value.id: value for value in self._tasks.list()}
        source_terminal = tuple(
            sorted(
                task_id
                for task_id, value in source_tasks.items()
                if str(value.get("workflow_status") or "").casefold() in _TERMINAL
            )
        )
        native_done = tuple(
            sorted(
                task_id
                for task_id in imported_ids
                if task_id in native and native[task_id].state is TaskState.DONE
            )
        )
        return _check(
            "completion_gate",
            ParityClassification.UNAVAILABLE,
            "Terminal statuses are visible but do not prove equivalent finish gates.",
            {"terminal_tasks": list(source_terminal)},
            {"completed_imported_tasks": list(native_done)},
            "The snapshot excludes Frog finish verification events; a status comparison cannot prove evidence/review/release behavior.",
        )


def _freshness_check(
    captured_at: datetime, observed_at: datetime, *, stale_after_seconds: int
) -> ParityCheck:
    age = (observed_at - captured_at).total_seconds()
    stale = age > stale_after_seconds
    return _check(
        "source_freshness",
        ParityClassification.BLOCKER if stale else ParityClassification.MATCH,
        "The source snapshot must be fresh enough for a cutover decision.",
        {"captured_at": captured_at.isoformat(), "age_seconds": age},
        {"observed_at": observed_at.isoformat(), "stale_after_seconds": stale_after_seconds},
        "A stale snapshot blocks cutover but remains inspectable.",
    )


def _frog_lock_state(
    value: Mapping[str, Any],
    observed_at: datetime,
    *,
    captured_at: datetime,
) -> str:
    status = str(value.get("status") or "").casefold()
    if status in {"stale", "expired"}:
        return "expired"
    if status == "released":
        return "released"
    if status != "active":
        raise FrogParityError("Frog lock has an unsupported status")
    try:
        started_at = datetime.fromisoformat(str(value["started_at"]))
        _require_aware(started_at, "Frog lock started_at")
        if started_at > captured_at:
            raise ValueError("lock starts after source capture")
        lease_seconds = value["lease_seconds"]
        if type(lease_seconds) is not int or lease_seconds < 1:
            raise ValueError("invalid lease")
        expires_at = started_at + timedelta(seconds=lease_seconds)
    except (KeyError, OverflowError, TypeError, ValueError) as error:
        raise FrogParityError("Frog lock lease observation is malformed") from error
    return "expired" if observed_at >= expires_at else "active"


def _lock_matches_repo(value: Mapping[str, Any], repo_path: str | None) -> bool:
    if repo_path is None:
        return True
    value_repo = value.get("repo_path")
    if not isinstance(value_repo, str) or not value_repo.strip():
        raise FrogParityError("Frog lock lacks a valid repository path")
    return value_repo == repo_path


def _requested_repo_path(value: str) -> str:
    if not value.strip() or not value.startswith("/"):
        raise FrogParityError("repo_path must be a non-empty absolute path")
    return posixpath.normpath(value)


def _frog_file_paths(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise FrogParityError("Frog lock file paths are malformed")
    return tuple(_frog_file_path(item) for item in value)


def _frog_file_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FrogParityError("Frog file path is empty or malformed")
    if not value.startswith("/") or posixpath.normpath(value) != value:
        raise FrogParityError("Frog file path is not canonical and absolute")
    return value


def _ledger_head(entries: tuple[Any, ...]) -> tuple[str, int]:
    if not entries:
        return LEDGER_GENESIS_DIGEST, 0
    return entries[-1].digest, entries[-1].sequence


def _pairs(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise FrogParityError("stored Frog relation list is malformed")
    result: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) and part for part in item)
        ):
            raise FrogParityError("stored Frog relation is malformed")
        result.append((item[0], item[1]))
    return tuple(result)


def _ordered_pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


def _text(value: Mapping[str, Any], name: str) -> str:
    text = value.get(name)
    if not isinstance(text, str) or not text.strip():
        raise FrogParityError(f"Frog task has invalid {name}")
    return text


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FrogParityError(f"{name} must include a timezone")


def _check(
    id: str,
    classification: ParityClassification,
    summary: str,
    frog: Mapping[str, Any],
    weftmark: Mapping[str, Any],
    detail: str,
) -> ParityCheck:
    return ParityCheck(id, classification, True, summary, frog, weftmark, detail)


def frog_parity_to_payload(value: FrogParityReport) -> dict[str, Any]:
    counts = {
        classification.value: sum(
            check.classification is classification for check in value.checks
        )
        for classification in ParityClassification
    }
    return {
        "schema": FROG_PARITY_SCHEMA,
        "generated_at": value.observed_at.isoformat(),
        "source": {
            "kind": "frog_snapshot_receipt",
            "label": value.source_label,
            "digest": value.snapshot_digest,
            "captured_at": value.captured_at.isoformat(),
        },
        "filter": {"repo_path": value.repo_path},
        "authority": {
            "mode": "read_only_comparison",
            "frog": "source_observation",
            "weftmark": "native_ledger",
            "native_ledger_digest": value.native_ledger_digest,
            "native_ledger_sequence": value.native_ledger_sequence,
        },
        "cutover_ready": value.cutover_ready,
        "counts": {"checks": len(value.checks), **counts},
        "checks": [
            {
                "id": check.id,
                "classification": check.classification.value,
                "required": check.required,
                "summary": check.summary,
                "frog": dict(check.frog),
                "weftmark": dict(check.weftmark),
                "detail": check.detail,
            }
            for check in value.checks
        ],
    }
