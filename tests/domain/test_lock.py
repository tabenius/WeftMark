from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from weftmark.domain.lock import (
    InvalidLockOperation,
    LockEventKind,
    LockState,
    SemanticLock,
    scopes_overlap,
)
from weftmark.domain.scope import Scope


NOW = datetime(2026, 8, 13, 20, 0, tzinfo=timezone.utc)


def lease(id: str, scope: Scope, *, agent: str = "agent-1") -> SemanticLock:
    return SemanticLock.acquire(
        id=id,
        scope=scope,
        agent_id=agent,
        session_id=f"session-{agent}",
        change_set_id=f"change-{agent}",
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
    )


def test_lease_binds_scope_agent_session_and_change_set() -> None:
    lock = lease("lock-1", Scope.contract("tenant-auth"))

    assert lock.agent_id == "agent-1"
    assert lock.session_id == "session-agent-1"
    assert lock.change_set_id == "change-agent-1"
    assert lock.events[-1].kind is LockEventKind.ACQUIRED
    assert lock.owns_scope_at(NOW + timedelta(minutes=1))


def test_exact_contract_collision_ignores_file_disjointness() -> None:
    first = lease("lock-1", Scope.contract("tenant-auth"), agent="agent-1")
    second = lease("lock-2", Scope.contract("Tenant Auth"), agent="agent-2")

    assert first.conflicts_with(second, at=NOW + timedelta(minutes=1))
    assert not scopes_overlap(Scope.file("src/a/**"), Scope.file("src/b/**"))


def test_different_semantic_kinds_do_not_collide_by_key_alone() -> None:
    assert not scopes_overlap(
        Scope.contract("evidence-v0"), Scope.schema("evidence-v0")
    )


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("src/**", "src/domain/model.py", True),
        ("src/domain/**", "src/domain/*.py", True),
        ("src/a/**", "src/b/**", False),
        ("src/a/**", "src/ab/**", False),
        ("README.md", "README.md", True),
        ("README.md", "docs/README.md", False),
    ],
)
def test_file_scope_overlap_is_deterministic(
    first: str, second: str, expected: bool
) -> None:
    assert scopes_overlap(Scope.file(first), Scope.file(second)) is expected
    assert scopes_overlap(Scope.file(second), Scope.file(first)) is expected


def test_expired_lease_never_grants_ownership_or_conflicts() -> None:
    first = lease("lock-1", Scope.contract("tenant-auth"))
    second = lease("lock-2", Scope.contract("tenant-auth"), agent="agent-2")
    after_expiry = NOW + timedelta(minutes=30)

    assert first.state_at(after_expiry) is LockState.EXPIRED
    assert not first.owns_scope_at(after_expiry)
    assert not first.conflicts_with(second, at=after_expiry)


def test_expiry_and_explicit_release_are_distinct_events() -> None:
    active = lease("lock-1", Scope.boundary("domain-adapter"))
    released = active.release(
        at=NOW + timedelta(minutes=5), reason="work completed"
    )
    expired = active.observe_expiry(at=NOW + timedelta(minutes=31))

    assert released.state is LockState.RELEASED
    assert released.events[-1].kind is LockEventKind.RELEASED
    assert expired.state is LockState.EXPIRED
    assert expired.events[-1].kind is LockEventKind.EXPIRED
    assert expired.events[-1].occurred_at == active.expires_at


def test_renewal_extends_only_a_current_active_lease() -> None:
    active = lease("lock-1", Scope.schema("events/v1"))
    renewed = active.renew(
        at=NOW + timedelta(minutes=10),
        expires_at=NOW + timedelta(hours=1),
    )

    assert renewed.expires_at == NOW + timedelta(hours=1)
    assert renewed.events[-1].kind is LockEventKind.RENEWED

    with pytest.raises(InvalidLockOperation, match="extend"):
        active.renew(
            at=NOW + timedelta(minutes=10),
            expires_at=NOW + timedelta(minutes=20),
        )
    with pytest.raises(InvalidLockOperation, match="expired"):
        active.renew(
            at=NOW + timedelta(minutes=30),
            expires_at=NOW + timedelta(hours=1),
        )

    with pytest.raises(InvalidLockOperation, match="previous event"):
        renewed.release(at=NOW + timedelta(minutes=5), reason="clock moved back")


def test_reacquisition_restores_only_an_expired_lease_with_history() -> None:
    active = lease("lock-1", Scope.schema("events/v1"))
    reacquired = active.reacquire(
        at=NOW + timedelta(minutes=31),
        expires_at=NOW + timedelta(minutes=61),
    )

    assert reacquired.state_at(NOW + timedelta(minutes=31)) is LockState.ACTIVE
    assert reacquired.acquired_at == active.acquired_at
    assert reacquired.events[:-1] == active.events
    assert reacquired.events[-1].kind is LockEventKind.REACQUIRED
    assert reacquired.events[-1].previous_expires_at == active.expires_at

    with pytest.raises(InvalidLockOperation, match="only an expired"):
        active.reacquire(
            at=NOW + timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=61),
        )
    released = active.release(at=NOW + timedelta(minutes=1), reason="done")
    with pytest.raises(InvalidLockOperation, match="only an expired"):
        released.reacquire(
            at=NOW + timedelta(minutes=31),
            expires_at=NOW + timedelta(minutes=61),
        )
    with pytest.raises(InvalidLockOperation, match="after operation time"):
        active.reacquire(
            at=NOW + timedelta(minutes=31),
            expires_at=NOW + timedelta(minutes=31),
        )
