"""Audit observed file and declared semantic changes without widening scope."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from weftmark.application.change_binding import ChangeBinding
from weftmark.domain.lock import scopes_overlap
from weftmark.domain.review import FindingSeverity, ReviewFinding
from weftmark.domain.scope import Scope, ScopeKind


class ScopeAuditError(ValueError):
    """Raised when a scope audit request is ambiguous or malformed."""


@dataclass(frozen=True, slots=True)
class ScopeAuditResult:
    observation_id: str
    declared_scopes: tuple[Scope, ...]
    actual_paths: tuple[str, ...]
    semantic_changes: tuple[Scope, ...]
    findings: tuple[ReviewFinding, ...]
    audited_at: datetime

    def __post_init__(self) -> None:
        if not self.observation_id or not self.observation_id.strip():
            raise ScopeAuditError("observation_id must not be empty")
        if self.audited_at.tzinfo is None or self.audited_at.utcoffset() is None:
            raise ScopeAuditError("audited_at must include a timezone")
        for name, scopes in (
            ("declared_scopes", self.declared_scopes),
            ("semantic_changes", self.semantic_changes),
        ):
            if len({scope.identity for scope in scopes}) != len(scopes):
                raise ScopeAuditError(f"{name} must not contain duplicates")

    @property
    def is_within_scope(self) -> bool:
        return not self.findings

    @property
    def uncovered_paths(self) -> tuple[str, ...]:
        return tuple(
            finding.scope.key
            for finding in self.findings
            if finding.scope.kind is ScopeKind.FILE
        )

    @property
    def undeclared_semantic_scopes(self) -> tuple[Scope, ...]:
        return tuple(
            finding.scope
            for finding in self.findings
            if finding.scope.kind is not ScopeKind.FILE
        )


class ScopeAuditService:
    def audit(
        self,
        binding: ChangeBinding,
        *,
        declared_scopes: tuple[Scope, ...],
        semantic_changes: tuple[Scope, ...] = (),
        audited_at: datetime,
    ) -> ScopeAuditResult:
        if audited_at.tzinfo is None or audited_at.utcoffset() is None:
            raise ScopeAuditError("audited_at must include a timezone")
        if len({scope.identity for scope in declared_scopes}) != len(declared_scopes):
            raise ScopeAuditError("declared_scopes must not contain duplicates")
        if len({scope.identity for scope in semantic_changes}) != len(semantic_changes):
            raise ScopeAuditError("semantic_changes must not contain duplicates")
        if any(scope.kind is ScopeKind.FILE for scope in semantic_changes):
            raise ScopeAuditError("semantic_changes cannot contain file scopes")

        observation = binding.latest
        actual_paths = tuple(
            sorted(set(observation.changed_paths) | set(observation.dirty_paths))
        )
        declared_files = tuple(
            scope for scope in declared_scopes if scope.kind is ScopeKind.FILE
        )
        findings: list[ReviewFinding] = []

        for path in actual_paths:
            actual_scope = Scope.file(path)
            if not any(
                scopes_overlap(declared, actual_scope) for declared in declared_files
            ):
                findings.append(
                    ReviewFinding(
                        id=f"{observation.id}:scope:file:{len(findings) + 1}",
                        severity=FindingSeverity.BLOCKING,
                        scope=actual_scope,
                        rationale=f"changed path is outside declared file scope: {path}",
                        created_at=audited_at,
                        updated_at=audited_at,
                    )
                )

        for changed_scope in semantic_changes:
            if not any(
                scopes_overlap(declared, changed_scope) for declared in declared_scopes
            ):
                findings.append(
                    ReviewFinding(
                        id=f"{observation.id}:scope:semantic:{len(findings) + 1}",
                        severity=FindingSeverity.BLOCKING,
                        scope=changed_scope,
                        rationale=(
                            "semantic change is outside declared scope: "
                            f"{changed_scope.canonical}"
                        ),
                        created_at=audited_at,
                        updated_at=audited_at,
                    )
                )

        return ScopeAuditResult(
            observation_id=observation.id,
            declared_scopes=declared_scopes,
            actual_paths=actual_paths,
            semantic_changes=semantic_changes,
            findings=tuple(findings),
            audited_at=audited_at,
        )
