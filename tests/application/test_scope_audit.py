from __future__ import annotations

from datetime import datetime, timezone

import pytest

from weftmark.application.change_binding import ChangeBinding, GitLineageObservation
from weftmark.application.scope_audit import ScopeAuditError, ScopeAuditService
from weftmark.domain.changeset import ChangeSet
from weftmark.domain.review import FindingSeverity
from weftmark.domain.scope import Scope


NOW = datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc)


def binding(
    *,
    changed: tuple[str, ...] = ("src/app.py",),
    dirty: tuple[str, ...] = (),
) -> ChangeBinding:
    change_set = ChangeSet.plan(
        id="chg-1",
        goal="Keep work in scope",
        repository_id="repo-1",
        base_sha="a" * 40,
        branch="feature",
        worktree="/work/repo",
        at=NOW,
    ).activate(head_sha="b" * 40, at=NOW)
    observation = GitLineageObservation(
        id="chg-1:git:1",
        repository_id="repo-1",
        base_revision="main",
        base_sha="a" * 40,
        head_sha="b" * 40,
        branch="feature",
        worktree="/work/repo",
        changed_paths=changed,
        dirty_paths=dirty,
        observed_at=NOW,
    )
    return ChangeBinding(change_set, "main", (observation,))


def test_file_changes_inside_glob_scope_pass() -> None:
    result = ScopeAuditService().audit(
        binding(changed=("src/app.py", "src/domain/model.py")),
        declared_scopes=(Scope.file("src/**"),),
        audited_at=NOW,
    )

    assert result.is_within_scope
    assert result.findings == ()


def test_committed_and_dirty_paths_outside_scope_become_blocking_findings() -> None:
    result = ScopeAuditService().audit(
        binding(changed=("src/app.py", "README.md"), dirty=("secrets.env",)),
        declared_scopes=(Scope.file("src/**"),),
        audited_at=NOW,
    )

    assert not result.is_within_scope
    assert result.actual_paths == ("README.md", "secrets.env", "src/app.py")
    assert result.uncovered_paths == ("README.md", "secrets.env")
    assert all(
        finding.severity is FindingSeverity.BLOCKING
        for finding in result.findings
    )


def test_declared_semantic_change_passes_without_file_inference() -> None:
    declared = (
        Scope.file("src/**"),
        Scope.contract("tenant-auth"),
        Scope.boundary("credentials"),
    )
    result = ScopeAuditService().audit(
        binding(),
        declared_scopes=declared,
        semantic_changes=(
            Scope.contract("Tenant Auth"),
            Scope.boundary("credentials"),
        ),
        audited_at=NOW,
    )

    assert result.is_within_scope
    assert result.semantic_changes[0] == Scope.contract("tenant-auth")


def test_undeclared_semantic_change_is_visible_as_a_blocker() -> None:
    changed = Scope.schema("ledger-v2")
    result = ScopeAuditService().audit(
        binding(),
        declared_scopes=(Scope.file("src/**"), Scope.schema("ledger-v1")),
        semantic_changes=(changed,),
        audited_at=NOW,
    )

    assert result.undeclared_semantic_scopes == (changed,)
    assert result.findings[0].scope == changed
    assert "schema:ledger-v2" in result.findings[0].rationale


def test_audit_does_not_mutate_or_widen_declared_scope() -> None:
    declared = (Scope.file("src/**"),)
    result = ScopeAuditService().audit(
        binding(changed=("README.md",)),
        declared_scopes=declared,
        audited_at=NOW,
    )

    assert declared == (Scope.file("src/**"),)
    assert result.declared_scopes == declared
    assert result.findings[0].scope not in result.declared_scopes


def test_findings_are_deterministic_for_same_observation() -> None:
    service = ScopeAuditService()
    kwargs = {
        "declared_scopes": (Scope.file("src/**"),),
        "semantic_changes": (Scope.surface("cli"),),
        "audited_at": NOW,
    }
    first = service.audit(binding(changed=("README.md",)), **kwargs)
    second = service.audit(binding(changed=("README.md",)), **kwargs)

    assert first == second
    assert tuple(finding.id for finding in first.findings) == (
        "chg-1:git:1:scope:file:1",
        "chg-1:git:1:scope:semantic:2",
    )


def test_duplicate_or_file_semantic_inputs_fail_closed() -> None:
    service = ScopeAuditService()
    with pytest.raises(ScopeAuditError, match="declared_scopes"):
        service.audit(
            binding(),
            declared_scopes=(Scope.file("src/**"), Scope.file("src/**")),
            audited_at=NOW,
        )
    with pytest.raises(ScopeAuditError, match="semantic_changes cannot"):
        service.audit(
            binding(),
            declared_scopes=(Scope.file("src/**"),),
            semantic_changes=(Scope.file("README.md"),),
            audited_at=NOW,
        )


def test_audit_timestamp_must_be_timezone_aware() -> None:
    with pytest.raises(ScopeAuditError, match="timezone"):
        ScopeAuditService().audit(
            binding(),
            declared_scopes=(Scope.file("src/**"),),
            audited_at=NOW.replace(tzinfo=None),
        )
