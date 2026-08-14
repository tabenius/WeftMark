"""Explicit promotion of imported Frog task intent into local Change Sets."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from weftmark.application.frog_receipts import FrogReceiptService
from weftmark.application.identifiers import new_id
from weftmark.application.ledger import LedgerService
from weftmark.application.ports.ledger import LEDGER_GENESIS_DIGEST, LedgerHeadChanged
from weftmark.application.workspace import (
    WorkspaceService,
    binding_to_payload,
)
from weftmark.domain.scope import Scope


class FrogPromotionError(ValueError):
    """Raised when imported intent cannot be promoted safely."""


@dataclass(frozen=True, slots=True)
class FrogTaskPromotion:
    source_snapshot_digest: str
    source_label: str
    source_task_slug: str
    source_repo_path: str
    change_set_id: str
    goal: str
    base_revision: str
    scopes: tuple[Scope, ...]
    promoted_at: datetime
    completed: bool


@dataclass(frozen=True, slots=True)
class FrogPromotionResult:
    promotion: FrogTaskPromotion
    change_set: Mapping[str, Any]
    promoted: bool


class FrogPromotionService:
    """Create one local authority record from explicitly selected external intent."""

    def __init__(
        self,
        receipts: FrogReceiptService,
        workspace: WorkspaceService,
        ledger: LedgerService,
    ) -> None:
        self._receipts = receipts
        self._workspace = workspace
        self._ledger = ledger

    def promote(
        self,
        snapshot_digest: str,
        task_slug: str,
        *,
        change_set_id: str | None,
        base_revision: str,
        scopes: tuple[Scope, ...],
        promoted_at: datetime,
    ) -> FrogPromotionResult:
        _require_aware(promoted_at)
        if not scopes:
            raise FrogPromotionError("promotion requires at least one local scope")
        receipt = self._receipts.get(snapshot_digest)
        if receipt is None:
            raise FrogPromotionError(f"Frog snapshot not found: {snapshot_digest}")
        tasks = tuple(
            task
            for task in receipt.snapshot["records"]["tasks"]
            if task.get("slug") == task_slug
        )
        if len(tasks) != 1:
            raise FrogPromotionError(f"Frog task not found: {task_slug}")
        task = tasks[0]
        try:
            goal = str(task["title"]).strip()
            source_repo_path = str(task["repo_path"]).strip()
        except KeyError as error:
            raise FrogPromotionError("Frog task lacks promotion intent") from error
        if not goal or not source_repo_path:
            raise FrogPromotionError("Frog task lacks promotion intent")

        key = _promotion_key(snapshot_digest, task_slug)
        requested_scopes = tuple(sorted(scopes, key=lambda value: value.canonical))
        existing = self._latest(key)
        if existing is not None:
            _require_same_request(
                existing,
                change_set_id=change_set_id,
                base_revision=base_revision,
                scopes=requested_scopes,
            )
            return self._finish(existing, promoted=False)

        selected_id = change_set_id or new_id("chg", at=promoted_at)
        reservation = FrogTaskPromotion(
            snapshot_digest,
            receipt.source_label,
            task_slug,
            source_repo_path,
            selected_id,
            goal,
            base_revision,
            requested_scopes,
            promoted_at,
            False,
        )
        for _ in range(8):
            entries = self._ledger.snapshot()
            existing = _find(entries, key)
            if existing is not None:
                restored = _promotion_from_payload(existing.payload)
                _require_same_request(
                    restored,
                    change_set_id=change_set_id,
                    base_revision=base_revision,
                    scopes=requested_scopes,
                )
                return self._finish(restored, promoted=False)
            expected = entries[-1].digest if entries else LEDGER_GENESIS_DIGEST
            try:
                self._ledger.record_if_head(
                    kind="frog_task_promotion",
                    entity_id=key,
                    payload=_promotion_to_payload(reservation),
                    recorded_at=promoted_at,
                    expected_digest=expected,
                )
                return self._finish(reservation, promoted=True)
            except LedgerHeadChanged:
                continue
        raise FrogPromotionError("ledger remained busy while reserving promotion")

    def get(
        self, snapshot_digest: str, task_slug: str
    ) -> FrogTaskPromotion | None:
        return self._latest(_promotion_key(snapshot_digest, task_slug))

    def _latest(self, key: str) -> FrogTaskPromotion | None:
        entry = self._ledger.latest(kind="frog_task_promotion", entity_id=key)
        return None if entry is None else _promotion_from_payload(entry.payload)

    def _finish(
        self, promotion: FrogTaskPromotion, *, promoted: bool
    ) -> FrogPromotionResult:
        binding = self._workspace.get_change_set(promotion.change_set_id)
        if binding is None:
            binding = self._workspace.create_change_set(
                id=promotion.change_set_id,
                goal=promotion.goal,
                base_revision=promotion.base_revision,
                scopes=promotion.scopes,
                created_at=promotion.promoted_at,
            )
        else:
            payload = binding_to_payload(binding)
            if (
                payload["goal"] != promotion.goal
                or (
                    promotion.base_revision != "HEAD"
                    and payload["base_revision"] != promotion.base_revision
                )
                or tuple(
                    sorted(Scope.from_dict(value).canonical for value in payload["scopes"])
                )
                != tuple(scope.canonical for scope in promotion.scopes)
            ):
                raise FrogPromotionError(
                    "reserved Change Set exists with different local intent"
                )
        if not promotion.completed:
            completed = FrogTaskPromotion(
                promotion.source_snapshot_digest,
                promotion.source_label,
                promotion.source_task_slug,
                promotion.source_repo_path,
                promotion.change_set_id,
                promotion.goal,
                promotion.base_revision,
                promotion.scopes,
                promotion.promoted_at,
                True,
            )
            self._ledger.record(
                kind="frog_task_promotion",
                entity_id=_promotion_key(
                    completed.source_snapshot_digest, completed.source_task_slug
                ),
                payload=_promotion_to_payload(completed),
                recorded_at=completed.promoted_at,
            )
            promotion = completed
        return FrogPromotionResult(promotion, binding_to_payload(binding), promoted)


def promotion_result_to_payload(result: FrogPromotionResult) -> dict[str, Any]:
    return {
        "promoted": result.promoted,
        "source_snapshot_digest": result.promotion.source_snapshot_digest,
        "source_label": result.promotion.source_label,
        "source_task_slug": result.promotion.source_task_slug,
        "source_repo_path": result.promotion.source_repo_path,
        "change_set": dict(result.change_set),
    }


def _promotion_key(snapshot_digest: str, task_slug: str) -> str:
    value = f"{snapshot_digest}\0{task_slug}".encode()
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _find(entries: tuple[Any, ...], key: str) -> Any | None:
    return next(
        (
            entry
            for entry in reversed(entries)
            if entry.kind == "frog_task_promotion" and entry.entity_id == key
        ),
        None,
    )


def _require_same_request(
    promotion: FrogTaskPromotion,
    *,
    change_set_id: str | None,
    base_revision: str,
    scopes: tuple[Scope, ...],
) -> None:
    if (
        (change_set_id is not None and change_set_id != promotion.change_set_id)
        or base_revision != promotion.base_revision
        or scopes != promotion.scopes
    ):
        raise FrogPromotionError(
            f"Frog task already promoted as {promotion.change_set_id} with different local intent"
        )


def _promotion_to_payload(value: FrogTaskPromotion) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "state": "completed" if value.completed else "reserved",
        "source_snapshot_digest": value.source_snapshot_digest,
        "source_label": value.source_label,
        "source_task_slug": value.source_task_slug,
        "source_repo_path": value.source_repo_path,
        "change_set_id": value.change_set_id,
        "goal": value.goal,
        "base_revision": value.base_revision,
        "scopes": [scope.to_dict() for scope in value.scopes],
        "promoted_at": value.promoted_at.isoformat(),
    }


def _promotion_from_payload(payload: Mapping[str, Any]) -> FrogTaskPromotion:
    try:
        if payload["schema_version"] != 1:
            raise ValueError("unsupported promotion schema")
        state = str(payload["state"])
        if state not in {"reserved", "completed"}:
            raise ValueError("unsupported promotion state")
        promoted_at = datetime.fromisoformat(str(payload["promoted_at"]))
        _require_aware(promoted_at)
        scopes = tuple(
            sorted(
                (Scope.from_dict(value) for value in payload["scopes"]),
                key=lambda value: value.canonical,
            )
        )
        if not scopes:
            raise ValueError("promotion has no local scopes")
        values = tuple(
            str(payload[name]).strip()
            for name in (
                "source_snapshot_digest",
                "source_label",
                "source_task_slug",
                "source_repo_path",
                "change_set_id",
                "goal",
                "base_revision",
            )
        )
        if not all(values):
            raise ValueError("promotion metadata is empty")
        return FrogTaskPromotion(*values, scopes, promoted_at, state == "completed")
    except (KeyError, TypeError, ValueError) as error:
        raise FrogPromotionError("stored Frog task promotion is malformed") from error


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FrogPromotionError("promoted_at must include a timezone")
